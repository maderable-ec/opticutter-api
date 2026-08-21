"""External catalog CSV sync: upserts board/edge-banding products from the
vendor's inventory export (see ``docs/ARCHITECTURE.md``'s ``products`` section).

Parsing here is deliberately STRICT, not lenient: dimensions are physical
measurements used to cut real material, so a misparsed row is far worse than
a rejected one. Validation runs as a full pass over every row before any
database write — ``sync_catalog`` raises ``BulkValidationError`` with the
complete list of problems on the first sign of trouble, so the caller fixes
the source file and retries instead of ending up with a partially-imported
catalog.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from src.modules.orders.model import OrderBoardModel, OrderLineModel, OrderPieceModel
from src.modules.products.model import ProductModel, ProductType
from src.modules.products.schemas import ProductSyncResult
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

_HEADER_COLUMNS = (
    "CODIGO",
    "ARTICULO",
    "MARCA",
    "TIPO",
    "CATEGORIA",
    "GRUPO",
    "IVA",
    "P.venta",
    "OBS.",
)

_IVA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%?")


def _decode(data: bytes) -> str:
    """The vendor export is Windows-1252, not UTF-8."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


@dataclass
class _Row:
    row_no: int
    codigo: str
    articulo: str
    marca: str
    tipo: str
    categoria: str
    grupo: str
    iva: str
    p_venta: str
    obs: str


def _parse_rows(text: str) -> List[_Row]:
    """Skips the report/date/blank lines before the real header, and the
    trailing "TOTAL:" summary row — by content, not by a fixed line number.

    ``row_no`` counts data rows (1-indexed), not raw file lines, since the
    junk header block is stripped before counting.
    """
    reader = csv.reader(io.StringIO(text), delimiter=";")
    parsed = [row for row in reader if any(cell.strip() for cell in row)]

    header_idx = next(
        (i for i, cells in enumerate(parsed) if cells and cells[0].strip() == "CODIGO"),
        None,
    )
    if header_idx is None:
        raise BulkValidationError(
            "No se encontró la fila de encabezado (CODIGO) en el archivo",
            errors=[{"field": None, "message": "Encabezado no encontrado"}],
        )
    header = [h.strip() for h in parsed[header_idx]]
    try:
        idx = {name: header.index(name) for name in _HEADER_COLUMNS}
    except ValueError as exc:
        raise BulkValidationError(
            "El archivo no tiene todas las columnas esperadas",
            errors=[{"field": None, "message": str(exc)}],
        ) from exc

    def cell(cells: List[str], key: str) -> str:
        i = idx[key]
        return cells[i].strip() if i < len(cells) else ""

    rows: List[_Row] = []
    row_no = 0
    for cells in parsed[header_idx + 1 :]:
        codigo = cell(cells, "CODIGO")
        if not codigo or codigo.upper().startswith("TOTAL"):
            continue
        row_no += 1
        rows.append(
            _Row(
                row_no=row_no,
                codigo=codigo,
                articulo=cell(cells, "ARTICULO"),
                marca=cell(cells, "MARCA"),
                tipo=cell(cells, "TIPO"),
                categoria=cell(cells, "CATEGORIA"),
                grupo=cell(cells, "GRUPO"),
                iva=cell(cells, "IVA"),
                p_venta=cell(cells, "P.venta"),
                obs=cell(cells, "OBS."),
            )
        )
    return rows


def _split_obs(obs: str) -> Tuple[Optional[str], Optional[str]]:
    """``"ALIAS - FAMILY"`` -> ``(alias, family)``; empty/no separator -> ``(None, None)``."""
    if not obs or " - " not in obs:
        return None, None
    alias, family = obs.split(" - ", 1)
    return alias.strip() or None, family.strip() or None


def _parse_iva_rate(iva: str) -> Optional[float]:
    """``"15%"`` -> ``0.15``. Blank -> ``0.0`` (no surcharge). Malformed -> ``None`` (error)."""
    if not iva.strip():
        return 0.0
    m = _IVA_RE.fullmatch(iva.strip())
    if not m:
        return None
    return float(m.group(1)) / 100


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


def _error(row_no: int, message: str) -> dict:
    return {"field": str(row_no), "message": f"Fila {row_no}: {message}"}


