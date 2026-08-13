"""The native kernel must be a pure speedup: identical geometry, or nothing.

``src/cutting/rust_backend.py`` swaps the interpreted geometry for the Rust
crate in ``rust/``. The whole bet is that the swap is invisible: payloads are
cached by input hash and ``ENGINE_VERSION`` does **not** move for the port, so a
box with the wheel and a box without it must be able to read each other's cache
entries. That is only true while these comparisons hold.

The exhaustive version of this lives in ``scripts/diff_rust_parity.py`` (48
greedy configs x every strip variant x 13 real pools) and
``scripts/diff_rust_engine.py`` (the full engine, per-pool geometry digests);
what is pinned here is a fast subset CI can run on every commit.

Everything skips when the wheel is absent — that is the supported fallback, and
CI additionally re-runs the cutting suite with ``OPT_ENGINE_BACKEND=python`` so
the interpreted path cannot rot behind the kernel.
"""

import json

import pytest

from src.cutting import (
    BinSpec,
    CuttingParameters,
    ExactConfig,
    Piece,
    SearchBudget,
    optimize_bins,
    rust_backend,
)
from src.cutting import constructors as py_constructors
from src.cutting.constructors import GREEDY_PORTFOLIO
from src.cutting.packer import expand_pieces

pytestmark = pytest.mark.skipif(
    rust_backend.opticutter_core is None,
    reason="opticutter_core (rust/) is not installed",
)

PARAMS = CuttingParameters(
    kerf=5, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)
FULL = BinSpec(key="board", width=2070, height=2800, thickness=15, cost_per_unit=58.0)
HALF = BinSpec(
    key="board",
    width=1035,
    height=2800,
    thickness=15,
    cost_per_unit=31.90,
    half_board=True,
)

# A cabinet-shaped mix: rotatable and grain-locked, a full-length strip that
# only fits a virgin board, and repeated types (which is what exercises the
# ``max_repeat`` cap and the id tiebreak of every sort key).
POOL = [
    (425, 750, 6, False),
    (665, 200, 8, False),
    (150, 570, 10, True),
    (370, 570, 6, True),
    (70, 2780, 4, False),
    (300, 450, 4, True),
]


@pytest.fixture
def pieces():
    return expand_pieces(
        [
            Piece(id=f"p{i}", width=float(w), height=float(h), quantity=q, can_rotate=r)
            for i, (w, h, q, r) in enumerate(POOL)
        ]
    )


@pytest.fixture(autouse=True)
def _restore_backend():
    yield
    rust_backend.set_enabled(None)


def shape(fill):
    """A fill reduced to the numbers that must match, ids included."""
    if fill is None:
        return None
    return (
        [
            (pp.piece.id, pp.x, pp.y, pp.width, pp.height, pp.rotated)
            for pp in fill.placed
        ],
        [(r.x, r.y, r.width, r.height) for r in fill.remainders],
        [(c.x, c.y, c.length, c.is_horizontal) for c in fill.cuts],
    )


@pytest.mark.parametrize("spec", [FULL, HALF], ids=["full", "half"])
def test_greedy_fill_is_bit_identical_across_the_whole_portfolio(pieces, spec):
    for config in GREEDY_PORTFOLIO:
        assert shape(rust_backend.greedy_fill(pieces, spec, PARAMS, config)) == shape(
            py_constructors.greedy_fill(pieces, spec, PARAMS, config)
        ), config


@pytest.mark.parametrize("horizontal", [False, True])
@pytest.mark.parametrize("max_repeat", [None, 1, 2])
def test_strip_fill_is_bit_identical(pieces, horizontal, max_repeat):
    kwargs = {"horizontal": horizontal, "max_repeat": max_repeat}
    assert shape(rust_backend.strip_fill(pieces, FULL, PARAMS, **kwargs)) == shape(
        py_constructors.strip_fill(pieces, FULL, PARAMS, **kwargs)
    )


def test_strip_fill_honors_the_first_dim_seed_identically(pieces):
    # ``first_dim`` forces the opening strip and is cleared afterwards whether
    # or not the seed fitted — the kind of detail a transliteration drops.
    for first_dim in (2780.0, 665.0, 70.0, 9999.0):
        assert shape(
            rust_backend.strip_fill(pieces, FULL, PARAMS, first_dim=first_dim)
        ) == shape(
            py_constructors.strip_fill(pieces, FULL, PARAMS, first_dim=first_dim)
        ), first_dim


def _run(pieces):
    layouts, unplaced = optimize_bins(
        pieces,
        [FULL, HALF],
        cutting_params=PARAMS,
        budget=SearchBudget.scaled(len(pieces)),
        exact_config=ExactConfig(),
    )
    return json.dumps(
        [lay.to_dict() for lay in layouts], sort_keys=True, default=str
    ), [p.id for p in unplaced]


def test_the_whole_engine_produces_identical_geometry_on_both_backends(pieces):
    """The claim that lets the port skip ``ENGINE_VERSION`` and the cache hash."""
    rust_backend.set_enabled(False)
    py_geometry, py_unplaced = _run(pieces)
    rust_backend.set_enabled(True)
    rust_geometry, rust_unplaced = _run(pieces)

    assert rust_unplaced == py_unplaced
    assert rust_geometry == py_geometry


def test_set_enabled_none_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("OPT_ENGINE_BACKEND", "python")
    rust_backend.set_enabled(None)
    assert rust_backend.available() is False

    monkeypatch.setenv("OPT_ENGINE_BACKEND", "rust")
    rust_backend.set_enabled(None)
    assert rust_backend.available() is True
