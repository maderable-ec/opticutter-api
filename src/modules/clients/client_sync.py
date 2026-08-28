"""Syncs the client list against the vendor's external system (SIFAC).

Mirrors ``products/catalog_sync.py`` but is much shorter, because two of that
sync's hardest parts don't exist here:

- **No reconciliation.** A client who stops appearing in SIFAC is not deleted or
  deactivated: ``clients`` has no ``is_active`` column and is referenced by
  orders, pre-orders and drafts. This pass only creates and updates.
- **No destination conflicts.** ``identifier`` is both the unique key and the
  match key, so a row can't collide with a hand-created client the way a
  product's name or code could — it either matches one or creates one.

What it does own is the vendor's data quality: SIFAC keeps one free-text name
per client, phones written however they were typed, and a handful of cédulas
that don't check out. Each of those has a rule below, and every row it can't
use is reported with its cédula and name so someone can fix it at the source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from src.modules.clients.external_clients import ClientSourceRow, fetch_rows
from src.modules.clients.model import ClientModel
from src.modules.clients.schemas import ClientSyncIssue, ClientSyncResult
from src.modules.clients.tax_id import is_company_ruc, tax_id_error
from src.shared.exceptions import BulkValidationError

logger = logging.getLogger(__name__)

# Marks a client this sync created. The column is the same free-text `source`
# the dashboard fills with "dashboard", so this needs no migration — and it is
# written on create only, never on update, so a client somebody registered by
# hand keeps saying so even once SIFAC starts bringing them too.
SYNC_SOURCE = "sifac"

# Column widths of `clients` (see model.py). A name longer than this is trimmed
# with a warning rather than skipped: a too-long company name is still a
# client, and today no row in SIFAC comes close (longest measured: 60).
_NAME_MAX = 64
_PHONE_MAX = 32
_EMAIL_MAX = 128
_IDENTIFIER_MAX = 32

# Below this many digits it isn't a phone number, it's a placeholder — SIFAC
# holds "N/A" among others. Storing it would be worse than storing nothing:
# `clients.require_phone` only checks for a non-blank value, so "N/A" would
# sail through the gate that exists to stop a proforma without a real contact.
_PHONE_MIN_DIGITS = 7


@dataclass
class _ValidRow:
    row_no: int
    cedula: str
    first_name: str
    last_name: str
    phone: str
    email: str


@dataclass
class _Issue:
    """Something to report about a source row, and why.

    Used for both severities: a row that couldn't be imported (``issues``) and
    one that imported with a field dropped (``warnings``). The severity is the
    list it lands in, not a field.

    Carries the vendor's cédula and name, because fixing it means finding that
    client in SIFAC — a row number would be useless there.
    """

    codigo: str
    name: str
    message: str

    def to_schema(self) -> ClientSyncIssue:
        return ClientSyncIssue(code=self.codigo, name=self.name, message=self.message)


def split_name(nombre: str, cedula: str) -> Tuple[str, str]:
    """Splits SIFAC's single ``nom`` column into ``(first_name, last_name)``.

    A company keeps its name whole — an organization has no surname, and the
    only thing separating it from a person is the RUC's third digit (a natural
    person's RUC is 13 digits too, so length says nothing).

    For a person the Ecuadorian convention holds: names first, then the two
    surnames. Measured over the 798 live rows, 410 have two words and 290 have
    four, which is exactly the shape this splits correctly.
    """
    words = nombre.split()
    if not words:
        return "", ""
    if is_company_ruc(cedula) or len(words) == 1:
        return " ".join(words), ""
    if len(words) >= 4:
        return " ".join(words[:-2]), " ".join(words[-2:])
    if len(words) == 3:
        return words[0], " ".join(words[1:])
    return words[0], words[1]


def normalize_phone(telefono: str, telefono2: str) -> Optional[str]:
    """The client's phone as digits, or ``None`` if the source has no usable one.

    ``tel2`` is a fallback, not a second number: the column is populated on a
    single row today and never without ``tel``. Separators are dropped because
    SIFAC holds the same number written half a dozen ways ("099 109 5477",
    "0994616578") and a phone that differs only in spacing reads as a change on
    every re-sync.
    """
    for candidate in (telefono, telefono2):
        digits = "".join(c for c in candidate if c.isdigit())
        if len(digits) >= _PHONE_MIN_DIGITS:
            return digits[:_PHONE_MAX]
    return None


def normalize_email(email: str) -> Optional[str]:
    """The client's e-mail, lowercased, or ``None`` if it isn't one.

    Three live rows hold an address with the ``@`` missing
    (``nelusata04gmail.com``). Storing that is storing a wrong e-mail, which is
    worse than an empty field — nobody chases a blank, but a plausible-looking
    address gets used.
    """
    value = email.strip().lower()
    if not value:
        return None
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        return None
    return value[:_EMAIL_MAX]


def _validate(
    rows: Sequence[ClientSourceRow],
) -> Tuple[List[_ValidRow], List[_Issue], List[_Issue]]:
    """Turns source rows into importable ones, plus what to report about them.

    Returns ``(valid, issues, warnings)``: an issue means the row was skipped, a
    warning means it imported with something dropped.
    """
    valid: List[_ValidRow] = []
    issues: List[_Issue] = []
    warnings: List[_Issue] = []
    seen: Dict[str, int] = {}

    for row in rows:
        cedula = row.cedula.strip()
        nombre = " ".join(row.nombre.split())

        def problem(message: str) -> None:
            issues.append(
                _Issue(codigo=cedula or "(sin cédula)", name=nombre, message=message)
            )

        def warn(message: str) -> None:
            warnings.append(_Issue(codigo=cedula, name=nombre, message=message))

        if not cedula:
            problem("La fila no tiene cédula/RUC")
            continue
        if len(cedula) > _IDENTIFIER_MAX:
            problem(f"La cédula/RUC tiene más de {_IDENTIFIER_MAX} caracteres")
            continue
        if not nombre:
            problem("La fila no tiene nombre")
            continue

        # `ced` is UNIQUE in `mcliente` and no duplicate exists today, but the
        # upsert keys on it: two rows sharing one would silently overwrite each
        # other, and the second would look like an update nobody made.
        if cedula in seen:
            problem(f"La cédula '{cedula}' ya vino en la fila {seen[cedula]}")
            continue

        error = tax_id_error(cedula)
        if error:
            problem(error)
            continue

        first_name, last_name = split_name(nombre, cedula)
        if len(first_name) > _NAME_MAX or len(last_name) > _NAME_MAX:
            warn(
                f"El nombre supera los {_NAME_MAX} caracteres y se recortó; "
                "revísalo en el sistema externo"
            )
            first_name = first_name[:_NAME_MAX]
            last_name = last_name[:_NAME_MAX]

        phone = normalize_phone(row.telefono, row.telefono2)
        if not phone and (row.telefono.strip() or row.telefono2.strip()):
            warn(
                f"El teléfono '{row.telefono or row.telefono2}' no es un número; "
                "el cliente se importó sin teléfono"
            )

        email = normalize_email(row.email)
        if not email and row.email.strip():
            warn(
                f"El correo '{row.email}' no es una dirección válida; "
                "el cliente se importó sin correo"
            )

        seen[cedula] = row.row_no
        valid.append(
            _ValidRow(
                row_no=row.row_no,
                cedula=cedula,
                first_name=first_name,
                last_name=last_name,
                phone=phone or "",
                email=email or "",
            )
        )

    return valid, issues, warnings


def _apply(
    db: Session,
    valid_rows: Sequence[_ValidRow],
    skipped_inactive: int,
    issues: Sequence[_Issue],
    warnings: Sequence[_Issue],
    *,
    dry_run: bool,
) -> ClientSyncResult:
    """Upserts the validated rows, matched on ``identifier``.

    The whole client table is preloaded into one dict and written back in a
    single commit. Deliberately *not* through ``ClientService``: ``CRUDService.
    _persist`` commits per row, and this writes ~800 of them.

    On update the source only overwrites a field it actually has a value for.
    SIFAC is the system of record, but 150 of its clients have no e-mail and 67
    no phone — mirroring those blanks would delete contact details a seller
    typed into the dashboard, which is the one thing this sync must not do.

    ``dry_run`` runs the identical pass and rolls back, so the preview can't
    drift from what applying it will do.
    """
    existing: Dict[str, ClientModel] = {
        client.identifier: client for client in db.query(ClientModel).all()
    }

    created = 0
    updated = 0
    for row in valid_rows:
        client = existing.get(row.cedula)
        if client is None:
            db.add(
                ClientModel(
                    identifier=row.cedula,
                    first_name=row.first_name or None,
                    last_name=row.last_name or None,
                    phone=row.phone or None,
                    email=row.email or None,
                    source=SYNC_SOURCE,
                )
            )
            created += 1
            continue

        changed = False
        for field, value in (
            ("first_name", row.first_name),
            ("last_name", row.last_name),
            ("phone", row.phone),
            ("email", row.email),
        ):
            if value and getattr(client, field) != value:
                setattr(client, field, value)
                changed = True
        # `source` is never touched on update: it records where the client came
        # from originally, and a hand-registered client doesn't become a synced
        # one just because SIFAC now carries them too.
        if changed:
            updated += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()

    logger.info(
        "Client sync: created=%d updated=%d skipped_invalid=%d dry_run=%s",
        created,
        updated,
        len(issues),
        dry_run,
    )

    return ClientSyncResult(
        created=created,
        updated=updated,
        skipped_invalid=len(issues),
        skipped_inactive=skipped_inactive,
        issues=[issue.to_schema() for issue in issues],
        warnings=[warning.to_schema() for warning in warnings],
        dry_run=dry_run,
    )


FetchRows = Callable[[], Tuple[List[ClientSourceRow], List[ClientSourceRow]]]


def sync_clients_from_external(
    db: Session,
    *,
    dry_run: bool = False,
    fetch: FetchRows = fetch_rows,
) -> ClientSyncResult:
    """Syncs the client list against the vendor's external system.

    ``fetch`` is injectable so tests drive the whole pipeline without a live
    MySQL; in production it reads ``mcliente`` (see ``external_clients.py``).
    """
    active_rows, retired_rows = fetch()

    if not active_rows:
        # Nothing is deleted here, so an empty read can't do damage — but it is
        # never legitimate either: it means a broken filter, an empty table or
        # the wrong database, and reporting "0 clientes" as a success would
        # hide that.
        raise BulkValidationError(
            "El sistema externo no devolvió ningún cliente activo; "
            "no se aplicaron cambios",
            errors=[
                {
                    "field": None,
                    "message": (
                        "La consulta al sistema externo devolvió 0 clientes "
                        "activos. Se abortó la sincronización."
                    ),
                }
            ],
        )

    valid_rows, issues, warnings = _validate(active_rows)
    return _apply(
        db,
        valid_rows,
        len(retired_rows),
        issues,
        warnings,
        dry_run=dry_run,
    )
