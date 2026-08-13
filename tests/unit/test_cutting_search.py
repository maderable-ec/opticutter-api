"""Board-count search: regression benchmarks + validity invariants.

The two benchmark fixtures are the real pre-orders (20 and 21) used to audit
the engine against a commercial cutting program (board-count parity is the
target). They pin the search's *outcomes* — the mechanisms stay general:

- Pre-order 21 (79 pieces, grain-locked, 16 full-length 70x2780 strips): the
  commercial reference uses 5 boards. The old single-pass greedy used 6 at any
  kerf (15 strips stranded on a near-empty last board).
- Pre-order 20 (80 rotatable drawer pieces): the commercial reference bills
  1 board + 1 half board.

The heuristic search (Phase 1) reached parity only at kerf 4 — at kerf 5 it
billed an extra half board on 21 and an extra full board on 20 — and the CP-SAT
endgame (Phase 2, ``cutting/exact.py``) closed the remaining gap. 21 at 5 boards
is the area lower bound, so it is provably optimal, not merely equal to the
reference.

**The parity numbers live at kerf 4**, because that is what the shop's saw cuts
(confirmed with the operator on 2026-08-13) and a board count is only a
commercial claim at the blade the shop actually uses. Pre-order 21 no longer
reaches 5 boards at kerf 5: metering the solver (``ENGINE_VERSION`` 6 — see
``ExactConfig``) traded that win for a 2.5x faster engine, deliberately and with
the numbers on the table. What remains pinned at kerf 5 is the *invariant* that
survives any configuration: the bill is never worse than the heuristics alone
(``test_preorder_21_at_kerf_5_is_never_worse_than_the_heuristics``).

**Pre-order 21 used to be BUILD-dependent at kerf 5**, and the tests still carry
the machinery that proves the engine no longer inherits a build's arbitrary
choices. That result hangs on ``_Searcher.exact_best_fill``, which asks CP-SAT
for the densest possible first board (``solve_bin(require_all=False)``,
maximizing area). Many packings tie at that maximum, and which one comes back is
arbitrary — reproducible only *for a given OR-Tools binary*. With
``ortools==9.14.6206`` the macOS arm64 wheel packed 5 boards while the manylinux
x86_64 wheel (the build that ships, and the one CI runs) packed 6, because the
layout inherited the binary's arbitrary pick and nothing downstream recovered it:
not a bigger call budget, not a wider beam, not LNS.

The fix was to stop inheriting it. ``exact._build_fill`` now reconstructs a
canonical layout from the solver's answer — column widths tightened to their
content, columns ordered by it, instances attached only afterwards — so tied
optima collapse to one layout.
``test_preorder_21_bills_the_same_under_any_solver_tie_break`` keeps it honest by
perturbing the tie-break 12 ways; ``tests/unit/test_cutting_exact.py`` pins the
reconstruction invariants themselves.

What is still *not* canonical: two tied optima can differ in total cut length
(the pieces consumed and the waste no longer vary — measured). Cut length is the
last tiebreak of ``_Solution.objective()``, reached only when cost, board count
and waste are all equal, so it can pick a different-looking diagram for the same
bill. Parity numbers stay marked ``benchmark`` and are measured against the
production build with ``make benchmark``; CI runs ``-m "not benchmark"``.
"""

import json

import pytest

from src.cutting import (
    BinSpec,
    CuttingParameters,
    ExactConfig,
    Piece,
    SearchBudget,
    exact,
    optimize_bins,
)
from src.cutting.search import ExactBudget, _Searcher
from tests.unit.cutting_invariants import assert_valid_layouts

PARAMS_KERF4 = CuttingParameters(
    kerf=4, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)
PARAMS_KERF5 = CuttingParameters(
    kerf=5, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)

# MDP 2070x2800; the half board is width/2 at price/2 * (1 + 10% markup).
FULL = BinSpec(key="board", width=2070, height=2800, thickness=15, cost_per_unit=58.0)
HALF = BinSpec(
    key="board",
    width=1035,
    height=2800,
    thickness=15,
    cost_per_unit=31.90,
    half_board=True,
)

