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
from src.modules.products.service import edge_width_fits_board, normalize_family
from src.modules.products.types.board import BoardAttributes
from src.modules.products.types.edge_banding import BandType, EdgeBandingAttributes
from src.modules.settings.service import SettingsService
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
#
# The vendor's own convention prints the LARGO (length) first, ANCHO (width)
# second — group(1) is always stored as `height`/largo (see
# ``BoardAttributes.height``'s docstring), group(2) as `width`/ancho. The
# regex doesn't enforce group(1) > group(2): a shorter-first pair is valid
# syntax but is very likely the vendor writing the sides backwards, which
# ``_collect_warnings`` flags instead of silently accepting.
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
    """``"15%"``/``"15.00"`` -> ``0.15``. Blank -> ``0.0``. Malformed -> ``None`` (error).

    The catalog stores prices NET, so this rate is no longer applied to anything
    — the tax is added once, at the document level, from the rate configured in
    ``settings``. It is still read because a row whose own rate differs from that
    one would be billed wrong in silence, and saying so is cheap
    (``_collect_warnings``). A malformed value still skips the row: a price
    column that can't be trusted next to a tax column that can't be parsed is
    not a row to import.
    """
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


# Sentinel for "this text isn't a number at all", so a level price can return
# three outcomes: a value, ``None`` (not loaded by the vendor) and malformed.
_MALFORMED = object()


