"""External catalog sync: upserts board/edge-banding products from the vendor's
inventory system (see ``docs/ARCHITECTURE.md``'s ``products`` section).

The source rows come from ``external_catalog.py``, which owns the vendor's
schema; everything here is source-agnostic and works on ``SourceRow`` text.

Parsing is deliberately STRICT, not lenient: dimensions are physical
measurements used to cut real material, so a misparsed row is far worse than
a rejected one. A row whose data doesn't parse is therefore **skipped and
reported** (``ProductSyncResult.issues``), never guessed at — and never fatal:
the source is a live database, so an article with no usable dimensions can't
be "fixed and re-uploaded" before every run the way a file could. Skipped rows
are also protected from the reconciliation pass, since "we couldn't read it"
must not be confused with "the vendor removed it".

What DOES abort the whole sync, with zero writes, is a conflict with the
catalog itself — a code or name already owned by a hand-created product. That
is a problem at the destination, not in the source data, and importing over it
would corrupt a catalog nobody asked to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from src.modules.orders.model import OrderBoardModel, OrderLineModel, OrderPieceModel
from src.modules.products.external_catalog import SourceRow, fetch_rows
from src.modules.products.model import ProductModel, ProductType
from src.modules.products.schemas import ProductSyncIssue, ProductSyncResult
from src.modules.products.service import normalize_family
from src.modules.products.types.board import BoardAttributes
from src.modules.products.types.edge_banding import BandType, EdgeBandingAttributes
from src.shared.exceptions import BulkValidationError

_CATEGORIA_TO_TYPE = {
    "TABLEROS": ProductType.BOARD,
    "TAPACANTOS": ProductType.EDGE_BANDING,
}

# Half-board SKUs the vendor tracks separately; this system computes half
# boards automatically from the full board at optimize time (price/2), so
# these rows are skipped rather than imported as their own catalog product.
_MEDIO_RE = re.compile(r"\bMEDI[OA]\b", re.IGNORECASE)

# Confirmed going-forward format: "(2.07X2.80)M-15MM" -> the two dimensions in
# meters (2 decimals), thickness in mm (integer or fractional — OSB/MDF/veneer
# commonly ship in fractional thicknesses like 9.5, 11.1, 5.5mm). Deliberately
# strict on width/height: older/legacy formats ("(244X122)X5MM",
# "1200X2400X45MM", ...) are ambiguous about units (is "244" already mm, or
# truncated meters?) and guessing wrong would silently produce the wrong
# physical dimensions. Rows that don't match this exact pattern are reported
# as validation errors, never guessed at.
_BOARD_DIMS_RE = re.compile(
    r"\((\d\.\d{2})[Xx](\d\.\d{2})\)M-(\d+(?:\.\d+)?)MM", re.IGNORECASE
)

# Confirmed going-forward format: "40X1.5MM" -> width/thickness in mm, no unit
# ambiguity at this scale. Slightly permissive (optional parens/spaces, "MM"
# or "M") to tolerate minor formatting noise without risking a wrong
# physical measurement.
_EDGE_DIMS_RE = re.compile(
    r"\(?\s*(\d+)\s*[Xx]\s*(\d+(?:\.\d+)?)\s*MM?\)?\s*$", re.IGNORECASE
)

# The vendor's tax column is a rate, written either bare ("15.00", straight from
# the database) or with a percent sign ("15%", as its printed reports render it).
_IVA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%?")


# The vendor's OBS. column carries the design data, one field for both
# categories: the FAMILY, optionally followed by the edge banding's short ALIAS.
#
#   "Cashmere"        -> family "Cashmere", no alias
#   "Cashmere - CSH"  -> family "Cashmere", alias "CSH"
#
# The family comes FIRST because it is the half that always applies: it is the
# board<->edge-banding coordination key (``ProductService.find_edge_bandings_for_board``)
# and a board has nothing else to write there. The alias — the short code the
# workshop notation prints (``2L1C CS CSH``) — only means anything on an edge
# banding, so it is an optional suffix rather than a mandatory prefix. Writing
# just the family is therefore correct in both categories, which is the whole
# point: the previous "ALIAS - FAMILY" order forced a board to invent an alias
# (or lead with an invisible " - ") and silently yielded NO family at all when
# it didn't.
#
# Both categories parse identically and the board simply drops the alias. That
# is deliberate: an operator who writes the same text on a board and on its
# edge banding ("Cashmere - CSH" in both) still gets a match, because both
# sides trim the same way. A board-takes-OBS-whole rule would break exactly
# there.
_OBS_SEP = " - "

# The alias is a short code: no inner whitespace and within the schema's cap
# (``EdgeBandingAttributes.alias``, 20 chars). A right-hand side that doesn't
# look like one is not an alias — it's part of the family. This keeps
# "Cashmere - Cashmere Claro" from becoming an alias that overflows the
# proforma's "Cantos" column, sized for "2L1C CS CSH".
_ALIAS_RE = re.compile(r"^\S{1,20}$")


def _parse_obs(obs: str) -> Tuple[Optional[str], Optional[str]]:
    """``"FAMILY"``/``"FAMILY - ALIAS"`` -> ``(family, alias)``; empty -> ``(None, None)``.

    Splits on the **last** separator so a hyphen inside the design's own name
    stays on the family side: ``"Roble Barroco - Dorado - RBD"`` yields
    ``("Roble Barroco - Dorado", "RBD")``.
    """
    text = obs.strip()
    if not text:
        return None, None
    if _OBS_SEP in text:
        family, alias = text.rsplit(_OBS_SEP, 1)
        family, alias = family.strip(), alias.strip()
        if family and _ALIAS_RE.match(alias):
            return family, alias
    return text, None


def _parse_iva_rate(iva: str) -> Optional[float]:
    """``"15%"``/``"15.00"`` -> ``0.15``. Blank -> ``0.0``. Malformed -> ``None`` (error)."""
    if not iva.strip():
        return 0.0
    m = _IVA_RE.fullmatch(iva.strip())
    if not m:
        return None
    return float(m.group(1)) / 100


def _external_code(categoria: str, codigo: str) -> str:
    """Namespaced match key, e.g. ``"TABLEROS:1033"``.

    Namespacing by category is what lets a board and an edge banding share a
    bare code in the vendor's system without colliding here.
    """
    return f"{categoria}:{codigo}"


@dataclass
class _ValidRow:
    row_no: int
    codigo: str
    external_code: str
    product_type: ProductType
    name: str
    description: Optional[str]
    price: float
    attributes: dict


@dataclass
class _Issue:
    """Something to report about a source row, and why.

    Used for both severities: a row that couldn't be imported (``issues``) and
    one that imported with a coordination problem (``warnings``,
    ``_collect_warnings``). Only the former reaches the reconciliation shield.

    ``external_code`` is kept so the reconciliation pass can leave the product
    alone: a row we failed to read is not a row the vendor deleted. It's
    ``None`` only when the row's category was unrecognizable, in which case
    there is no key to protect anyway.
    """

    row_no: int
    codigo: str
    name: str
    message: str
    external_code: Optional[str]

    def to_schema(self) -> ProductSyncIssue:
        return ProductSyncIssue(code=self.codigo, name=self.name, message=self.message)


def _conflict(row_no: int, codigo: str, message: str) -> dict:
    """A destination conflict, which aborts the sync.

    ``field`` keeps the read position so the list sorts stably, but the message
    leads with the vendor's CODIGO: that's what the operator searches for in
    the inventory system.
    """
    return {"field": str(row_no), "message": f"Código {codigo}: {message}"}


def _collect_warnings(valid: Sequence[_ValidRow]) -> List[_Issue]:
    """Rows that imported fine but whose design data can't do its job.

    Nothing here skips a row or blocks the sync — these products are in the
    catalog and sellable. What they've lost is the board<->edge-banding
    *coordination*, which fails **silently**: a board with no family (or with a
    family no banding shares) simply returns an empty picker, and nobody finds
    out until a seller can't quote the tapacanto. Reporting it is the only way
    the operator sees it, and the dry run is where they'd want to.

    Deliberately narrow, so the list stays worth reading:

    * A **board** with no family is NOT a warning. Plywood, OSB and MDF fondo
      have no coordinated banding at all, and warning about every one of them
      would bury the real problems.
    * A family is only checked for a counterpart when it was actually
      *declared* — writing one is the statement of intent that makes its
      absence on the other side a mistake.
    * The thickness->width rule (``BOARD_THICKNESS_TO_EDGE_WIDTH``) is left
      out on purpose: "no soft banding for a 36 mm board" is a real catalog
      gap, not a data-entry error. Matching at the family level is what catches
      the drift this is for — a board left on "Cashmere" while its banding
      moved to "CSH".
    """
    warnings: List[_Issue] = []

    def warn(row: _ValidRow, message: str) -> None:
        warnings.append(
            _Issue(
                row_no=row.row_no,
                codigo=row.codigo,
                name=row.name,
                message=message,
                external_code=row.external_code,
            )
        )

    # First row that declared each family, per side — the anchor for the
    # "no counterpart" warning, so an orphan family is reported once instead of
    # once per article that carries it.
    first_by_family: Dict[ProductType, Dict[str, _ValidRow]] = {
        ProductType.BOARD: {},
        ProductType.EDGE_BANDING: {},
    }

    for row in valid:
        family = normalize_family(row.attributes.get("family"))
        if family:
            # setdefault on the outer dict too: a future ProductType reaching
            # the sync should not KeyError its way out of a warning.
            first_by_family.setdefault(row.product_type, {}).setdefault(family, row)

        if row.product_type is not ProductType.EDGE_BANDING:
            continue
        if not family:
            # An edge banding always *is* a design, so a missing family here is
            # a blank field, not a product that has none.
            warn(
                row,
                "tapacanto sin familia (OBS. vacío): no va a coordinar con "
                "ningún tablero",
            )
        elif not row.attributes.get("alias"):
            warn(
                row,
                "tapacanto sin alias (OBS. sin ' - CÓDIGO'): la notación de "
                "despiece no lo va a distinguir de otro diseño",
            )

    boards = first_by_family[ProductType.BOARD]
    bandings = first_by_family[ProductType.EDGE_BANDING]
    for family, row in boards.items():
        if family not in bandings:
            warn(
                row,
                f"la familia '{row.attributes['family']}' solo aparece en "
                "tableros; ningún tapacanto la coordina",
            )
    for family, row in bandings.items():
        if family not in boards:
            warn(
                row,
                f"la familia '{row.attributes['family']}' solo aparece en "
                "tapacantos; ningún tablero la usa",
            )

    warnings.sort(key=lambda w: w.row_no)
    return warnings


def _validate(
    rows: Sequence[SourceRow],
) -> Tuple[List[_ValidRow], int, List[_Issue], List[_Issue]]:
    """Splits the source rows into what can be imported and what can't.

    Nothing here is fatal: every problem becomes an ``_Issue`` the caller
    reports, so one unusable article can't hold back the whole catalog.

    Returns ``(valid, skipped_medio, issues, warnings)``. The last two share a
    shape but not a meaning: an **issue** is a row that was skipped, a
    **warning** is a row that imported with a coordination problem
    (``_collect_warnings``). Keeping them apart matters beyond presentation —
    ``_apply`` reads ``issues`` to shield those products from reconciliation,
    and a warned row was read just fine.
    """
    issues: List[_Issue] = []
    valid: List[_ValidRow] = []
    skipped_medio = 0
    seen_external_codes: Dict[str, str] = {}
    seen_codigos: Dict[str, str] = {}
    seen_names: Dict[str, str] = {}

    for row in rows:
        if _MEDIO_RE.search(row.articulo):
            skipped_medio += 1
            continue

        def problem(message: str, external_code: Optional[str] = None) -> None:
            issues.append(
                _Issue(
                    row_no=row.row_no,
                    codigo=row.codigo,
                    name=row.articulo,
                    message=message,
                    external_code=external_code,
                )
            )

        categoria = row.categoria.strip().upper()
        product_type = _CATEGORIA_TO_TYPE.get(categoria)
        if product_type is None:
            problem(f"CATEGORIA '{row.categoria}' no reconocida")
            continue

        external_code = _external_code(categoria, row.codigo)

        if external_code in seen_external_codes:
            other = seen_external_codes[external_code]
            problem(
                f"código duplicado dentro del inventario (también en '{other}')",
                external_code,
            )
            continue
        seen_external_codes[external_code] = row.articulo

        # `code` (unlike `external_code`) is the bare CODIGO, not namespaced by
        # categoría — so a board and an edge banding sharing the same CODIGO
        # would collide on `code` even though their `external_code`s differ.
        if row.codigo in seen_codigos:
            other = seen_codigos[row.codigo]
            problem(
                "código duplicado dentro del inventario entre tableros y "
                f"tapacantos (también en '{other}')",
                external_code,
            )
            continue
        seen_codigos[row.codigo] = row.articulo

        if row.articulo in seen_names:
            other = seen_names[row.articulo]
            problem(
                f"nombre '{row.articulo}' duplicado dentro del inventario "
                f"(también en el código {other})",
                external_code,
            )
            continue
        seen_names[row.articulo] = row.codigo

        family, alias = _parse_obs(row.obs)

        try:
            price = float(row.p_venta)
        except ValueError:
            problem(
                f"precio de venta '{row.p_venta}' no es un número válido",
                external_code,
            )
            continue

        iva_rate = _parse_iva_rate(row.iva)
        if iva_rate is None:
            problem(f"IVA '{row.iva}' no es un porcentaje válido", external_code)
            continue
        # El precio de venta viene sin IVA; el catálogo guarda el precio final
        # con impuesto incluido.
        price = round(price * (1 + iva_rate), 2)

        if product_type is ProductType.BOARD:
            match = _BOARD_DIMS_RE.search(row.articulo)
            if not match:
                problem(
                    "no se pudieron extraer las medidas "
                    "(formato esperado: (2.07X2.80)M-15MM)",
                    external_code,
                )
                continue
            attrs = {
                "width": round(float(match.group(1)) * 1000, 2),
                "height": round(float(match.group(2)) * 1000, 2),
                "thickness": float(match.group(3)),
            }
            if row.tipo:
                attrs["subtype"] = row.tipo
            if family:
                attrs["family"] = family
            try:
                validated = BoardAttributes(**attrs)
            except PydanticValidationError as exc:
                problem(
                    f"atributos de tablero inválidos: {exc.errors()[0]['msg']}",
                    external_code,
                )
                continue
        else:
            match = _EDGE_DIMS_RE.search(row.articulo)
            if not match:
                problem(
                    "no se pudieron extraer las medidas "
                    "(formato esperado: 40X1.5MM)",
                    external_code,
                )
                continue
            thickness = float(match.group(2))
            attrs = {
                "width": float(match.group(1)),
                "thickness": thickness,
                # The vendor's data has no band-type column; thickness is the
                # tell (see BandType's docstring: soft ~0.45mm, hard 1.0/1.5mm).
                "band_type": (
                    BandType.HARD.value if thickness >= 1 else BandType.SOFT.value
                ),
            }
            grupo = row.grupo.strip()
            if grupo and grupo != "-":
                attrs["subtype"] = grupo
            if family:
                attrs["family"] = family
            if alias:
                attrs["alias"] = alias
            try:
                validated = EdgeBandingAttributes(**attrs)
            except PydanticValidationError as exc:
                problem(
                    f"atributos de tapacanto inválidos: {exc.errors()[0]['msg']}",
                    external_code,
                )
                continue

        valid.append(
            _ValidRow(
                row_no=row.row_no,
                codigo=row.codigo,
                external_code=external_code,
                product_type=product_type,
                name=row.articulo,
                description=row.marca or None,
                price=price,
                attributes=validated.model_dump(by_alias=True, mode="json"),
            )
        )

    return valid, skipped_medio, issues, _collect_warnings(valid)


def _is_product_in_use(db: Session, product_id: int) -> bool:
    """Whether any durable order record references this product by FK.

    Pre-orders/optimization drafts aren't checked — they reference products
    only inside a JSON blob, not a DB foreign key (see ``_apply``).
    """
    return (
        db.query(OrderLineModel.id)
        .filter(OrderLineModel.product_id == product_id)
        .first()
        is not None
        or db.query(OrderPieceModel.id)
        .filter(OrderPieceModel.product_id == product_id)
        .first()
        is not None
        or db.query(OrderBoardModel.id)
        .filter(OrderBoardModel.product_id == product_id)
        .first()
        is not None
    )


def _apply(
    db: Session,
    valid_rows: List[_ValidRow],
    skipped_medio: int,
    skipped_inactive: int,
    issues: List[_Issue],
    warnings: List[_Issue],
    *,
    dry_run: bool,
) -> ProductSyncResult:
    """Upserts the validated rows and reconciles what the source no longer has.

    On create, ``code`` is set to the bare CODIGO from the source (editable
    afterward, like any manually created product's code) — ``external_code``
    is the namespaced ``"{CATEGORIA}:{CODIGO}"`` key used for matching on
    re-sync. Since CODIGO isn't namespaced, a board and an edge banding
    sharing the same CODIGO would collide on ``code``; that's validated for
    explicitly (see ``seen_codigos`` in ``_validate``).

    Aborts with zero writes only on a conflict with the catalog itself: a name
    or code already owned by a product this sync didn't create. Row-level data
    problems are carried in ``issues`` and skipped instead. ``warnings`` is
    pass-through: those rows are written like any other, and only the result
    carries them.

    Reconciliation: a product is reconciled only if it was created by a
    previous sync (has a non-null ``external_code``), its ``type`` matches
    one present in this read, and its ``external_code`` is neither among this
    read's valid rows nor among its skipped ones — which covers the articles
    the vendor retired (``est``/``FecEli``) and those that vanished from the
    table, while leaving alone the ones we simply failed to parse. Hand-created
    products (``external_code`` null) are never touched. A reconciled product
    is deleted outright if nothing durable references it (no ``order_lines``/
    ``order_pieces``/``order_boards`` row); otherwise it's deactivated instead.
    Pre-orders and optimization drafts reference products only inside a JSON
    blob (no DB FK), so they're not checked here — deleting a product still
    referenced by an open pre-order will surface as a "product not found" the
    next time that quote is re-optimized.

    ``dry_run`` runs the whole pass and rolls back, so the caller sees exactly
    what would happen without touching the catalog.
    """
    existing = db.query(ProductModel).all()
    existing_by_external_code = {
        p.external_code: p for p in existing if p.external_code
    }
    existing_by_name = {p.name: p for p in existing}
    existing_by_code = {p.code: p for p in existing}

    conflicts: List[dict] = []
    for row in valid_rows:
        name_owner = existing_by_name.get(row.name)
        if name_owner is not None and name_owner.external_code != row.external_code:
            conflicts.append(
                _conflict(
                    row.row_no,
                    row.codigo,
                    f"el nombre '{row.name}' ya lo usa otro producto del catálogo "
                    f"(id {name_owner.id}, no vinculado a este código externo)",
                )
            )
        code_owner = existing_by_code.get(row.codigo)
        if (
            row.external_code not in existing_by_external_code
            and code_owner is not None
        ):
            conflicts.append(
                _conflict(
                    row.row_no,
                    row.codigo,
                    f"el código '{row.codigo}' ya lo usa otro producto del "
                    f"catálogo (id {code_owner.id})",
                )
            )

    if conflicts:
        conflicts.sort(key=lambda e: int(e["field"]) if e["field"] else -1)
        raise BulkValidationError(
            "El inventario externo choca con productos del catálogo",
            errors=conflicts,
        )

    created = updated = 0
    synced_codes_by_type: Dict[ProductType, set] = {}
    for row in valid_rows:
        synced_codes_by_type.setdefault(row.product_type, set()).add(row.external_code)
        product = existing_by_external_code.get(row.external_code)
        if product is None:
            db.add(
                ProductModel(
                    type=row.product_type.value,
                    code=row.codigo,
                    external_code=row.external_code,
                    name=row.name,
                    description=row.description,
                    price=row.price,
                    is_active=True,
                    attributes=row.attributes,
                )
            )
            created += 1
        else:
            product.name = row.name
            product.description = row.description
            product.price = row.price
            product.attributes = row.attributes
            product.is_active = True
            updated += 1

    # A row we couldn't read is not a row the vendor removed, so its product
    # stays exactly as it is instead of being deleted or deactivated.
    unreadable = {i.external_code for i in issues if i.external_code}

    deactivated = deleted = 0
    for product_type, codes in synced_codes_by_type.items():
        stale = (
            db.query(ProductModel)
            .filter(
                ProductModel.type == product_type.value,
                ProductModel.external_code.isnot(None),
                ProductModel.is_active.is_(True),
                ~ProductModel.external_code.in_(codes | unreadable),
            )
            .all()
        )
        for product in stale:
            if _is_product_in_use(db, product.id):
                product.is_active = False
                deactivated += 1
            else:
                db.delete(product)
                deleted += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return ProductSyncResult(
        created=created,
        updated=updated,
        deactivated=deactivated,
        deleted=deleted,
        skipped_medio=skipped_medio,
        skipped_inactive=skipped_inactive,
        skipped_invalid=len(issues),
        issues=[i.to_schema() for i in issues],
        warnings=[w.to_schema() for w in warnings],
        dry_run=dry_run,
    )


FetchRows = Callable[[], Tuple[List[SourceRow], List[SourceRow]]]


def sync_catalog_from_external(
    db: Session,
    *,
    dry_run: bool = False,
    fetch: FetchRows = fetch_rows,
) -> ProductSyncResult:
    """Syncs the catalog against the vendor's inventory system.

    ``fetch`` is injectable so tests drive the whole pipeline without a live
    MySQL; in production it reads ``marticulo`` (see ``external_catalog.py``).

    Retired rows never reach ``_validate``: they only need to be *absent* from
    the valid set for the reconciliation pass to take them out, and validating
    them would produce a pile of issues about articles nobody sells any more.
    """
    active_rows, retired_rows = fetch()

    if not active_rows:
        # Reconciliation removes whatever this read didn't bring, so an empty
        # read would wipe the catalog. Nothing legitimate looks like this —
        # it's a broken filter, an empty table or the wrong database.
        raise BulkValidationError(
            "El inventario externo no devolvió ningún producto activo; "
            "no se aplicaron cambios",
            errors=[
                {
                    "field": None,
                    "message": (
                        "La consulta al inventario externo devolvió 0 productos "
                        "activos. Se abortó la sincronización para no vaciar el "
                        "catálogo."
                    ),
                }
            ],
        )

    valid_rows, skipped_medio, issues, warnings = _validate(active_rows)
    return _apply(
        db,
        valid_rows,
        skipped_medio,
        len(retired_rows),
        issues,
        warnings,
        dry_run=dry_run,
    )