def _validate(rows: List[_Row]) -> Tuple[List[_ValidRow], int, List[dict]]:
    errors: List[dict] = []
    valid: List[_ValidRow] = []
    skipped_medio = 0
    seen_external_codes: Dict[str, int] = {}
    seen_codigos: Dict[str, int] = {}
    seen_names: Dict[str, int] = {}

    for row in rows:
        if _MEDIO_RE.search(row.articulo):
            skipped_medio += 1
            continue

        categoria = row.categoria.strip().upper()
        product_type = _CATEGORIA_TO_TYPE.get(categoria)
        if product_type is None:
            errors.append(
                _error(row.row_no, f"CATEGORIA '{row.categoria}' no reconocida")
            )
            continue

        external_code = f"{categoria}:{row.codigo}"
        if external_code in seen_external_codes:
            other = seen_external_codes[external_code]
            errors.append(
                _error(
                    row.row_no,
                    f"CODIGO '{row.codigo}' duplicado dentro del archivo (también en la fila {other})",
                )
            )
            continue
        seen_external_codes[external_code] = row.row_no

        # `code` (unlike `external_code`) is the bare CODIGO, not namespaced by
        # categoría — so a board and an edge banding sharing the same CODIGO
        # would collide on `code` even though their `external_code`s differ.
        if row.codigo in seen_codigos:
            other = seen_codigos[row.codigo]
            errors.append(
                _error(
                    row.row_no,
                    f"CODIGO '{row.codigo}' duplicado dentro del archivo entre "
                    f"tableros y tapacantos (también en la fila {other})",
                )
            )
            continue
        seen_codigos[row.codigo] = row.row_no

        if row.articulo in seen_names:
            other = seen_names[row.articulo]
            errors.append(
                _error(
                    row.row_no,
                    f"ARTICULO '{row.articulo}' duplicado dentro del archivo (también en la fila {other})",
                )
            )
            continue
        seen_names[row.articulo] = row.row_no

        alias, family = _split_obs(row.obs)

        try:
            price = float(row.p_venta)
        except ValueError:
            errors.append(
                _error(row.row_no, f"P.venta '{row.p_venta}' no es un número válido")
            )
            continue

        iva_rate = _parse_iva_rate(row.iva)
        if iva_rate is None:
            errors.append(
                _error(row.row_no, f"IVA '{row.iva}' no es un porcentaje válido")
            )
            continue
        # P.venta viene sin IVA; el catálogo guarda el precio final con impuesto incluido.
        price = round(price * (1 + iva_rate), 2)

        if product_type is ProductType.BOARD:
            match = _BOARD_DIMS_RE.search(row.articulo)
            if not match:
                errors.append(
                    _error(
                        row.row_no,
                        f"no se pudieron extraer las medidas de '{row.articulo}' "
                        "(formato esperado: (2.07X2.80)M-15MM)",
                    )
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
                errors.append(
                    _error(
                        row.row_no,
                        f"atributos de tablero inválidos: {exc.errors()[0]['msg']}",
                    )
                )
                continue
        else:
            match = _EDGE_DIMS_RE.search(row.articulo)
            if not match:
                errors.append(
                    _error(
                        row.row_no,
                        f"no se pudieron extraer las medidas de '{row.articulo}' "
                        "(formato esperado: 40X1.5MM)",
                    )
                )
                continue
            thickness = float(match.group(2))
            attrs = {
                "width": float(match.group(1)),
                "thickness": thickness,
                # The vendor export has no band-type column; thickness is the
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
                errors.append(
                    _error(
                        row.row_no,
                        f"atributos de tapacanto inválidos: {exc.errors()[0]['msg']}",
                    )
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

    return valid, skipped_medio, errors


def _is_product_in_use(db: Session, product_id: int) -> bool:
    """Whether any durable order record references this product by FK.

    Pre-orders/optimization drafts aren't checked — they reference products
    only inside a JSON blob, not a DB foreign key (see ``sync_catalog``).
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


def sync_catalog(db: Session, file_bytes: bytes) -> ProductSyncResult:
    """Upserts board/edge-banding products from the vendor's CSV export.

    On create, ``code`` is set to the bare CODIGO from the file (editable
    afterward, like any manually created product's code) — ``external_code``
    is the namespaced ``"{CATEGORIA}:{CODIGO}"`` key used for matching on
    re-sync. Since CODIGO isn't namespaced, a board and an edge banding
    sharing the same CODIGO would collide on ``code``; that's validated for
    explicitly (see ``seen_codigos`` in ``_validate``).

    All-or-nothing: any row problem (duplicate external code, code or name,
    unrecognized CATEGORIA/subtype, unparseable dimensions, invalid price)
    aborts with zero writes and the complete list of problems.

    Reconciliation: a product is reconciled only if it was created by a
    previous sync (has a non-null ``external_code``), its ``type`` matches
    one present in this file, and its ``external_code`` is absent from this
    upload's valid rows. Hand-created products (``external_code`` null) are
    never touched. A reconciled product is deleted outright if nothing
    durable references it (no ``order_lines``/``order_pieces``/``order_boards``
    row); otherwise it's deactivated instead, same as before. Pre-orders and
    optimization drafts reference products only inside a JSON blob (no DB
    FK), so they're not checked here — deleting a product still referenced by
    an open pre-order will surface as a "product not found" the next time
    that quote is re-optimized.
    """
    text = _decode(file_bytes)
    rows = _parse_rows(text)
    valid_rows, skipped_medio, errors = _validate(rows)

    existing = db.query(ProductModel).all()
    existing_by_external_code = {
        p.external_code: p for p in existing if p.external_code
    }
    existing_by_name = {p.name: p for p in existing}
    existing_by_code = {p.code: p for p in existing}

    for row in valid_rows:
        name_owner = existing_by_name.get(row.name)
        if name_owner is not None and name_owner.external_code != row.external_code:
            errors.append(
                _error(
                    row.row_no,
                    f"el nombre '{row.name}' ya lo usa otro producto del catálogo "
                    f"(id {name_owner.id}, no vinculado a este código externo)",
                )
            )
        code_owner = existing_by_code.get(row.codigo)
        if (
            row.external_code not in existing_by_external_code
            and code_owner is not None
        ):
            errors.append(
                _error(
                    row.row_no,
                    f"el código '{row.codigo}' ya lo usa otro producto del "
                    f"catálogo (id {code_owner.id})",
                )
            )

    if errors:
        errors.sort(key=lambda e: int(e["field"]) if e["field"] else -1)
        raise BulkValidationError(
            "Se encontraron errores de validación en el archivo", errors=errors
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

    deactivated = deleted = 0
    for product_type, codes in synced_codes_by_type.items():
        stale = (
            db.query(ProductModel)
            .filter(
                ProductModel.type == product_type.value,
                ProductModel.external_code.isnot(None),
                ProductModel.is_active.is_(True),
                ~ProductModel.external_code.in_(codes),
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

    db.commit()

    return ProductSyncResult(
        created=created,
        updated=updated,
        deactivated=deactivated,
        deleted=deleted,
        skipped_medio=skipped_medio,
    )
