"""Read-only access to the vendor's per-warehouse stock (SIFAC ``binventario``).

This module is the *only* place that knows that schema: it owns the query and
the column mapping, and hands the service a list of ``StockRow`` — everything
downstream joins on ``external_code`` and never sees a vendor column again.

The connection itself lives in ``src/shared/external_db.py``, shared with the
catalog and client readers: same server, one more table.

**The join is the whole point of this file.** ``marticulo`` carries two
identifiers and they are not interchangeable: ``cod`` is SIFAC's internal
autoincrement, which is what ``binventario.art`` points at, while ``cin`` is the
code the vendor prints as CODIGO and therefore the one this catalog stores in
``products.external_code`` (see ``external_catalog.py`` for why keying on ``cod``
would recreate the whole catalog under wrong codes). ``binventario`` has no
``cin`` at all, so somebody has to translate, and the only table that can is
``marticulo``. Doing it *inside this query* is what keeps ``cod`` out of our
database entirely: no column to add, no backfill, and nothing to keep in sync —
if the vendor renumbers an article the next read already tells the truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.modules.products.catalog_sync import build_external_code
from src.shared.exceptions import ExternalServiceError
from src.shared.external_db import ExternalMySQLSource, is_retired

logger = logging.getLogger(__name__)

# The category filter is the SAME literal as ``external_catalog._QUERY``, on
# purpose: stock we could never match to a product is noise, and the two lists
# have to describe the same universe or the report grows rows the catalog has
# never heard of.
_QUERY = text(
    """
    SELECT a.cat AS categoria, a.cin AS codigo, a.est AS est, a.FecEli AS fec_eli,
           i.bod AS bodega, i.can AS cantidad
    FROM binventario i
    JOIN marticulo a ON a.cod = i.art
    WHERE a.cat = 'TABLEROS'
       OR (a.cat = 'TAPACANTOS' AND a.tip = 'TAPACANTOS')
    ORDER BY a.cat, a.cin
    """
)


@dataclass(frozen=True)
class StockRow:
    """One (warehouse, article) quantity, already keyed the way we store codes."""

    external_code: str
    warehouse_code: int
    quantity: float


class ExternalInventorySource(ExternalMySQLSource):
    """Reader over the vendor's ``binventario`` table."""

    def fetch_stock(self) -> List[StockRow]:
        """Reads every stocked quantity of both categories, all warehouses.

        Retired articles are dropped here rather than in the ``WHERE``, matching
        the other two readers — and it is not cosmetic: ``cin`` is unique only
        among the live rows (the live catalog has one duplicate, two
        ``TAPACANTOS:57`` articles the vendor took out of service), so without
        this filter two rows would claim the same ``external_code`` and the last
        one read would win at random.

        A row whose quantity does not parse is skipped rather than fatal: this
        feeds an informational alert, and one unreadable article must not cost
        the whole report.
        """
        try:
            with self.engine.connect() as conn:
                records = conn.execute(_QUERY).mappings().all()
        except SQLAlchemyError as exc:
            logger.warning("External inventory read failed: %s", exc)
            raise ExternalServiceError(
                "No se pudo leer el inventario externo. Verifica la conexión "
                "e inténtalo de nuevo."
            ) from exc

        rows: List[StockRow] = []
        retired = 0
        for record in records:
            if is_retired(record["est"], record["fec_eli"]):
                retired += 1
                continue
            try:
                warehouse = int(record["bodega"])
                quantity = float(record["cantidad"])
            except (TypeError, ValueError):
                logger.warning(
                    "Unreadable stock row for %s: bod=%r can=%r",
                    record["codigo"],
                    record["bodega"],
                    record["cantidad"],
                )
                continue
            rows.append(
                StockRow(
                    external_code=build_external_code(
                        str(record["categoria"]).strip().upper(),
                        str(record["codigo"]).strip(),
                    ),
                    warehouse_code=warehouse,
                    quantity=quantity,
                )
            )

        logger.info(
            "External inventory read: %d rows, %d retired skipped", len(rows), retired
        )
        return rows


# Shared instance; the service imports ``fetch_stock`` and uses it directly.
external_inventory = ExternalInventorySource()


def fetch_stock() -> List[StockRow]:
    """Module-level entry point (see ``ExternalInventorySource.fetch_stock``)."""
    return external_inventory.fetch_stock()