# (width, height, quantity, can_rotate)
PREORDER_21 = [
    (425, 750, 6, False),
    (425, 1890, 6, False),
    (665, 750, 3, False),
    (665, 1075, 3, False),
    (665, 200, 12, False),
    (520, 2770, 5, False),
    (200, 470, 14, False),
    (200, 185, 4, False),
    (600, 2770, 1, False),
    (315, 2770, 1, False),
    (300, 450, 6, False),
    (300, 200, 2, False),
    (70, 2780, 16, False),
]
PREORDER_20 = [
    (150, 570, 32, True),
    (150, 400, 32, True),
    (370, 570, 16, True),
]


def _pieces(spec):
    return [
        Piece(id=f"p{i}", width=w, height=h, quantity=q, can_rotate=r)
        for i, (w, h, q, r) in enumerate(spec)
    ]


def _total_instances(spec):
    return sum(q for _, _, q, _ in spec)


HALF_20 = BinSpec(
    key="board",
    width=1035,
    height=2800,
    thickness=15,
    cost_per_unit=26.40,
    half_board=True,
)
FULL_20 = BinSpec(
    key="board", width=2070, height=2800, thickness=15, cost_per_unit=48.0
)


@pytest.mark.slow
def test_preorder_21_reaches_commercial_parity_at_kerf_4():
    """5 boards (= the area lower bound = the commercial reference)."""
    layouts, unplaced = optimize_bins(
        _pieces(PREORDER_21), [FULL, HALF], cutting_params=PARAMS_KERF4
    )
    assert unplaced == []
    assert_valid_layouts(layouts, unplaced, PARAMS_KERF4, _total_instances(PREORDER_21))
    cost = sum(layout.material.cost_per_unit for layout in layouts)
    assert len(layouts) == 5
    assert cost == pytest.approx(290.0)


# What the heuristics alone bill on pre-order 21 at kerf 5: 5 full boards + 1
# half (5 * 58.00 + 31.90). Measured identical on macOS arm64 and linux x86_64,
# because no solver is involved — it is the honest ceiling for that cut list.
P21_KERF5_HEURISTIC_COST = 321.90


@pytest.mark.slow
def test_preorder_21_at_kerf_5_is_never_worse_than_the_heuristics():
    """Build-independent contract for the one case where builds disagree.

    Whichever tied-optimal first board CP-SAT happens to return, the pipeline
    keeps the better of the exact-seeded and pure-heuristic runs, so the bill
    can never exceed the heuristic-only one. This is the *only* claim this cut
    list still makes at kerf 5: the parity number lives at kerf 4, the blade the
    shop cuts with — see the module docstring.
    """
    layouts, unplaced = optimize_bins(
        _pieces(PREORDER_21), [FULL, HALF], cutting_params=PARAMS_KERF5
    )
    assert unplaced == []
    assert_valid_layouts(layouts, unplaced, PARAMS_KERF5, _total_instances(PREORDER_21))
    cost = sum(layout.material.cost_per_unit for layout in layouts)
    assert len(layouts) <= 6
    assert cost <= P21_KERF5_HEURISTIC_COST + 1e-6


@pytest.mark.slow
@pytest.mark.benchmark
def test_preorder_21_bills_the_same_under_any_solver_tie_break(monkeypatch):
    """The bill does not depend on WHICH tied optimum CP-SAT returns.

    Offsetting ``random_seed`` on every solver call is the local proxy for
    running a different OR-Tools build: both change the arbitrary pick among
    equally-optimal answers, and offsets 3 and 6 used to reproduce exactly the
    6-board result the linux/amd64 wheel gave while macOS billed 5. Sweeping 12
    of them is the honest acceptance criterion — one green build proves nothing.

    Run at kerf 5 on purpose: that is where the solver actually decides this cut
    list (at kerf 4 the heuristics reach the parity number on their own, so a
    sweep there would pass without testing anything). What is asserted is the
    property canonical reconstruction bought — every tie-break lands on the same
    bill — and not a specific board count, which is a *commercial* claim and
    belongs at the kerf the shop cuts (see the kerf-4 test above).
    """
    from ortools.sat.python import cp_model

    original = cp_model.CpSolver.Solve
    costs = set()

    for offset in range(12):

        def perturbed(self, model, *args, _offset=offset, **kwargs):
            self.parameters.random_seed = self.parameters.random_seed + _offset
            return original(self, model, *args, **kwargs)

        monkeypatch.setattr(cp_model.CpSolver, "Solve", perturbed)
        layouts, unplaced = optimize_bins(
            _pieces(PREORDER_21), [FULL, HALF], cutting_params=PARAMS_KERF5
        )
        assert unplaced == []
        assert_valid_layouts(
            layouts, unplaced, PARAMS_KERF5, _total_instances(PREORDER_21)
        )
        costs.add(round(sum(layout.material.cost_per_unit for layout in layouts), 2))

    assert len(costs) == 1, f"the tie-break changes the bill: {sorted(costs)}"
    assert max(costs) <= P21_KERF5_HEURISTIC_COST + 1e-6


