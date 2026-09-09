"""Wipes the transactional data (orders, quotes, catalog, history) and keeps the
staff configuration: users, branches, settings and additional services.

The goal is a clean production database to start operating from, WITHOUT losing
the accounts people log in with. It deliberately does **not** truncate ``users``:
``branches.id`` is referenced by ``users.branch_id`` and ``users.id`` by every
``AuditMixin`` table, so emptying and re-seeding them would either need a
``CASCADE`` (taking the preserved rows with it) or a ``DELETE`` that nulls out
the audit trail. Excluding them yields the same end state, atomically, and with
no window where the accounts only exist inside a backup file.

A JSON backup of everything preserved is written first anyway — it is the safety
net, it is what lets you restore the users into a *different* database, and
``--restore`` reads it back.

Wiped tables get ``RESTART IDENTITY``, so ``ORD-2026-0001`` / ``PRE-2026-0001``
(both derived from the row id) start over at 1.

Examples::

    make db-reset                       # local, asks for confirmation
    python scripts/reset_data.py --dry-run
    python scripts/reset_data.py --keep products,clients
    python scripts/reset_data.py --restore backups/reset-cutter_db-20260908-101500.json

In production the procedure follows ``opticutter-infra``'s own convention for
touching live data (``scripts/restore.sh``): take a full backup, stop the API so
nothing writes while the ground moves, run the one-shot, bring the stack back.
From ``/opt/opticutter`` on the VPS::

    ./scripts/backup.sh                       # pg_dump + uploads tarball
    ./scripts/verify-backup.sh                # a backup you have not restored is a file
    docker compose stop api                   # Caddy answers 502 meanwhile
    docker compose run --rm --no-deps -v /opt/opticutter/backups:/backup api \
        python scripts/reset_data.py --backup-dir /backup
    docker compose up -d

``--no-deps`` keeps the one-shot ``migrate`` service from being re-triggered, and
mounting the host's backup directory is what makes the JSON outlive the ``--rm``
container. This is the same ``docker compose run --rm api python scripts/…``
shape that ``docs/DEPLOYMENT.md`` documents for ``seed_admin.py``.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import inspect as sa_inspect  # noqa: E402
from sqlalchemy import text

# Importing each module's models populates ``Base.metadata``: the script derives
# the wipe list from it, so a table nobody imported here would be silently left
# behind. Same canonical list as ``alembic/env.py``.
from src.modules.additional_services.model import (
    AdditionalServiceModel,  # noqa: E402,F401
)
from src.modules.branches.model import BranchModel  # noqa: E402,F401
from src.modules.clients.model import ClientModel  # noqa: E402,F401
from src.modules.notifications.model import NotificationModel  # noqa: E402,F401
from src.modules.optimization_drafts.model import (
    OptimizationDraftModel,  # noqa: E402,F401
)
from src.modules.orders import model as orders_model  # noqa: E402,F401
from src.modules.preorders import model as preorders_model  # noqa: E402,F401
from src.modules.print_jobs import model as print_jobs_model  # noqa: E402,F401
from src.modules.products.model import ProductModel  # noqa: E402,F401
from src.modules.settings.model import SettingsModel  # noqa: E402,F401
from src.modules.users.enums import UserRole  # noqa: E402
from src.modules.users.login_event_model import UserLoginEventModel  # noqa: E402,F401
from src.modules.users.model import UserModel  # noqa: E402
from src.modules.users.refresh_token_model import RefreshTokenModel  # noqa: E402,F401
from src.shared.config import config  # noqa: E402
from src.shared.database import Base, SessionLocal, engine  # noqa: E402

# Configuration that must survive: the accounts people log in with, the branches
# those accounts are bound to, the singleton settings row (letterhead, cutting
# parameters, tax rate) and the hand-typed service price list, which has neither
# a sync nor a seeder.
PRESERVED = ("users", "branches", "settings", "additional_services")

# Alembic's own bookkeeping: not part of the model metadata, and truncating it
# would make the next `alembic upgrade` re-run every migration.
ALEMBIC_TABLE = "alembic_version"


class Abort(Exception):
    """A precondition failed: nothing was written."""


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def plan_tables(extra_keep: set[str]) -> tuple[list[str], list[str]]:
    """Splits the model's tables into (preserved, wiped) and validates the split.

    Two checks earn their keep here. The database is inspected for tables the
    models don't declare (a leftover from a reverted migration would otherwise
    quietly survive the reset), and every preserved table is checked for foreign
    keys into a wiped one — that is exactly the constraint that rules out
    truncating ``users``, and it must keep holding as tables are added.
    """
    known = set(Base.metadata.tables)

    unknown_keep = extra_keep - known
    if unknown_keep:
        raise Abort(f"--keep nombra tablas que no existen: {sorted(unknown_keep)}")

    preserved = set(PRESERVED) | extra_keep
    wiped = known - preserved

    live = set(sa_inspect(engine).get_table_names()) - {ALEMBIC_TABLE}
    if orphans := live - known:
        raise Abort(
            "La base tiene tablas que los modelos no declaran, así que este script "
            f"no sabe qué hacer con ellas: {sorted(orphans)}. "
            "Agrégalas al modelo o pásalas en --keep."
        )
    if missing := known - live:
        raise Abort(
            f"Faltan tablas en la base: {sorted(missing)}. ¿Faltan migraciones "
            "por aplicar (make upgrade)?"
        )

    for name in sorted(preserved):
        for fk in Base.metadata.tables[name].foreign_keys:
            target = fk.column.table.name
            if target in wiped:
                raise Abort(
                    f"'{name}' se preserva pero apunta a '{target}', que se vacía "
                    f"({fk.parent.name} → {target}.{fk.column.name}). Preserva "
                    f"'{target}' también o quita '{name}' de la lista."
                )

    return sorted(preserved), sorted(wiped)


def row_counts(db, tables: list[str]) -> dict[str, int]:
    return {
        t: db.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one() for t in tables
    }


# --------------------------------------------------------------------------- #
# Backup / restore
# --------------------------------------------------------------------------- #


def dump_preserved(db, preserved: list[str], path: Path) -> Path:
    """Writes every preserved row to JSON.

    Users additionally carry ``_branch_code``: restoring into a database whose
    branches were renumbered has to resolve the branch by its stable code, not
    by an id that means nothing there.
    """
    branch_code = {
        row.id: row.code for row in db.execute(text("SELECT id, code FROM branches"))
    }

    payload: dict = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "database": engine.url.database,
        "tables": {},
    }
    for table in preserved:
        rows = [
            dict(r._mapping)
            for r in db.execute(text(f'SELECT * FROM "{table}" ORDER BY 1'))
        ]
        if table == "users":
            for row in rows:
                row["_branch_code"] = branch_code.get(row.get("branch_id"))
        payload["tables"][table] = rows

    path.parent.mkdir(parents=True, exist_ok=True)
    # default=str renders datetimes/Decimals; JSON columns are already dicts.
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return path


def restore_users(db, path: Path) -> None:
    """Re-seeds users from a backup, matching by email and branch **code**.

    Idempotent: an account already present is updated in place rather than
    duplicated, which is what makes re-running safe after a partial restore.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    users = payload.get("tables", {}).get("users")
    if not users:
        raise Abort(f"El respaldo {path} no contiene usuarios.")

    branch_id_by_code = {
        row.code: row.id for row in db.execute(text("SELECT id, code FROM branches"))
    }

    created = updated = 0
    for row in users:
        code = row.get("_branch_code")
        branch_id = branch_id_by_code.get(code) if code else None
        if code and branch_id is None:
            print(
                f"  ! {row['email']}: la sucursal '{code}' ya no existe → sin sucursal"
            )

        user = db.query(UserModel).filter(UserModel.email == row["email"]).first()
        if user is None:
            user = UserModel(email=row["email"])
            # Keep the original id when it is free, so audit references written
            # by other databases still line up.
            if db.get(UserModel, row["id"]) is None:
                user.id = row["id"]
            db.add(user)
            created += 1
        else:
            updated += 1

        user.full_name = row.get("full_name")
        user.hashed_password = row["hashed_password"]
        user.role = row.get("role") or UserRole.OPERATOR.value
        user.is_active = bool(row.get("is_active", True))
        user.branch_id = branch_id

    db.flush()
    # Explicit ids don't advance the sequence: without this the next INSERT
    # would collide with a restored row.
    db.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('users','id'), "
            "GREATEST(COALESCE((SELECT MAX(id) FROM users), 0), 1), "
            "(SELECT MAX(id) FROM users) IS NOT NULL)"
        )
    )
    db.commit()
    print(f"\n✅ Usuarios restaurados: {created} creados, {updated} actualizados.")


