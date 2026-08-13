"""Seed script: syncs the Trend 2026 boards + edge bandings catalog.

By default it **upserts by name** (idempotent): existing products are updated in
place — the row keeps its ``id``, so orders referencing it stay valid — and
missing ones are created. Pass ``--reset`` for a hard rebuild: it unlinks orders
from the catalog products (the ``product_id`` FKs are nullable; orders keep their
``product_code``/``product_name``) and deletes every board/edge banding before
recreating them fresh.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

from src.modules.products.model import ProductModel, ProductType
from src.modules.users.model import (
    UserModel,  # noqa: F401 — registers users table for FK resolution
)
from src.shared.database import SessionLocal

CATALOG_TYPES = [ProductType.BOARD.value, ProductType.EDGE_BANDING.value]
# Order tables whose (nullable) product_id references the catalog; unlinked on --reset.
_REFERENCING_TABLES = ("order_boards", "order_pieces", "order_lines")

TEXTURE_DESC = {
    "BS": "acabado Bureau Structure, superficie estructurada suave",
    "SU": "acabado Super Matt, superficie ultra mate sin brillo",
    "PE": "acabado Pearl, superficie perlada de tacto sedoso",
    "PW": "acabado Pure Wood, textura madera pura de alta definición",
    "RW": "acabado Rift Wood, veta rift de madera aserrada",
    "SN": "acabado Super Natural, textura ultrarrealista imitación madera",
}

# (abbr, display_name, category, grain_direction, texture)
DESIGNS = [
    # Solids
    ("CSH", "Cashmere", "SL", None, "BS"),
    ("GRT", "Gris Ratón", "SL", None, "SU"),
    ("ANT", "Antracita", "SL", None, "PE"),
    ("NGR", "Negro", "SL", None, "PE"),
    ("BNV", "Blanco Nieve", "SL", None, "BS"),
    # Oaks
    ("COE", "Costa Evoke", "RO", "H", "PW"),
    ("ARD", "Artesanal Dorado", "RO", "H", "PW"),
    ("BRD", "Barroco Dorado", "RO", "H", "RW"),
    ("BRA", "Barroco Ámbar", "RO", "H", "RW"),
    ("BRR", "Barroco Ristretto", "RO", "H", "RW"),
    # Lights
    ("IBZ", "Ibiza Lineal", "CL", "H", "SN"),
    ("CHA", "Chantillí", "CL", "H", "SN"),
    ("JAP", "Japandi", "CL", "H", "SN"),
    # Cremonas
    ("COT", "Cotta", "CR", "H", "PW"),
    ("TOR", "Torro", "CR", "H", "PW"),
    ("CAN", "Cannolo", "CR", "H", "PW"),
]

CATEGORY_LABEL = {"SL": "Sólido", "RO": "Roble", "CL": "Claro", "CR": "Cremona"}

PRICES = {
    ("SL", 15, False): 48.00,
    ("SL", 15, True): 56.00,
    ("SL", 36, False): 95.00,
    ("SL", 36, True): 112.00,
    ("RO", 15, False): 62.00,
    ("RO", 15, True): 72.00,
    ("RO", 36, False): 122.00,
    ("RO", 36, True): 140.00,
    ("CL", 15, False): 58.00,
    ("CL", 15, True): 68.00,
    ("CL", 36, False): 115.00,
    ("CL", 36, True): 132.00,
    ("CR", 15, False): 64.00,
    ("CR", 15, True): 75.00,
    ("CR", 36, False): 126.00,
    ("CR", 36, True): 145.00,
}


def make_board(
    abbr,
    name,
    cat,
    grain,
    texture,
    thickness,
    rh,
    height=2800,
    width=2070,
    name_suffix="",
    family=None,
):
    rh_label = " RH" if rh else ""
    rh_text = ", resistente a la humedad" if rh else ""
    description = f"MDP {CATEGORY_LABEL[cat]} {name}, {thickness}mm, {TEXTURE_DESC[texture]}{rh_text}"
    return {
        "type": ProductType.BOARD.value,
        # Short code: {abbr}-{thickness}{R?} (e.g. CSH-15, CSH-36R). The board↔tapacanto
        # pairing no longer relies on the code — it uses the shared ``family`` below.
        "code": f"{abbr}-{thickness}{'R' if rh else ''}",
        "name": f"MDP {thickness}mm {name}{name_suffix}{rh_label}",
        "description": description[:256],
        "price": PRICES[(cat, thickness, rh)],
        "is_active": True,
        # Attributes in the products catalog's canonical camelCase shape.
        "attributes": {
            "height": height,
            "width": width,
            "thickness": thickness,
            "grainDirection": grain,
            # Shared design family: a board and its coordinated tapacanto carry the SAME value,
            # which is how the optimizer infers the tapacanto. It's the SHORT design code
            # (``CSH``, not "Cashmere") because the tapacanto's family is also what gets
            # printed at the end of the workshop notation ("1L CS CSH"); the readable label
            # lives in the tapacanto's ``color``. Defaults to the code prefix, but they're
            # separate arguments: a variant can have its own prefix and still share a family.
            "family": family or abbr,
        },
    }


def build_boards():
    boards = []
    for abbr, name, cat, grain, texture in DESIGNS:
        for thickness in (15, 36):
            for rh in (False, True):
                boards.append(
                    make_board(abbr, name, cat, grain, texture, thickness, rh)
                )
    # Blanco Nieve special 2440 mm format
    for thickness in (15, 36):
        for rh in (False, True):
            boards.append(
                make_board(
                    # Distinct short code prefix (BNS-*), but the BNV ``family`` of the base
                    # boards, so it stays coordinated with the same Blanco tapacanto.
                    "BNS",
                    "Blanco Nieve",
                    "SL",
                    None,
                    "BS",
                    thickness,
                    rh,
                    height=2440,
                    width=2070,
                    name_suffix=" Especial",
                    family="BNV",
                )
            )
    return boards


# --- Edge bandings ---------------------------------------------------------
#
# Each board design has a coordinated PVC edge banding (1:1 mapping with
# DESIGNS). Except for Blanco, all come in the 3 standard sizes. The "type" is
# the Spanish label the client sees in the name/description; the value stored
# in attributes.bandType is its canonical English equivalent (see
# BAND_TYPE_VALUE), which is what the endpoint filters on.
#   (type, thickness_mm, width_mm)
EDGE_BAND_VARIANTS = [
    ("Suave", 0.45, 19),
    ("Duro", 1.00, 40),
    ("Duro", 1.50, 19),
]

# Spanish label (client-facing) -> canonical English value of the BandType enum.
BAND_TYPE_VALUE = {"Suave": "Soft", "Duro": "Hard"}

# TODO: PLACEHOLDER prices (not provided). Replace with the real ones.
EDGE_PRICES = {
    (0.45, 19): 12.00,
    (1.00, 40): 22.00,
    (1.50, 19): 15.00,
}


def edge_label(abbr, name, cat):
    """Commercial label of the edge banding based on the design's category."""
    if abbr == "BNV":  # the edge banding is named "Blanco", not "Blanco Nieve"
        name = "Blanco"
    if cat == "RO":
        return f"Roble {name}"
    if cat == "CR":
        return f"{name} Cremona"
    return name


