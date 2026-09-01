"""Optional Rust kernel for candidate generation (``opticutter_core``).

``search._Searcher.gen_fills`` is the hot loop of the whole optimizer: the
interpreted geometry underneath it (``packer._place_piece``, ``strip_fill``,
``_split_remainder``) is ~90% of the run time on pools above 120 pieces, where
``OPT_EXACT_MAX_PIECES`` gates CP-SAT out. The Rust crate in ``rust/`` is a
transliteration of that geometry, and this module is the bridge to it.

**The Python implementation stays the oracle.** The crate produces byte-identical
placements, leftovers and cuts (``scripts/diff_rust_parity.py`` compares them
exhaustively over the shop's real jobs), which has two consequences worth
stating explicitly:

- the backend must **NOT** enter the cache hash — a box without the wheel falls
  back to Python and reproduces the very same layouts, so a cached payload stays
  valid across both;
- ``ENGINE_VERSION`` does not move either, for the same reason.

The import is guarded exactly like ``exact.py``'s ``ortools``: without the wheel
``available()`` is ``False`` and the engine runs the pure-Python path.

Selection is by ``OPT_ENGINE_BACKEND`` (``auto`` — the default — ``rust`` or
``python``); ``auto`` uses the crate whenever it imported. Reading the variable
here keeps ``src/cutting/`` framework-free (stdlib only) while letting a
deployment pin the interpreted path without a redeploy of the caller.
"""

import os
from typing import Dict, List, Optional, Sequence, Tuple

from src.cutting.constructors import BinFill, GreedyConfig
from src.cutting.enums import PackingStrategy, SplitRule
from src.cutting.models import BinSpec, Cut, Piece, PlacedPiece, Rectangle
from src.cutting.parameters import CuttingParameters

try:  # pragma: no cover - exercised through ``available``
    import opticutter_core
except ImportError:  # pragma: no cover - optional native dependency
    opticutter_core = None


# Wire codes for the enums. The crate matches on these integers, so the mapping
# lives on both sides and must move together (see ``models.rs``).
_SORT_CODES: Dict[str, int] = {
    "area": 0,
    "maxdim": 1,
    "height": 2,
    "width": 3,
    "perimeter": 4,
    "mindim": 5,
}
_SPLIT_CODES: Dict[SplitRule, int] = {
    SplitRule.SHORTER_LEFTOVER_AXIS: 0,
    SplitRule.LONGER_LEFTOVER_AXIS: 1,
    SplitRule.MINIMIZE_AREA: 2,
    SplitRule.MAXIMIZE_AREA: 3,
    SplitRule.SHORTER_AXIS: 4,
    SplitRule.LONGER_AXIS: 5,
}
_SELECTION_CODES: Dict[PackingStrategy, int] = {
    PackingStrategy.MAX_EFFICIENCY: 0,
    PackingStrategy.LONG_OFFCUTS: 1,
}


_ENABLED: Optional[bool] = None


def _resolve() -> bool:
    if opticutter_core is None:
        return False
    return os.environ.get("OPT_ENGINE_BACKEND", "auto").strip().lower() in (
        "auto",
        "rust",
    )


def available() -> bool:
    """``True`` when the native kernel is installed and selected.

    Resolved once per process: the answer must not change under a running
    search, and it is read on paths hot enough that re-reading the environment
    would show up in a profile.
    """
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = _resolve()
    return _ENABLED


def requested() -> str:
    """The backend the environment ASKED for: ``auto`` / ``rust`` / ``python``.

    Deliberately separate from ``available()``: a box that asks for ``rust`` and
    has no wheel runs Python, and reporting only the effective answer would make
    that degradation indistinguishable from a deliberate ``python``.
    """
    return os.environ.get("OPT_ENGINE_BACKEND", "auto").strip().lower()


def status() -> Dict[str, object]:
    """What this process would actually run, and what it was asked to run.

    Pure and cheap: returns data, never logs. ``src/cutting/`` has no logging at
    all and stays that way — the caller decides where this goes. Callers outside
    the domain layer (``main``'s startup line, the pool worker probe, the bench
    scripts) are the ones that render it.
    """
    return {
        "requested": requested(),
        "effective": "rust" if available() else "python",
        "wheel_importable": opticutter_core is not None,
        "wheel_version": getattr(opticutter_core, "__version__", None),
    }


def degraded() -> bool:
    """``True`` when ``rust`` was demanded but the wheel could not be imported.

    The silent failure this whole reporting path exists for: ``_resolve`` checks
    the wheel BEFORE the variable, so the demand is dropped without an error.
    """
    return requested() == "rust" and opticutter_core is None


def set_enabled(value: Optional[bool]) -> None:
    """Forces the backend choice; ``None`` re-resolves from the environment.

    For the differential harnesses and the tests that pin one path — production
    selects via ``OPT_ENGINE_BACKEND``.
    """
    global _ENABLED
    _ENABLED = None if value is None else (bool(value) and opticutter_core is not None)


