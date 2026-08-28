"""Read-only access to the external inventory MySQL (SIFAC) that feeds the
client list (see ``client_sync.py``).

This module is the *only* place that knows the vendor's ``mcliente`` schema: it
owns the query and the column mapping, and hands the sync a list of
``ClientSourceRow`` — plain strings, one per client. Everything downstream
(validation, name splitting, upsert) is source-agnostic.

The connection lives in ``src/shared/external_db.py``, shared with the catalog
reader: same server and same database (``mcliente`` and ``marticulo`` are
siblings), so there is one credential and one URL for both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Tuple

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.shared.exceptions import ExternalServiceError
from src.shared.external_db import ExternalMySQLSource, is_retired, text_value

logger = logging.getLogger(__name__)

# `ced` is the vendor's cédula/RUC column and is UNIQUE in `mcliente`, which is
# what lets the sync key on it directly against `clients.identifier` — no
# `external_code` marker column, and no migration.
#
# `est` is filtered in Python rather than in the WHERE clause: the retired rows
# are counted into the report (`skippedInactive`) so a shrinking import has an
# explanation instead of silently reading fewer rows. `mcliente` has no
# `FecEli`, so `est` alone decides.
_QUERY = text(
    """
    SELECT ced, nom, mai, tel, tel2, est
    FROM mcliente
    ORDER BY cod
    """
)


@dataclass
class ClientSourceRow:
    """One client from the external system, as text.

    Every field is a string for the same reason the catalog's ``SourceRow`` is:
    the validation layer parses and normalizes free text (a phone written with
    spaces, an e-mail missing its ``@``) and should never have to care whether
    the driver handed back an ``int``, a ``Decimal`` or ``None``.
    """

    row_no: int
    cedula: str
    nombre: str
    email: str
    telefono: str
    telefono2: str


class ExternalClientsSource(ExternalMySQLSource):
    """Reader over the vendor's ``mcliente`` table."""

    def fetch_rows(self) -> Tuple[List[ClientSourceRow], List[ClientSourceRow]]:
        """Reads the client list, split into ``(active, retired)``.

        Retired rows are returned whole but deliberately *not* validated by the
        caller: nothing is done with them beyond counting, and running one
        through the cédula check would report problems about a client nobody
        bills any more.

        The table is small (~800 rows), so this is a single unpaginated read.
        """
        try:
            with self.engine.connect() as conn:
                records = conn.execute(_QUERY).mappings().all()
        except SQLAlchemyError as exc:
            logger.warning("External clients read failed: %s", exc)
            raise ExternalServiceError(
                "No se pudo leer el listado de clientes externo. Verifica la "
                "conexión e inténtalo de nuevo."
            ) from exc

        active: List[ClientSourceRow] = []
        retired: List[ClientSourceRow] = []
        for position, record in enumerate(records, start=1):
            row = ClientSourceRow(
                row_no=position,
                cedula=text_value(record["ced"]),
                nombre=text_value(record["nom"]),
                email=text_value(record["mai"]),
                telefono=text_value(record["tel"]),
                telefono2=text_value(record["tel2"]),
            )
            bucket = retired if is_retired(record["est"]) else active
            bucket.append(row)

        logger.info(
            "External clients read: %d active, %d retired", len(active), len(retired)
        )
        return active, retired


# Shared instance; the sync imports ``fetch_rows`` and uses it directly.
external_clients = ExternalClientsSource()


def fetch_rows() -> Tuple[List[ClientSourceRow], List[ClientSourceRow]]:
    """Module-level entry point (see ``ExternalClientsSource.fetch_rows``)."""
    return external_clients.fetch_rows()
