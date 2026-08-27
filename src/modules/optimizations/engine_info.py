"""What engine this process runs, rendered for a log line.

Exists because **nothing** used to make the answer observable. The packing
backend degrades to pure Python in total silence (``rust_backend._resolve``
checks the wheel before the environment variable, so a demanded ``rust`` with no
wheel is dropped without an error), CP-SAT degrades the same way, and production
runs at ``LOG_LEVEL=WARNING`` — where every ``INFO`` line in the codebase is
invisible. The result was a box that could be 3x slower than intended with no
way to tell from outside.

Deliberately a *rendering* module, not a source of truth: every value comes from
``src.cutting`` or ``config``. It lives under ``modules/`` rather than in the
domain package because it reads ``src.shared.config``, which ``src/cutting/``
must never import.
"""

import os
from importlib.metadata import PackageNotFoundError, version
from typing import Dict, Optional

from src.cutting import exact_available, rust_backend
from src.cutting.search import ENGINE_VERSION
from src.shared.config import config


def _ortools_version() -> Optional[str]:
    """Installed ``ortools`` version, or ``None`` when it isn't importable.

    Read from the distribution metadata rather than the package: ``ortools``
    exposes no reliable ``__version__``, and the pin is what we actually care
    about (a solver upgrade can return a different, still optimal, layout).
    """
    try:
        return version("ortools")
    except PackageNotFoundError:  # pragma: no cover - depends on the install
        return None


def engine_summary() -> str:
    """One line naming the backend, the solver and the budgets that shape both.

    Logged at WARNING on startup so it survives production's log level. Keep it
    to a single line: it is meant to be greppable (``grep motor:``), not pretty.
    """
    rust = rust_backend.status()
    wheel = rust["wheel_version"] or "?"
    packing = (
        f"{rust['effective']} (wheel {wheel}, pedido={rust['requested']})"
        if rust["wheel_importable"]
        else f"{rust['effective']} (sin wheel, pedido={rust['requested']})"
    )
    ortools = _ortools_version()
    cpsat = f"sí (ortools {ortools})" if exact_available() else "NO (sólo heurísticas)"
    return (
        f"motor: packing={packing} · CP-SAT={cpsat} · "
        f"ENGINE_VERSION={ENGINE_VERSION} · workers={config.OPT_POOL_WORKERS} · "
        f"tries={config.OPT_TRIES_PER_BOARD} iters={config.OPT_SEARCH_ITERATIONS} · "
        f"exact(max_pieces={config.OPT_EXACT_MAX_PIECES} "
        f"calls={config.OPT_EXACT_MAX_CALLS} "
        f"root={config.OPT_EXACT_ROOT_DETERMINISTIC_TIME}/"
        f"{config.OPT_EXACT_ROOT_PATIENCE})"
    )


def degraded_warning() -> Optional[str]:
    """The message for a demanded-but-missing Rust wheel; ``None`` when fine.

    Separated from ``engine_summary`` so the caller can raise the level: this is
    a misconfiguration that costs ~3x on the packing kernel, not an FYI.
    """
    if not rust_backend.degraded():
        return None
    return (
        "OPT_ENGINE_BACKEND=rust pero el wheel opticutter_core NO se pudo "
        "importar: el motor está corriendo la geometría interpretada (~3x más "
        "lenta). Revisar que la imagen incluya la etapa rustbuild."
    )


def backend_name() -> str:
    """``rust`` or ``python`` — the short form for the per-optimization log."""
    return "rust" if rust_backend.available() else "python"


def probe_worker() -> Dict[str, object]:
    """Reports the engine **as seen from a pool worker**. Runs in a child.

    The parent's own summary describes the parent, and the packing actually
    happens in forkserver children: they resolve ``rust_backend.available()``
    and ``exact_available()`` in their own process, from their own module state.
    Used as the warm-up payload so proving the child costs nothing beyond the
    fork the warm-up already pays for.

    Module-level (not a closure or a lambda) because it has to pickle, and pure
    for the same reason ``run_pool_job`` is: a child has no logging config.
    """
    return {
        "pid": os.getpid(),
        "packing": "rust" if rust_backend.available() else "python",
        "cpsat": exact_available(),
    }