# --------------------------------------------------------------------------- #
# Wipe
# --------------------------------------------------------------------------- #


def wipe(db, wiped: list[str]) -> None:
    """Empties every wiped table in one statement and resets their sequences.

    Deliberately no ``CASCADE``: if a future table references one of these and is
    missing from the list, Postgres refuses loudly instead of quietly taking it.
    """
    targets = ", ".join(f'"{t}"' for t in wiped)
    db.execute(text(f"TRUNCATE TABLE {targets} RESTART IDENTITY"))
    db.commit()


def purge_files() -> list[str]:
    """Removes the on-disk bytes whose metadata rows just disappeared.

    Order attachments and spooled print payloads are addressed only by their
    table rows, so once those are gone nothing can read or reclaim them.
    Contents only — the directories are mount points.
    """
    report = []
    for label, raw in (
        ("anexos", config.ATTACHMENTS_DIR),
        ("spool de impresión", config.PRINT_SPOOL_DIR),
    ):
        root = Path(raw)
        if not root.is_dir():
            report.append(f"  = {label}: {root} no existe, nada que borrar")
            continue
        removed = freed = 0
        for child in root.iterdir():
            size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            if child.is_dir():
                shutil.rmtree(child)
            else:
                size = child.stat().st_size
                child.unlink()
            removed += 1
            freed += size
        report.append(
            f"  - {label}: {removed} entrada(s), {freed / 1e6:.1f} MB ({root})"
        )
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _survives_the_container(path: Path) -> bool:
    """True when ``path`` is outside a container, or inside a mounted directory.

    A backup written to the container's own writable layer dies with a ``run
    --rm``. A bind mount or a volume outlives it — which is the difference
    between the development stack (the repo is mounted at /src) and production
    (where the answer is to mount the host's backup directory). Reading the
    mount table answers that exactly, instead of guessing from the environment.
    """
    if not Path("/.dockerenv").exists():
        return True
    try:
        mounts = {
            line.split(" ")[4]
            for line in Path("/proc/self/mountinfo").read_text().splitlines()
        }
    except OSError:
        return False
    # "/" is always mounted and is precisely the ephemeral layer, so it proves
    # nothing: only a mount BELOW it means the bytes outlive the container.
    mounts.discard("/")
    resolved = path.resolve()
    return any(str(p) in mounts for p in (resolved, *resolved.parents))