def _parse_level_price(raw: str):
    """``"76.13"`` -> 76.13. Blank/zero -> ``None``. Malformed -> ``_MALFORMED``.

    Zero is the vendor's own default for a level nobody filled in (the column is
    ``NOT NULL DEFAULT 0.000000``), so it means "no reduced price at this level",
    never "free". Storing ``None`` is what makes the fallback to the list price
    explicit downstream instead of quoting a board at $0 — measured on the live
    catalog, 11 articles have no level 2 and 30 no level 3.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return _MALFORMED
    return round(value, 6) if value > 0 else None


@dataclass
class _ValidRow:
    row_no: int
    codigo: str
    external_code: str
    product_type: ProductType
    name: str
    description: Optional[str]
    # The three NET sale prices. ``price`` (level 1) is always present;
    # ``price_2``/``price_3`` are ``None`` when the source never loaded them.
    price: float
    price_2: Optional[float]
    price_3: Optional[float]
    # The row's own tax rate, carried only so ``_collect_warnings`` can flag a
    # row the configured rate would bill wrong. Never applied to the price.
    iva_rate: float
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


def _collect_warnings(valid: Sequence[_ValidRow], tax_rate: float) -> List[_Issue]:
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
    * A family that coordinates but whose stocked widths cover none of its
      own boards (``edge_width_fits_board``) is reported: a 36 mm board whose
      design only comes in 19 mm tape has a picker as empty as a board with a
      broken family, and from the seller's chair the two are the same problem.
      Only families that HAVE bandings are checked, so this never restates the
      orphan-family warning below.
    * A **tapacanto thicker than it is wide** is a measurement that cannot
      exist: the vendor almost certainly dropped a decimal point ("18X45MM"
      where its siblings say "18X0.45MM"), and the thickness is also what the
      band type is inferred from, so the row lands in the catalog mislabelled.
      Reported, not corrected — the fix belongs in the inventory system.
    * A **price level above the one before it** is reported per row: the levels
      are meant to descend (``ven`` >= ``pv2`` >= ``pv3``), so an inverted pair
      is a data-entry slip that would make "apply the discount" charge MORE.
      Two such rows exist in the live catalog, one of them plainly wrong
      (``ven`` 4.46 against ``pv2`` 61.16).
    * An article **whose own tax rate isn't the configured one** is reported:
      the tax is now added once at the document level, so a 0%-rated product
      would be billed at the configured rate in complete silence.
    * A level the vendor never loaded is reported **once per level**, anchored
      on the first article missing it, not once per article: 11 articles have no
      level 2 and 30 no level 3, and 41 lines would bury everything else. It is
      also not a defect — falling back to the list price is the correct reading
      of "this design has no reduced price" — so what the operator needs is to
      know it happens, not a roll call.
    * A **board whose largo ended up shorter than its ancho** is flagged too:
      the vendor's convention prints the longer side first (``_BOARD_DIMS_RE``'s
      first captured measure is always stored as ``height``/largo), so an
      inverted pair usually means the source data has the two sides swapped,
      not that the board is genuinely narrow-first. Reported, not corrected —
      the sync stores whichever number came first, exactly like every other
      strict-parsing rule in this module.
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
    # Every width a family is stocked in, and the first board that needs each
    # (family, thickness) combination covered. Keyed by thickness and not just
    # by family because a design can coordinate perfectly at 15mm and have no
    # tape wide enough for its 36mm sibling — that's one gap, not two.
    widths_by_family: Dict[str, set] = {}
    boards_by_family_thickness: Dict[Tuple[str, float], _ValidRow] = {}

    # Anchors for the once-per-level warnings, plus how many rows they stand for.
    missing_level: Dict[int, Tuple[_ValidRow, int]] = {}

    for row in valid:
        for level, value in ((2, row.price_2), (3, row.price_3)):
            if value is None:
                anchor, count = missing_level.get(level, (row, 0))
                missing_level[level] = (anchor, count + 1)
        if row.price_2 is not None and row.price_2 > row.price:
            warn(
                row,
                f"el Precio 2 (${row.price_2:.2f}) es mayor que el de lista "
                f"(${row.price:.2f}): revisar los precios en el inventario",
            )
        upper = row.price_2 if row.price_2 is not None else row.price
        if row.price_3 is not None and row.price_3 > upper:
            warn(
                row,
                f"el Precio 3 (${row.price_3:.2f}) es mayor que el anterior "
                f"(${upper:.2f}): revisar los precios en el inventario",
            )
        if row.iva_rate != tax_rate:
            warn(
                row,
                f"el IVA del artículo ({row.iva_rate * 100:g}%) no es el "
                f"configurado ({tax_rate * 100:g}%): se va a facturar con el "
                "configurado",
            )

        family = normalize_family(row.attributes.get("family"))
        if family:
            # setdefault on the outer dict too: a future ProductType reaching
            # the sync should not KeyError its way out of a warning.
            first_by_family.setdefault(row.product_type, {}).setdefault(family, row)

        if row.product_type is ProductType.BOARD:
            height = row.attributes.get("height")
            width = row.attributes.get("width")
            if height is not None and width is not None and height < width:
                warn(
                    row,
                    f"el largo ({height:g}mm) es menor que el ancho "
                    f"({width:g}mm): revisar si el proveedor escribió el "
                    "lado más corto primero",
                )
            thickness = row.attributes.get("thickness")
            if family and thickness is not None:
                boards_by_family_thickness.setdefault((family, thickness), row)
            continue

        band_width = row.attributes.get("width")
        band_thickness = row.attributes.get("thickness")
        if band_width is not None and family:
            # The width still counts towards coverage even on the row below:
            # when the two numbers disagree it's the thickness that lost its
            # decimal point, and a width the family really stocks shouldn't be
            # dropped from the check on the strength of the other field.
            widths_by_family.setdefault(family, set()).add(band_width)
        if (
            band_width is not None
            and band_thickness is not None
            and band_thickness > band_width
        ):
            warn(
                row,
                f"el espesor ({band_thickness:g}mm) es mayor que el ancho "
                f"({band_width:g}mm): un tapacanto así no existe, revisar la "
                "medida en el inventario",
            )

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

    for level, (anchor, count) in sorted(missing_level.items()):
        others = f" (y {count - 1} artículo{'s' if count > 2 else ''} más)"
        warn(
            anchor,
            f"sin Precio {level} en el inventario{others if count > 1 else ''}: "
            f"se cobra{'n' if count > 1 else ''} al precio de lista",
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

    for (family, thickness), row in boards_by_family_thickness.items():
        widths = widths_by_family.get(family)
        if not widths:
            # No banding at all on this family: already reported above, and
            # saying it twice would only make the list harder to read.
            continue
        if any(edge_width_fits_board(thickness, w) for w in widths):
            continue
        stocked = "/".join(f"{w:g}" for w in sorted(widths))
        warn(
            row,
            f"la familia '{row.attributes['family']}' no tiene ningún "
            f"tapacanto que cubra un tablero de {thickness:g}mm "
            f"(solo hay de {stocked}mm)",
        )

    warnings.sort(key=lambda w: w.row_no)
    return warnings


def _validate(
    rows: Sequence[SourceRow],
    tax_rate: float,
) -> Tuple[List[_ValidRow], int, List[_Issue], List[_Issue]]:
    """Splits the source rows into what can be imported and what can't.

    ``tax_rate`` is the configured rate the documents will bill at. It is not
    applied to anything here — prices are stored net — only compared against
    each row's own rate so a mismatch gets reported instead of silently billed.

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

        # Los tres precios se guardan NETOS, tal como los publica el proveedor:
        # el IVA se suma una sola vez, a nivel de documento, con la tasa
        # configurada en `settings`. NO se redondea a 2 decimales: el proveedor
        # escribe 6 justamente para que el precio CON impuesto sea redondo
        # (79.086957 * 1.15 = 90.95 exacto), y redondear el neto aquí correría
        # ese número un centavo.
        price = round(price, 6)
        price_2 = _parse_level_price(row.p_venta_2)
        price_3 = _parse_level_price(row.p_venta_3)
        if price_2 is _MALFORMED or price_3 is _MALFORMED:
            problem(
                f"precio de nivel 2/3 ('{row.p_venta_2}'/'{row.p_venta_3}') "
                "no es un número válido",
                external_code,
            )
            continue

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
                "height": round(float(match.group(1)) * 1000, 2),
                "width": round(float(match.group(2)) * 1000, 2),
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
                price_2=price_2,
                price_3=price_3,
                iva_rate=iva_rate,
                attributes=validated.model_dump(by_alias=True, mode="json"),
            )
        )

    return valid, skipped_medio, issues, _collect_warnings(valid, tax_rate)


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
                    price_2=row.price_2,
                    price_3=row.price_3,
                    is_active=True,
                    attributes=row.attributes,
                )
            )
            created += 1
        else:
            product.name = row.name
            product.description = row.description
            product.price = row.price
            product.price_2 = row.price_2
            product.price_3 = row.price_3
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

    tax_rate = SettingsService(db).get_tax_rate()
    valid_rows, skipped_medio, issues, warnings = _validate(active_rows, tax_rate)
    return _apply(
        db,
        valid_rows,
        skipped_medio,
        len(retired_rows),
        issues,
        warnings,
        dry_run=dry_run,
    )