@pytest.mark.slow
@pytest.mark.parametrize("params", [PARAMS_KERF4, PARAMS_KERF5], ids=["kerf4", "kerf5"])
def test_preorder_20_reaches_commercial_parity(params):
    """1 full + 1 half board (the commercial '1.5 boards'), at either kerf.

    At kerf 5 the heuristic search billed 2 full boards (96.00) and the residual
    left by its best first board genuinely did not fit a half — the exact
    endgame finds a denser first board, which makes the split feasible.
    """
    layouts, unplaced = optimize_bins(
        _pieces(PREORDER_20), [FULL_20, HALF_20], cutting_params=params
    )
    assert unplaced == []
    assert_valid_layouts(layouts, unplaced, params, _total_instances(PREORDER_20))
    assert len(layouts) == 2
    assert sorted(layout.material.half_board for layout in layouts) == [False, True]
    cost = sum(layout.material.cost_per_unit for layout in layouts)
    assert cost == pytest.approx(74.40)


# A job where the solver's densest opening board is NOT the globally cheapest
# one: seeding the beam with it used to displace the partition that saved a half
# board, so the exact endgame came out *worse* than the heuristics alone. Pinned
# because that failure mode is invisible on the benchmarks above (there the
# solver always wins) and only shows up as silently pricier quotes.
EXACT_TRAP = [
    (150, 1890, 11, True),
    (600, 150, 5, False),
    (665, 1890, 14, False),
    (665, 1890, 4, True),
    (400, 900, 5, False),
    (150, 750, 12, False),
    (150, 300, 3, False),
    (570, 450, 5, False),
    (150, 150, 14, True),
    (250, 450, 13, True),
    (400, 570, 10, True),
]


@pytest.mark.slow
def test_exact_endgame_never_costs_more_than_the_heuristics_alone():
    """The solver is additive: enabling it can only lower the bill."""

    def cost(exact_config):
        layouts, unplaced = optimize_bins(
            _pieces(EXACT_TRAP),
            [FULL, HALF],
            cutting_params=PARAMS_KERF5,
            exact_config=exact_config,
        )
        assert unplaced == []
        assert_valid_layouts(
            layouts, unplaced, PARAMS_KERF5, _total_instances(EXACT_TRAP)
        )
        return sum(layout.material.cost_per_unit for layout in layouts)

    assert cost(ExactConfig(enabled=True)) <= cost(ExactConfig(enabled=False))


