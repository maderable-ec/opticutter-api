"""Seeds branches and the singleton company/cutting settings row.

Populates ``branches`` and ``settings`` with the real business data (branch
addresses, cutting parameters, price tiers, company letterhead) instead of
relying on hand-entry through the admin panel or the placeholder defaults in
``config.py``. Idempotent: upserts by ``code``/singleton id, safe to re-run
to resync values after editing them below.

Requires the schema to already exist (migrations applied). Examples:

    make seed-settings
    DATABASE_URL=postgresql://cutter:cutter@localhost:5433/cutter_db \\
        .venv/bin/python scripts/seed_settings.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.modules.branches.model import BranchModel  # noqa: E402
from src.modules.settings.model import SETTINGS_ID, SettingsModel  # noqa: E402
from src.modules.users.model import (  # noqa: E402, F401
    UserModel,  # registers users table for branches/settings.created_by|updated_by FKs
)
from src.shared.database import SessionLocal  # noqa: E402

BRANCHES = [
    {
        "code": "SUCUA",
        "name": "Sucúa",
        "address": "Calle Pastor Bernal entre Domingo Comín y Juan Sangurima.",
        "phone": None,
        "is_active": True,
        "print_labels_enabled": False,
        "print_consolidated_enabled": False,
    },
    {
        "code": "MACAS",
        "name": "Macas",
        "address": "Av. 29 de Mayo y Pedro Nolasco.",
        "phone": None,
        "is_active": True,
        "print_labels_enabled": False,
        "print_consolidated_enabled": True,
    },
]

SETTINGS = {
    # Cutting parameters (mm)
    "kerf": 4,
    "top_trim": 10,
    "bottom_trim": 10,
    "left_trim": 10,
    "right_trim": 10,
    "edge_banding_waste_factor": 0.3,
    "half_board_markup_pct": 0.1,
    # Pre-orders
    "preorder_validity_days": 2,
    "max_open_preorders_per_client": 5,
    # Price tiers
    "price_tiers": [
        {
            "code": "consumidor",
            "name": "Precio Consumidor",
            "rate": 0.0,
            "is_active": True,
            "sort_order": 1,
        },
        {
            "code": "carpintero",
            "name": "Precio Carpintero",
            "rate": 0.02,
            "is_active": True,
            "sort_order": 2,
        },
        {
            "code": "efectivo",
            "name": "Precio Efectivo",
            "rate": 0.05,
            "is_active": True,
            "sort_order": 3,
        },
    ],
    # Company letterhead
    "company_name": "MADERABLE",
    "company_tagline": "tableros + accesorios",
    "company_email": "maderable.ec@gmail.com",
    "company_phone": "0982259098 / 09886449936",
    "company_branches": [
        {
            "name": "Sucúa",
            "address": "Calle Pastor Bernal entre Domingo Comín y Juan Sangurima.",
        },
        {"name": "Macas", "address": "Av. 29 de Mayo y Pedro Nolasco."},
    ],
}


def upsert_branches(db) -> None:
    for entry in BRANCHES:
        branch = db.query(BranchModel).filter(BranchModel.code == entry["code"]).first()
        if branch is None:
            db.add(BranchModel(**entry))
            print(f"  + Branch {entry['code']} ({entry['name']})")
        else:
            for field, value in entry.items():
                setattr(branch, field, value)
            print(f"  = Branch {entry['code']} ({entry['name']}) updated")


def upsert_settings(db) -> None:
    settings = db.get(SettingsModel, SETTINGS_ID)
    if settings is None:
        db.add(SettingsModel(id=SETTINGS_ID, **SETTINGS))
        print("  + Settings row created")
    else:
        for field, value in SETTINGS.items():
            setattr(settings, field, value)
        print("  = Settings row updated")


def main() -> None:
    db = SessionLocal()
    try:
        print("Branches:")
        upsert_branches(db)
        print("Settings:")
        upsert_settings(db)
        db.commit()
        print("\n✅ Seed complete.")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
