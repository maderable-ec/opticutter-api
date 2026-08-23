"""Read-only access to the external inventory MySQL (SIFAC) that feeds the
product catalog (see ``catalog_sync.py``).

This module is the *only* place that knows the vendor's schema: it owns the
connection, the query and the column mapping, and hands the sync a list of
``SourceRow`` — plain strings, one per article. Everything downstream
(validation, upsert, reconciliation) is source-agnostic.

Unlike ``shared/cache.py``, whose lazy-client shape this borrows, **this source
must never degrade silently**. The cache is an accelerator, so a dead Redis just
means recomputing; here an empty read would look exactly like "the vendor
deleted their whole catalog" and the sync's reconciliation pass would wipe ours.
Every failure is raised as ``ExternalServiceError`` instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from src.shared.config import config
from src.shared.exceptions import ExternalServiceError

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


def _text(value: Any) -> str:
    """Coerces a column to the trimmed text the validator expects.

    ``NULL`` becomes ``""``, never ``"None"``: a blank reads as a missing field
    and gets a sensible message, while the literal string would surface as a
    baffling ``P.venta 'None' no es un número válido``.
    """
    if value is None:
        return ""
    return str(value).strip()


def _is_retired(est: Any, fec_eli: Any) -> bool:
    """Whether the vendor has taken this article out of service.

    ``est`` is the status flag (1 = active, the column default) and ``FecEli``
    the deletion date. Reading them makes the retirement *explicit*: the
    previous CSV-based sync could only infer it from a row's absence, which
    conflates "the vendor retired it" with "our query was wrong".

    A NULL ``est`` counts as active — 1 is the column default, so NULL carries
    no retirement intent and the safe reading is the one that doesn't remove a
    product.
    """
    if fec_eli is not None:
        return True
    if est is None:
        return False
    try:
        return int(est) != 1
    except (TypeError, ValueError):
        return False


def _check_url(raw: str) -> None:
    """Fails early, and quietly, on a URL whose credentials aren't encoded.

    SQLAlchemy splits the credentials off at the **first** ``@``, so a password
    containing one swallows the host: the driver then tries to resolve the rest
    of the password as a hostname and answers ``Name or service not known``,
    which points nowhere near the real problem.
    Worse, that message — and anything that logs it — ends up **containing the
    password**. Catching it here means the credential never reaches a log line
    or an HTTP response, so nothing below echoes the URL back.
    """
    try:
        url = make_url(raw)
    except Exception as exc:
        raise ExternalServiceError(
            "La URL del inventario externo está mal formada. Si la contraseña "
            "tiene caracteres especiales (@ : / ? #) hay que escribirlos "
            "percent-encodeados: '@' es '%40'."
        ) from exc

    if not url.host or any(c in url.host for c in "@/ "):
        raise ExternalServiceError(
            "La URL del inventario externo está mal formada: el host no se "
            "pudo separar de las credenciales. Casi siempre es una contraseña "
            "con caracteres especiales (@ : / ? #) sin percent-encodear: '@' "
            "se escribe '%40'."
        )


class ExternalCatalogSource:
    """Lazily-connected reader over the vendor's ``marticulo`` table.

    The engine can be injected (``ExternalCatalogSource(engine=...)``) so tests
    run against SQLite or a stub without a live MySQL.
    """

    def __init__(self, engine: Optional[Engine] = None):
        self._engine = engine
        self._initialized = engine is not None

    @property
    def engine(self) -> Engine:
        """Pooled engine, built on first use. Raises if unconfigured."""
        if not self._initialized:
            if not config.EXTERNAL_CATALOG_URL:
                raise ExternalServiceError(
                    "La conexión al inventario externo no está configurada "
                    "(EXTERNAL_CATALOG_URL)"
                )
            _check_url(config.EXTERNAL_CATALOG_URL)
            self._engine = create_engine(
                config.EXTERNAL_CATALOG_URL,
                pool_pre_ping=True,
                pool_recycle=config.DB_POOL_RECYCLE_SECONDS,
                connect_args={
                    "connect_timeout": (
                        config.EXTERNAL_CATALOG_CONNECT_TIMEOUT_SECONDS
                    ),
                    "read_timeout": config.EXTERNAL_CATALOG_READ_TIMEOUT_SECONDS,
                },
            )
            self._initialized = True
        return self._engine

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
                codigo=_text(record["cin"]),
                articulo=_text(record["nom"]),
                marca=_text(record["mar"]),
                tipo=_text(record["tip"]),
                categoria=_text(record["cat"]),
                grupo=_text(record["gru"]),
                iva=_text(record["iva"]),
                p_venta=_text(record["ven"]),
                obs=_text(record["obs"]),
            )
            bucket = retired if _is_retired(record["est"], record["FecEli"]) else active
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