def test_the_two_solver_entries_get_different_work_budgets(monkeypatch):
    """The entry that decides keeps the big budget; the one that optimizes doesn't.

    ``fits_one_bin`` (``require_all=True``) terminates on a proof, so a generous
    allowance is mostly unspent. ``solve_bin(require_all=False)`` maximizes area
    and never proves optimality on a real pool, so it spends every unit it is
    handed — 13.79s of a 15.18s run on one measured pool, for an identical bill.
    Pinned here because the two budgets are one keyword apart and nothing else
    in the suite would notice them being swapped or collapsed.
    """
    seen = []

    def fake_solve_bin(pool, spec, params, *, require_all=True, **kwargs):
        seen.append((require_all, kwargs["deterministic_time"]))
        return None

    monkeypatch.setattr(exact, "is_available", lambda: True)
    monkeypatch.setattr(exact, "solve_bin", fake_solve_bin)

    searcher = _Searcher(
        specs=[FULL],
        params=PARAMS_KERF4,
        budget=SearchBudget(),
        seed=0,
        min_rect_size=0.1,
        max_sheets=10,
        exact_config=ExactConfig(deterministic_time=6.0, root_deterministic_time=0.5),
    )
    # Dense enough to clear ``exact_fill``'s min_fill_ratio gate.
    pool = [Piece(id=f"p{i}", width=1000, height=1400, quantity=1) for i in range(3)]
    searcher.exact_fill(pool, FULL)
    searcher.exact_best_fill(pool, FULL)

    assert (True, 6.0) in seen, "the feasibility entry lost its full budget"
    assert (False, 0.5) in seen, "the maximizing entry is not on its own budget"


def test_the_stagnation_cutoff_forgets_a_miss_as_soon_as_the_solver_pays():
    """Patience is about *consecutive* misses, not a lifetime quota.

    A run where the solver wins a board every third round must keep being
    consulted; only an uninterrupted streak is evidence that the neighborhood is
    exhausted. Same contract as ``_RESTART_PATIENCE``.
    """
    budget = ExactBudget(max_calls=40, root_patience=2)
    assert budget.root_allowed()

    budget.note_root(improved=False)
    assert budget.root_allowed(), "one fruitless round must not stop the solver"
    budget.note_root(improved=False)
    assert not budget.root_allowed(), "two in a row is the cutoff"

    budget.note_root(improved=True)
    assert budget.root_allowed(), "an improvement has to revive it"


@pytest.mark.slow
def test_the_stagnation_cutoff_stops_the_repair_from_asking(monkeypatch):
    """The cutoff is actually wired into LNS, not just available on the budget.

    Asserted as a *strict* drop in solver asks rather than a fixed number: the
    count depends on how the beam ruins bins, which any search change may move,
    while "patience 0 asks less than unlimited patience" is the contract itself.
    """

    # Captured once, before any patching: the helper runs twice and would
    # otherwise wrap its own wrapper on the second pass.
    original = _Searcher.exact_best_fill

    def count_asks(patience):
        asks = []

        def counting(self, pool, spec):
            asks.append(len(pool))
            return original(self, pool, spec)

        monkeypatch.setattr(_Searcher, "exact_best_fill", counting)
        # Pre-order 21: a pool whose lower bound the repair loop chases for many
        # rounds, so the asks it makes are the LNS ones. (A job that proves its
        # bound immediately never enters LNS and would count only the beam's own
        # root ask, which this gate deliberately leaves alone.)
        optimize_bins(
            _pieces(PREORDER_21),
            [FULL, HALF],
            cutting_params=PARAMS_KERF5,
            budget=SearchBudget(tries_per_board=16, iterations=8, beam_width=4),
            exact_config=ExactConfig(root_patience=patience),
        )
        return len(asks)

    unlimited = count_asks(10**6)
    cut = count_asks(0)
    assert cut < unlimited, "the patience gate never reached the repair loop"


@pytest.mark.slow
def test_search_without_the_exact_endgame_still_solves_and_stays_valid():
    """OR-Tools is optional: disabling it degrades quality, never correctness."""
    layouts, unplaced = optimize_bins(
        _pieces(PREORDER_20),
        [FULL_20, HALF_20],
        cutting_params=PARAMS_KERF5,
        exact_config=ExactConfig(enabled=False),
    )
    assert unplaced == []
    assert_valid_layouts(layouts, unplaced, PARAMS_KERF5, _total_instances(PREORDER_20))
    # The heuristic-only result: 2 full boards, one board worse than with CP-SAT.
    assert sum(layout.material.cost_per_unit for layout in layouts) == pytest.approx(
        96.0
    )


