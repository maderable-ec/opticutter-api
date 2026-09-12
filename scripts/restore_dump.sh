#!/usr/bin/env bash
#
# Load a pg_dump file into the LOCAL development database, replacing it.
#
#   scripts/restore_dump.sh <archivo.dump> [opciones]
#
# This is the local twin of opticutter-infra's scripts/restore.sh, and the two
# differ deliberately in one place: infra restores over the LIVE database with
# `pg_restore --clean --if-exists`, which only drops what the dump itself
# mentions. Here the database is DROPped and recreated, because a development
# checkout routinely carries schema the dump does not know about (a migration
# from a branch, a table from a stash) and `--clean` would leave those tables
# behind, half-populated and invisible until something queries them.
#
# DESTRUCTIVE: the target database is replaced. A pg_dump of the current one is
# written to backups/ first unless --no-backup is passed.
#
# Both pg_dump formats are accepted. A custom-format dump (what infra's
# backup.sh writes) is restored with --no-owner, so the server's roles need not
# exist here; a plain .sql one is replayed verbatim, so dump it with --no-owner
# yourself or every GRANT fails.
#
# The uploads volume is the other half of the data. A database restored without
# the matching tarball leaves order_attachments rows pointing at files that do
# not exist, so --uploads takes the tarball produced by infra's backup.sh.
#
#   scripts/restore_dump.sh db_20260912_081501.dump
#   scripts/restore_dump.sh db_*.dump --uploads uploads_*.tar.gz
#   scripts/restore_dump.sh db_*.dump --db cutter_scratch --no-backup
#
set -euo pipefail

DB_FILE=""
UPLOADS_FILE=""
TARGET_DB="${TARGET_DB:-cutter_db}"
DO_BACKUP=1
ASSUME_YES=0

usage() {
    cat >&2 <<'USAGE'
Uso: scripts/restore_dump.sh <archivo.dump> [opciones]

  --uploads <tar.gz>  Restaura también los adjuntos en el volumen de uploads
  --db <nombre>       Base destino (por defecto: cutter_db)
  --no-backup         Omite el pg_dump de respaldo de la base actual
  -y, --yes           No pide confirmación
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --uploads)   UPLOADS_FILE="${2:-}"; shift 2 ;;
        --db)        TARGET_DB="${2:-}"; shift 2 ;;
        --no-backup) DO_BACKUP=0; shift ;;
        -y|--yes)    ASSUME_YES=1; shift ;;
        -h|--help)   usage ;;
        -*)          echo "ERROR: opción desconocida: $1" >&2; usage ;;
        *)           [ -z "$DB_FILE" ] || { echo "ERROR: solo se acepta un archivo .dump" >&2; usage; }
                     DB_FILE="$1"; shift ;;
    esac
done

[ -n "$DB_FILE" ] || usage
[ -f "$DB_FILE" ] || { echo "ERROR: no existe ${DB_FILE}" >&2; exit 2; }
[ -z "$UPLOADS_FILE" ] || [ -f "$UPLOADS_FILE" ] || { echo "ERROR: no existe ${UPLOADS_FILE}" >&2; exit 2; }

# Resolve the dump to an absolute path BEFORE cd'ing to the repo root, so a
# relative argument keeps meaning what the caller meant.
DB_FILE="$(cd "$(dirname "$DB_FILE")" && pwd)/$(basename "$DB_FILE")"
if [ -n "$UPLOADS_FILE" ]; then
    UPLOADS_FILE="$(cd "$(dirname "$UPLOADS_FILE")" && pwd)/$(basename "$UPLOADS_FILE")"
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
# docker compose names volumes after the project, which defaults to the
# directory name (opticutter-api_uploads_data).
UPLOADS_VOLUME="${UPLOADS_VOLUME:-$(basename "$APP_DIR")_uploads_data}"
PSQL_USER="${POSTGRES_USER:-cutter}"

# Pick the restore tool from the file's own header rather than from its
# extension: a custom-format dump needs pg_restore and a plain one needs psql,
# and getting it wrong fails with an unhelpful syntax error a hundred lines in.
MAGIC="$(head -c 5 "$DB_FILE" 2>/dev/null || true)"
if [ "$MAGIC" = "PGDMP" ]; then
    DUMP_FORMAT="custom"
elif [ "$(head -c 2 "$DB_FILE" | od -An -tx1 | tr -d ' \n')" = "1f8b" ]; then
    echo "ERROR: ${DB_FILE} está comprimido con gzip. Descomprímelo primero:" >&2
    echo "       gunzip -k $(basename "$DB_FILE")" >&2
    exit 2
else
    DUMP_FORMAT="plain"
fi

# `docker compose exec` attaches the caller's stdin, so every invocation that
# is not being fed the dump has to be closed off with </dev/null. Without it
# the health check below eats the answer to the confirmation prompt, `read`
# gets EOF and `set -e` kills the script with no message at all.
echo ">> Levantando postgres"
docker compose up -d postgres >/dev/null </dev/null
until docker compose exec -T postgres pg_isready -U "$PSQL_USER" >/dev/null 2>&1 </dev/null; do sleep 1; done

psql_db() { docker compose exec -T postgres psql -U "$PSQL_USER" -d "$1" "${@:2}" </dev/null; }

CURRENT_SIZE="$(psql_db postgres -tAc \
    "SELECT pg_size_pretty(pg_database_size('${TARGET_DB}'))" 2>/dev/null || echo "no existe")"