def write_backup(db, preserved: list[str], backup_dir: str, db_name: str) -> Path:
    """Writes the backup and says how to get it off a container.

    Run inside the api container the file lands on its writable layer, which the
    next `docker compose up` throws away — so the reminder is part of the step,
    not a footnote.
    """
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    path = dump_preserved(
        db, preserved, Path(backup_dir) / f"reset-{db_name}-{stamp}.json"
    )
    print(f"\n📦 Respaldo de la configuración preservada: {path}")
    print(
        "   No sustituye al respaldo completo: eso es scripts/backup.sh de "
        "opticutter-infra (pg_dump + tarball de uploads)."
    )
    if not _survives_the_container(path):
        print(
            "   ⚠️  Estás dentro de un contenedor: con `run --rm` el archivo se va "
            "con él.\n   Escríbelo en el host montando el directorio de respaldos:\n"
            "   docker compose run --rm -v /opt/opticutter/backups:/backup api \\\n"
            "     python scripts/reset_data.py --backup-dir /backup"
        )
    return path


def confirm(db_name: str, wiped: list[str], counts: dict[str, int]) -> bool:
    """Asks for a token that is different in every environment.

    Both the development and the production database are called ``cutter_db``, so
    typing the name alone proves nothing about which one is on the other end.
    The host does distinguish them (``localhost``/``postgres`` vs the compose
    service ``db``), so the token is ``base@host``.
    """
    total = sum(counts.values())
    token = f"{db_name}@{engine.url.host}"
    print(
        f"\n⚠️  Entorno: {config.ENVIRONMENT} · Base: {db_name} · "
        f"Servidor: {engine.url.host}:{engine.url.port}"
    )
    if config.ENVIRONMENT == "production":
        print(
            "   🚨 PRODUCCIÓN. Antes de seguir debe existir un respaldo completo: "
            "./scripts/backup.sh en /opt/opticutter (opticutter-infra)."
        )
    print(
        f"   Se van a borrar {total} fila(s) de {len(wiped)} tabla(s). "
        "Esto es IRREVERSIBLE."
    )
    return input(f"Escribe '{token}' para confirmar: ").strip() == token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep", default="", help="tablas extra a preservar, separadas por coma"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="solo muestra el plan y los conteos"
    )
    parser.add_argument("--yes", action="store_true", help="no pedir confirmación")
    parser.add_argument(
        "--keep-files", action="store_true", help="no borrar anexos ni spool"
    )
    parser.add_argument("--backup-dir", default="backups")
    parser.add_argument(
        "--backup-only", action="store_true", help="solo escribe el respaldo y sale"
    )
    parser.add_argument(
        "--restore", metavar="ARCHIVO", help="restaura usuarios desde un respaldo JSON"
    )
    args = parser.parse_args()

    db_name = engine.url.database
    db = SessionLocal()
    try:
        if args.restore:
            print(f"Restaurando usuarios en '{db_name}' desde {args.restore}")
            restore_users(db, Path(args.restore))
            return 0

        extra = {t.strip() for t in args.keep.split(",") if t.strip()}
        preserved, wiped = plan_tables(extra)

        print(f"Base: {db_name}\n")
        print("Se PRESERVAN:")
        for t, n in row_counts(db, preserved).items():
            print(f"  = {t}: {n} fila(s)")

        counts = row_counts(db, wiped)
        print("\nSe VACÍAN (RESTART IDENTITY):")
        for t, n in counts.items():
            print(f"  - {t}: {n} fila(s)")

        if args.dry_run:
            print("\n(--dry-run: no se tocó nada)")
            return 0

        if args.backup_only:
            write_backup(db, preserved, args.backup_dir, db_name)
            return 0

        if not args.yes and not confirm(db_name, wiped, counts):
            print("Cancelado: no se tocó nada.")
            return 1

        write_backup(db, preserved, args.backup_dir, db_name)

        wipe(db, wiped)
        print(f"🧹 {len(wiped)} tabla(s) vaciadas y secuencias reiniciadas.")

        # Files go after the commit: unlike the truncate, this one cannot be
        # rolled back, so it only runs once the database change is durable.
        if not args.keep_files:
            print("\n🗑  Archivos en disco:")
            for line in purge_files():
                print(line)

        print(
            "\n✅ Listo. Próximos pasos: volver a sincronizar el catálogo y los "
            "clientes desde SIFAC (POST /products/sync y POST /clients/sync).\n"
            "   El caché de optimize se llavea por hash de entradas, así que no "
            "queda inconsistente; si lo quieres limpio: make redis-flush."
        )
        return 0
    except Abort as exc:
        print(f"\n❌ {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