def test_search_is_deterministic():
    """Same inputs, same budget -> byte-identical serialized layouts."""
    budget = SearchBudget(tries_per_board=16, iterations=8, beam_width=4)

    def run():
        layouts, _ = optimize_bins(
            _pieces(PREORDER_21),
            [FULL, HALF],
            cutting_params=PARAMS_KERF5,
            budget=budget,
        )
        return json.dumps([layout.to_dict() for layout in layouts], sort_keys=True)

    assert run() == run()


def test_variant_seed_produces_valid_alternative():
    """A non-zero seed keeps every physical invariant (it only reorders search)."""
    budget = SearchBudget(tries_per_board=16, iterations=8, beam_width=4)
    layouts, unplaced = optimize_bins(
        _pieces(PREORDER_20),
        [FULL, HALF],
        cutting_params=PARAMS_KERF5,
        budget=budget,
        seed=3,
    )
    assert_valid_layouts(layouts, unplaced, PARAMS_KERF5, _total_instances(PREORDER_20))
    assert unplaced == []


def test_oversized_piece_is_reported_unplaced():
    """A piece that fits no bin comes back in ``unplaced`` (never dropped silently)."""
    pieces = [
        Piece(id="ok", width=400, height=400, quantity=1),
        Piece(id="huge", width=3000, height=3000, quantity=1, can_rotate=True),
    ]
    layouts, unplaced = optimize_bins(pieces, [FULL], cutting_params=PARAMS_KERF5)
    assert [p.id for p in unplaced] == ["huge"]
    assert sum(len(la.placed_pieces) for la in layouts) == 1


def test_finite_bin_count_is_respected():
    """A finite bin is never opened more times than its count."""
    offcut = BinSpec(
        key="offcut", width=600, height=600, thickness=15, cost_per_unit=0.0, count=1
    )
    pieces = [Piece(id="p", width=500, height=500, quantity=3)]
    layouts, unplaced = optimize_bins(
        pieces, [offcut, FULL], cutting_params=CuttingParameters(kerf=5)
    )
    offcut_sheets = [la for la in layouts if la.material.id == "offcut"]
    assert len(offcut_sheets) <= 1
    assert unplaced == []
    assert sum(len(la.placed_pieces) for la in layouts) == 3


def test_scaled_budget_shrinks_with_job_size():
    small = SearchBudget.scaled(40)
    big = SearchBudget.scaled(600)
    assert big.tries_per_board < small.tries_per_board
    assert big.iterations < small.iterations
    assert big.beam_width <= small.beam_width


# Anchors from ``scripts/bench_battery.py``: a handful of its generated jobs,
# pinned as cost **ceilings** rather than equalities so a future improvement
# passes and only a regression fails. They exist because P20/P21 alone are two
# single-material jobs of 14 piece types — they say nothing about the
# multi-material, many-boards shape where a stopping rule actually starts
# costing boards. The full 80-job sweep stays out of the suite (~35 min); this
# subset is the part that lives in CI's reach.
BATTERY_CEILINGS = [
    (76, 480.00),
    (26, 654.50),
    (38, 572.40),
    (58, 663.60),
    (44, 428.80),
    (39, 592.10),
    (3, 392.00),
    (19, 142.80),
]


@pytest.mark.slow
@pytest.mark.parametrize("job_index,max_cost", BATTERY_CEILINGS)
def test_battery_job_never_gets_more_expensive(job_index, max_cost):
    """A generated furniture job still bills at most what it billed before."""
    from scripts.bench_battery import PARAMS as BATTERY_PARAMS
    from scripts.bench_battery import build_job

    total = 0.0
    for full, half, pieces in build_job(job_index, seed=20260807):
        instances = sum(p.quantity for p in pieces)
        layouts, unplaced = optimize_bins(
            pieces,
            [full, half],
            cutting_params=BATTERY_PARAMS,
            budget=SearchBudget.scaled(instances, tries_per_board=48, iterations=40),
        )
        assert unplaced == []
        assert_valid_layouts(layouts, unplaced, BATTERY_PARAMS, instances)
        total += sum(layout.material.cost_per_unit for layout in layouts)

    assert (
        total <= max_cost + 1e-6
    ), f"battery job {job_index} now bills ${total:.2f}, was ${max_cost:.2f}"