def make_edge_banding(abbr, name, cat, band_type, thickness, width):
    label = edge_label(abbr, name, cat)
    thick_txt = f"{thickness:g}"  # 0.45, 1, 1.5 (no trailing zeros)
    band_value = BAND_TYPE_VALUE[band_type]  # canonical English value (Soft/Hard)
    description = (
        f"Tapacanto PVC {label}, tipo {band_type}, {thick_txt}mm x {width}mm, "
        f"coordinado con tablero MDP {label}"
    )
    return {
        "type": ProductType.EDGE_BANDING.value,
        # Short code: {abbr}-C{thickness_centi} (e.g. CSH-C045, CSH-C150).
        "code": f"{abbr}-C{int(round(thickness * 100)):03d}",
        "name": f"Tapacanto PVC {label} {band_type} {thick_txt}x{width}mm",
        "description": description[:256],
        "price": EDGE_PRICES[(thickness, width)],
        "is_active": True,
        # Attributes in the products catalog's canonical camelCase shape.
        "attributes": {
            "bandType": band_value,
            "thickness": thickness,
            "width": width,
            # Readable commercial label ("Roble Barroco Dorado"), the long form the
            # short ``family`` code replaced.
            "color": label,
            # Shared design family, matching the coordinated board's. The SHORT code,
            # because this is also what the workshop notation prints ("1L CS CSH").
            "family": abbr,
        },
    }


def build_edge_bandings():
    bands = []
    for abbr, name, cat, _grain, _texture in DESIGNS:
        # Blanco isn't offered in all 3 coordinated sizes; only the standard Suave.
        variants = [EDGE_BAND_VARIANTS[0]] if abbr == "BNV" else EDGE_BAND_VARIANTS
        for band_type, thickness, width in variants:
            bands.append(
                make_edge_banding(abbr, name, cat, band_type, thickness, width)
            )
    return bands


def upsert_products(db, items, label):
    """Idempotent upsert by name (unique + stable): updates existing rows in place
    (keeping their ``id`` so order FKs stay valid) and creates the missing ones."""
    created = updated = 0
    for data in items:
        existing = (
            db.query(ProductModel).filter(ProductModel.name == data["name"]).first()
        )
        if existing is None:
            db.add(ProductModel(**data))
            created += 1
        else:
            existing.code = data["code"]
            existing.description = data["description"]
            existing.price = data["price"]
            existing.is_active = data["is_active"]
            existing.attributes = data["attributes"]
            updated += 1
    db.flush()
    print(f"  {label}: {created} created, {updated} updated.")


def reset_catalog(db):
    """Hard reset: unlinks orders from the catalog products (nullable ``product_id``
    FKs; orders keep ``product_code``/``product_name``) and deletes every board and
    edge banding so they're recreated with fresh ids.

    Uses raw SQL for the unlink to avoid importing the order ORM models (which would
    pull the whole mapper graph into this standalone script)."""
    # CATALOG_TYPES holds trusted enum values, so this interpolation is injection-safe.
    types_in = ", ".join(f"'{t}'" for t in CATALOG_TYPES)
    subquery = f"SELECT id FROM products WHERE type IN ({types_in})"
    for table in _REFERENCING_TABLES:
        db.execute(
            text(
                f"UPDATE {table} SET product_id = NULL WHERE product_id IN ({subquery})"
            )
        )
    deleted = (
        db.query(ProductModel)
        .filter(ProductModel.type.in_(CATALOG_TYPES))
        .delete(synchronize_session=False)
    )
    db.flush()
    print(
        f"Reset: unlinked orders and deleted {deleted} existing boards/edge bandings."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Seed the boards + edge bandings catalog."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Hard rebuild: unlink orders and delete the catalog before recreating it.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.reset:
            reset_catalog(db)
        print("Boards:")
        upsert_products(db, build_boards(), "tableros")
        print("Edge bandings:")
        upsert_products(db, build_edge_bandings(), "tapacantos")
        db.commit()
        print("\n✅ Catalog seeded.")
    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