echo
echo "Se va a REEMPLAZAR la base local:"
echo "  destino:  ${TARGET_DB} (contenido actual: ${CURRENT_SIZE})"
echo "  dump:     ${DB_FILE} (${DUMP_FORMAT})"
if [ -n "$UPLOADS_FILE" ]; then
    echo "  uploads:  ${UPLOADS_FILE} -> volumen ${UPLOADS_VOLUME}"
else
    echo "  uploads:  sin tocar (los adjuntos quedarán como están)"
fi
if [ "$DO_BACKUP" -eq 1 ]; then
    echo "  respaldo: backups/ (antes de borrar)"
else
    echo "  respaldo: OMITIDO (--no-backup)"
fi
echo
if [ "$ASSUME_YES" -eq 0 ]; then
    read -r -p "Escribe 'yes' para continuar: " CONFIRM
    [ "$CONFIRM" = "yes" ] || { echo "Cancelado."; exit 1; }
fi

if [ "$DO_BACKUP" -eq 1 ] && [ "$CURRENT_SIZE" != "no existe" ]; then
    mkdir -p backups
    BACKUP_FILE="backups/pre-restore-${TARGET_DB}-$(date +%Y%m%d-%H%M%S).dump"
    echo ">> Respaldando ${TARGET_DB} en ${BACKUP_FILE}"
    docker compose exec -T postgres pg_dump -U "$PSQL_USER" -d "$TARGET_DB" -Fc </dev/null > "$BACKUP_FILE"
fi

# WITH (FORCE) terminates the open connections as part of the DROP (PG 13+).
# Terminating them in a separate statement leaves a race the API reconnects
# through — with make dev running, the DROP loses it about half the time.
echo ">> Recreando ${TARGET_DB}"
psql_db postgres -q -c "DROP DATABASE IF EXISTS ${TARGET_DB} WITH (FORCE);" \
                 -c "CREATE DATABASE ${TARGET_DB} OWNER ${PSQL_USER};"

echo ">> Restaurando el dump"
RESTORE_LOG="$(mktemp -t restore_dump)"
trap 'rm -f "$RESTORE_LOG"' EXIT
if [ "$DUMP_FORMAT" = "custom" ]; then
    # --no-owner/--no-privileges: the dump carries the server's roles, which do
    # not exist locally, and every GRANT would fail one by one.
    docker compose exec -T postgres pg_restore -U "$PSQL_USER" -d "$TARGET_DB" \
        --no-owner --no-privileges < "$DB_FILE" > "$RESTORE_LOG" 2>&1 || true
else
    docker compose exec -T postgres psql -U "$PSQL_USER" -d "$TARGET_DB" -q \
        -v ON_ERROR_STOP=1 < "$DB_FILE" > "$RESTORE_LOG" 2>&1 || true
fi
if grep -qiE '^(pg_restore: )?error' "$RESTORE_LOG"; then
    echo "ERROR: la restauración reportó errores:" >&2
    grep -iE '^(pg_restore: )?error' "$RESTORE_LOG" | head -20 >&2
    exit 1
fi

if [ -n "$UPLOADS_FILE" ]; then
    echo ">> Restaurando los adjuntos"
    UPLOADS_DIR="$(dirname "$UPLOADS_FILE")"
    UPLOADS_BASE="$(basename "$UPLOADS_FILE")"
    docker volume create "$UPLOADS_VOLUME" >/dev/null </dev/null
    # uid 1000 is `appuser` in the API image, which owns /src/uploads.
    docker run --rm \
        -v "${UPLOADS_VOLUME}:/data" \
        -v "${UPLOADS_DIR}:/backup:ro" \
        alpine:3 sh -c "find /data -mindepth 1 -delete && tar xzf '/backup/${UPLOADS_BASE}' -C /data && chown -R 1000:1000 /data" </dev/null
fi

echo
echo ">> Listo. Estado de ${TARGET_DB}:"
psql_db "$TARGET_DB" -c "
SELECT pg_size_pretty(pg_database_size('${TARGET_DB}')) AS tamano,
       (SELECT count(*) FROM pg_stat_user_tables) AS tablas,
       (SELECT version_num FROM alembic_version) AS migracion;"

# Revision ids in this repo are zero-padded and sequential (000000000001,
# 000000000002, ...), so the newest file sorts last and a plain string compare
# is enough to tell whether the checkout has migrations the dump never saw.
DB_REV="$(psql_db "$TARGET_DB" -tAc "SELECT version_num FROM alembic_version" 2>/dev/null || true)"
HEAD_REV="$(ls alembic/versions/ 2>/dev/null | grep -oE '^[0-9]+' | sort | tail -1 || true)"
if [ -n "$DB_REV" ] && [ -n "$HEAD_REV" ]; then
    if [ "$DB_REV" \< "$HEAD_REV" ]; then
        echo "   ⚠️  El checkout tiene migraciones posteriores (${DB_REV} -> ${HEAD_REV}): corre 'make upgrade'."
    elif [ "$DB_REV" \> "$HEAD_REV" ]; then
        echo "   ⚠️  El dump viene de un esquema MÁS NUEVO que este checkout (${DB_REV} > ${HEAD_REV})."
    fi
fi
if [ -z "$UPLOADS_FILE" ] && [ "$(psql_db "$TARGET_DB" -tAc \
        "SELECT count(*) FROM order_attachments" 2>/dev/null || echo 0)" -gt 0 ]; then
    echo "   ⚠️  Hay adjuntos en la base y no se restauró el volumen: las descargas darán 404."
fi
echo "   Las credenciales son las del dump. Si tenías la api corriendo, reiníciala."