def encode_portfolio(
    portfolio: Sequence[GreedyConfig],
) -> List[Tuple[int, int, int]]:
    """Greedy configs as wire codes, preserving the exploration order.

    Hoisted out of ``gen_fills`` because the portfolio is fixed for a whole
    search while ``gen_fills`` runs thousands of times.
    """
    return [
        (
            _SORT_CODES[c.sort],
            _SPLIT_CODES[c.split],
            _SELECTION_CODES[c.selection],
        )
        for c in portfolio
    ]


def _encode_pool(pool: Sequence[Piece]) -> List[Tuple]:
    """The pool as the crate sees it: no strings, positions instead of ids.

    Every comparator in ``SORT_KEYS`` ends in ``p.id`` as its final tiebreak, so
    each piece carries the *rank* of its id in this pool's lexicographic id
    order; comparing ranks reproduces Python's string ordering exactly. The
    leading index is the pool position, and it is what comes back on each
    placement.
    """
    ranks = {pid: i for i, pid in enumerate(sorted(p.id for p in pool))}
    return [
        (i, ranks[p.id], p.width, p.height, bool(p.can_rotate), int(p.priority))
        for i, p in enumerate(pool)
    ]


def gen_fills(
    pool: Sequence[Piece],
    spec: BinSpec,
    params: CuttingParameters,
    portfolio_codes: Sequence[Tuple[int, int, int]],
    tries: int,
    min_rect_size: float,
) -> List[BinFill]:
    """``search._Searcher.gen_fills`` executed natively.

    Same contract: candidate fills of ``spec`` from ``pool``, already deduped by
    placed-piece-type multiset and in the same order.
    """
    raw = opticutter_core.gen_fills(
        _encode_pool(pool),
        spec.width,
        spec.height,
        (
            params.kerf,
            params.top_trim,
            params.bottom_trim,
            params.left_trim,
            params.right_trim,
        ),
        list(portfolio_codes),
        int(tries),
        min_rect_size,
    )
    return [_decode_fill(fill, pool, spec) for fill in raw]


def probe_fill(
    pool: Sequence[Piece],
    spec: BinSpec,
    params: CuttingParameters,
    portfolio_codes: Sequence[Tuple[int, int, int]],
    target: int,
    min_rect_size: float,
) -> Optional[BinFill]:
    """``search._Searcher._probe_fill`` executed natively.

    Ten constructors collapse into one crossing, and the pool is encoded once
    instead of ten times — which the profile showed mattering as much as the
    packing itself once the geometry moved to Rust.
    """
    raw = opticutter_core.probe_fill(
        _encode_pool(pool),
        spec.width,
        spec.height,
        (
            params.kerf,
            params.top_trim,
            params.bottom_trim,
            params.left_trim,
            params.right_trim,
        ),
        list(portfolio_codes),
        int(target),
        min_rect_size,
    )
    return None if raw is None else _decode_fill(raw, pool, spec)


def _decode_fill(raw: Tuple, pool: Sequence[Piece], spec: BinSpec) -> BinFill:
    placed, rects, cuts = raw
    return BinFill(
        spec=spec,
        placed=[
            PlacedPiece(
                piece=pool[i],
                x=x,
                y=y,
                width=width,
                height=height,
                rotated=rotated,
            )
            for (i, x, y, width, height, rotated) in placed
        ],
        remainders=[Rectangle(x, y, width, height) for (x, y, width, height) in rects],
        cuts=[
            Cut(x, y, length, is_horizontal=is_horizontal)
            for (x, y, length, is_horizontal) in cuts
        ],
    )


def strip_fill(
    pool: Sequence[Piece],
    spec: BinSpec,
    params: CuttingParameters,
    horizontal: bool = False,
    first_dim: Optional[float] = None,
    max_repeat: Optional[int] = None,
    min_rect_size: float = 0.1,
) -> Optional[BinFill]:
    """``constructors.strip_fill`` executed natively.

    No production caller today: ``gen_fills`` seeds its own strip
    variants inside the crate, so this crosses only for the differential
    harnesses, which need to call it one variant at a time.
    """
    raw = opticutter_core.strip_fill(
        _encode_pool(pool),
        spec.width,
        spec.height,
        (
            params.kerf,
            params.top_trim,
            params.bottom_trim,
            params.left_trim,
            params.right_trim,
        ),
        bool(horizontal),
        first_dim,
        max_repeat,
        min_rect_size,
    )
    return None if raw is None else _decode_fill(raw, pool, spec)


def greedy_fill(
    pool: Sequence[Piece],
    spec: BinSpec,
    params: CuttingParameters,
    config: GreedyConfig,
    min_rect_size: float = 0.1,
) -> Optional[BinFill]:
    """``constructors.greedy_fill`` executed natively.

    Used by ``search._sequential_fill`` — the stage-0 baseline greedy,
    which is outside a ``_Searcher`` and so dispatches on
    ``available()`` itself rather than on a cached flag.
    """
    raw = opticutter_core.greedy_fill(
        _encode_pool(pool),
        spec.width,
        spec.height,
        (
            params.kerf,
            params.top_trim,
            params.bottom_trim,
            params.left_trim,
            params.right_trim,
        ),
        _SORT_CODES[config.sort],
        _SPLIT_CODES[config.split],
        _SELECTION_CODES[config.selection],
        min_rect_size,
    )
    return None if raw is None else _decode_fill(raw, pool, spec)
