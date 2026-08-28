"""Read-only access to the external inventory MySQL (SIFAC) that feeds the
product catalog (see ``catalog_sync.py``).

This module is the *only* place that knows the vendor's ``marticulo`` schema:
it owns the query and the column mapping, and hands the sync a list of
``SourceRow`` — plain strings, one per article. Everything downstream
(validation, upsert, reconciliation) is source-agnostic.

The connection itself lives in ``src/shared/external_db.py``, shared with the
client reader: same server, different table.
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

# Boards are the whole TABLEROS category (every ``tip`` within it: MDP, PLYWOOD,
# MDF, ...). Edge bandings are narrower: the TAPACANTOS category holds a second
# ``tip`` that isn't edge banding at all and must stay out of the catalog.
#
# Both branches leave ``cat`` at exactly TABLEROS/TAPACANTOS, which is what
# keeps ``external_code`` ("{cat}:{cod}") identical to the codes the previous
# CSV-based sync stored — a re-sync updates those products instead of
# recreating them.
# The article code is `cin`, NOT the `cod` primary key. `cod` is an internal
# autoincrement; `cin` is the code the vendor's own reports print as CODIGO and
# therefore the one this catalog already stores in `external_code` from the era
# when it was synced from those reports. Measured against the last exports:
# every one of their 543 codes matches a `cin`, and matching by `cod` instead
# lines up 424 codes with **zero** matching article names — a re-sync keyed on
# `cod` would recreate the whole catalog under wrong codes and delete the
# originals. `cin` is populated on every row this query returns.
_QUERY = text(
    """
    SELECT cin, nom, mar, tip, cat, gru, iva, ven, obs, est, FecEli
    FROM marticulo
    WHERE cat = 'TABLEROS'
       OR (cat = 'TAPACANTOS' AND tip = 'TAPACANTOS')
    ORDER BY cat, cod
    """
)


@dataclass
class SourceRow:
    """One article from the external inventory, as text.

    Every field is a string because the validator was written against a CSV
    export of this same table and parses units, percentages and dimensions out
    of free text. Keeping that contract is what let the whole validation and
    upsert layer survive the source change untouched.
    """

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


class ExternalCatalogSource(ExternalMySQLSource):
    """Reader over the vendor's ``marticulo`` table."""

    def fetch_rows(self) -> Tuple[List[SourceRow], List[SourceRow]]:
        """Reads the catalog, split into ``(active, retired)``.

        Retired rows are returned whole but are deliberately *not* validated by
        the caller: taking a product out of service only needs its category and
        code, and running a retired row through the strict dimension parsing
        would let a malformed article nobody sells any more abort the entire
        sync.

        The table is small (~1.8k rows), so this is a single unpaginated read.
        """
        try:
            with self.engine.connect() as conn:
                records = conn.execute(_QUERY).mappings().all()
        except SQLAlchemyError as exc:
            logger.warning("External catalog read failed: %s", exc)
            raise ExternalServiceError(
                "No se pudo leer el inventario externo. Verifica la conexión "
                "e inténtalo de nuevo."
            ) from exc

        active: List[SourceRow] = []
        retired: List[SourceRow] = []
        for position, record in enumerate(records, start=1):
            row = SourceRow(
                row_no=position,
                codigo=text_value(record["cin"]),
                articulo=text_value(record["nom"]),
                marca=text_value(record["mar"]),
                tipo=text_value(record["tip"]),
                categoria=text_value(record["cat"]),
                grupo=text_value(record["gru"]),
                iva=text_value(record["iva"]),
                p_venta=text_value(record["ven"]),
                obs=text_value(record["obs"]),
            )
            bucket = retired if is_retired(record["est"], record["FecEli"]) else active
            bucket.append(row)

        logger.info(
            "External catalog read: %d active, %d retired", len(active), len(retired)
        )
        return active, retired


# Shared instance; the sync imports ``fetch_rows`` and uses it directly.
external_catalog = ExternalCatalogSource()


def fetch_rows() -> Tuple[List[SourceRow], List[SourceRow]]:
    """Module-level entry point (see ``ExternalCatalogSource.fetch_rows``)."""
    return external_catalog.fetch_rows()
