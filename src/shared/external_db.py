"""Shared plumbing for reading the vendor's external MySQL (SIFAC).

Two feature slices read that database — the product catalog
(``src/modules/products/external_catalog.py``) and the client list
(``src/modules/clients/external_clients.py``) — and they read *different
tables of the same server*. What they share lives here: the credential guard,
the lazily-built engine and the text coercion every reader needs.

Each module still owns its own query and column mapping: this file knows how to
*connect*, never what the vendor's schema means.

Unlike ``shared/cache.py``, whose lazy-client shape this borrows, **this source
must never degrade silently**. The cache is an accelerator, so a dead Redis just
means recomputing; here an empty read is indistinguishable from "the vendor
deleted everything", and a sync that believes it would wipe the destination.
Every failure is raised as ``ExternalServiceError`` instead.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from src.shared.config import config
from src.shared.exceptions import ExternalServiceError


def text_value(value: Any) -> str:
    """Coerces a column to the trimmed text the validators expect.

    ``NULL`` becomes ``""``, never ``"None"``: a blank reads as a missing field
    and gets a sensible message, while the literal string would surface as a
    baffling ``P.venta 'None' no es un número válido``.
    """
    if value is None:
        return ""
    return str(value).strip()


def is_retired(est: Any, fec_eli: Any = None) -> bool:
    """Whether the vendor has taken this record out of service.

    ``est`` is the status flag (1 = active, the column default) and ``FecEli``
    the deletion date, which only some tables carry — ``mcliente`` has no such
    column, so it passes ``est`` alone. Reading them makes the retirement
    *explicit*: a sync that could only infer it from a row's absence conflates
    "the vendor retired it" with "our query was wrong".

    A NULL ``est`` counts as active — 1 is the column default, so NULL carries
    no retirement intent and the safe reading is the one that doesn't remove a
    record.
    """
    if fec_eli is not None:
        return True
    if est is None:
        return False
    try:
        return int(est) != 1
    except (TypeError, ValueError):
        return False


def check_external_url(raw: str) -> None:
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


class ExternalMySQLSource:
    """Lazily-connected reader over the vendor's database.

    Subclasses add the query and the column mapping; this holds the connection.
    The engine can be injected (``Source(engine=...)``) so tests run against a
    stub without a live MySQL.

    Both readers point at the same server, so they share
    ``EXTERNAL_CATALOG_URL``: the name is catalog-era, but the credential and
    the database behind it are the vendor's whole SIFAC instance.
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
            check_external_url(config.EXTERNAL_CATALOG_URL)
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
