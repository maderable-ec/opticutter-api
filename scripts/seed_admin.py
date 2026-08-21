"""Seeds the first administrator from ADMIN_EMAIL in .env; prompts for the password."""

import getpass
import sys

sys.path.insert(0, ".")

from src.modules.branches.model import (
    BranchModel,  # noqa: F401 — registers branches table for users.branch_id FK
)
from src.modules.users.enums import UserRole
from src.modules.users.model import UserModel
from src.shared.config import config
from src.shared.database import SessionLocal
from src.shared.security import hash_password

if not config.ADMIN_EMAIL:
    print("ERROR: set ADMIN_EMAIL in .env")
    sys.exit(1)

db = SessionLocal()
try:
    exists = db.query(UserModel).filter(UserModel.email == config.ADMIN_EMAIL).first()
    if exists:
        print(f"User already exists: {config.ADMIN_EMAIL}")
        sys.exit(0)

    password = getpass.getpass("Contraseña del administrador: ")
    confirm = getpass.getpass("Confirmar contraseña: ")
    if not password or password != confirm:
        print("ERROR: las contraseñas no coinciden o están vacías")
        sys.exit(1)

    db.add(
        UserModel(
            email=config.ADMIN_EMAIL,
            hashed_password=hash_password(password),
            role=UserRole.ADMIN.value,
            is_active=True,
        )
    )
    db.commit()
    print(f"Admin created: {config.ADMIN_EMAIL}")
finally:
    db.close()
