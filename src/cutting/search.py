"""Board-count search: bin packing as an explicit objective.

The single-pass greedy treats the board count as *emergent* (fill until nothing
fits, open another), which strands awkward pieces — full-length strips only fit
a virgin board, so they pile onto a near-empty last one, and a half board can
never be targeted on purpose. ``optimize_bins`` makes total material COST the
objective, in stages:

1. **Baseline**: the legacy sequential greedy (first portfolio config) gives an
   upper bound in milliseconds; if it already matches the cost lower bound the
   search exits immediately (most small jobs).
2. **Beam search over partitions**: states are (remaining pieces, opened bins);
   each expansion evaluates a portfolio of candidate fills per bin type
   (greedy configs x strip constructors, see ``constructors.py``) and keeps
   the ``beam_width`` cheapest states by cost + lower bound.
3. **LNS repair**: ruin pairs/triples of the worst bins and re-search just
   that sub-pool, accepting strictly better solutions. This is what closes
   "5 boards + a few orphan pieces" into 5 boards.
4. **Half-board downgrade**: any full-bin layout whose content re-packs into
   its half sibling (same ``key``, ``half_board=True``, cheaper) is swapped —
   guaranteed parity with the old post-hoc `apply_half_boards` even when the
   search itself didn't pick the half bin.

The CP-SAT endgame (``exact.py``) plugs into 2 and 3: it decides exactly whether
a tail closes in one more bin, and it can open a bin with the densest fill that
exists rather than the densest a sort-and-place order stumbles onto. Because the
beam keeps a bounded number of states, a solver-seeded opening can *displace*
the partition that was globally right — so ``optimize_bins`` runs the seeded and
the pure-heuristic pipelines separately and keeps the better one, skipping the
second whenever the first already proved the lower bound. That is what makes the
solver strictly additive: it can never cost a board the heuristics would have
saved.

**Determinism is a hard requirement** — the payload is cached by input hash, so
the budget is counted in candidate fills (decodes) and solver calls, never wall
clock, every iteration order is stable, and ``seed`` (the request ``variant``)
only reorders exploration to yield alternative solutions on demand.

``LONG_OFFCUTS`` keeps the legacy single pass on purpose: its contract is
geometric (one continuous reusable strip) and minimizing boards would trade it
away. It still benefits from the half-board downgrade.
"""

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from src.cutting import constructors as _py_constructors
from src.cutting import exact, rust_backend
from src.cutting.constructors import (
    GREEDY_PORTFOLIO,
    BinFill,
    GreedyConfig,
    greedy_fill,
    piece_type,
    strip_fill,
)
from src.cutting.enums import (
    PACKING_STRATEGY_SPLIT_RULE,
    PackingStrategy,
    SplitRule,
)
from src.cutting.models import BinSpec, CuttingLayout, Piece, PlacedPiece
from src.cutting.packer import expand_pieces
from src.cutting.parameters import CuttingParameters

# Bump whenever the search produces different geometry for the same inputs, so
# the orchestrator can salt its cache hash and never serve stale layouts.
# 3 = CP-SAT exact endgame (``exact.py``); 4 = canonical reconstruction of the
# solver's answer, which changes exact-built layouts (tighter columns) and makes
# them build-independent; 5 = seeded restarts stop after ``_RESTART_PATIENCE``
# fruitless rounds, so a pool whose lower bound is unreachable no longer burns
# the whole restart budget; 6 = the solver is metered instead of being trusted —
# its maximizing entry gets its own small work budget
# (``ExactConfig.root_deterministic_time``) and stops being consulted after
# ``root_patience`` fruitless LNS rounds, which changes which opening board the
# beam is seeded with on tight pools; 8 = the relaxed-kerf repair
# (``_REPAIR_FILL_GATE``) re-partitions plans that close on a nearly empty
# sheet; 7 = the solver's call allowance drops from
# 40 to 20 (``ExactConfig.max_calls``), which changes the layout of any pool that
# used to ask more than twenty times -- the bill is identical on all 584 real
# pools, the geometry is not; 9 = finished boards get their cut tree re-derived
# for compact leftovers (``consolidate.py``). That one moves no piece and no
# board -- only the ``cuts`` and ``remainders`` a payload reports -- but both are
# serialized into the cached payload and the order snapshot, so a stale Redis
# entry would keep drawing the old diagram; 10 = a pool whose sheets are all free
# (the client's retazos) now actually searches -- ``_cost_lower_bound`` collapses
# to 0 with any zero-cost bin, so matching it "proved" optimality before the beam
# ever opened and the plan came straight out of the greedy (see
# ``_proven_optimal``). Catalog geometry is untouched: a bought board costs more
# than zero, so it takes the same branch it always did; 11 = a pool of finite
# retazos stops letting the greedy decide what gets left over. Granting rotation
# could place FEWER pieces than forbidding it, because orientation is a fixed
# tie-break in ``packer._place_piece`` rather than a searched decision, and
# ``optimize_bins`` skips its whole search once the baseline strands a piece --
# so the unrotated pool and a yield-maximizing pass now enter as candidates and
# ``finite_plan_objective`` picks (see ``fill_finite_bins_max_yield``). Catalog
# geometry is untouched again, this time by construction: the new code is only
# reachable through ``optimize_offcut_pool``, and an infinite bin set strands
# nothing to begin with. Also bump this when the pinned ortools version moves,
# since a solver upgrade can return a different solution.
ENGINE_VERSION = 11

# A half bin is only worth opening near the end of a job: gate it by remaining
# area so early states don't waste decodes on fills the cost objective would
# discard anyway.
_HALF_GATE = 1.25

# How many consecutive seeded restarts may fail to improve the incumbent before
# the search gives up on restarting (see ``optimize_bins``).
#
# **2, not 1** — and that is measured, not cautious. Pre-order 21 at kerf 4 (the
# commercial parity fixture) gets its 5th-board win from the *second* restart:
# the first one finds nothing, so patience=1 stops one round too early and bills
# 6 boards / $321.90 instead of 5 / $290.00. An 80-job randomized battery did
# NOT catch that — every job there was cost-neutral at patience=1 — so lowering
# this again needs `make benchmark` (the P20/P21 parity fixtures), not just
# `scripts/bench_battery.py`.
_RESTART_PATIENCE = 2

# Which opening a *restart* tries. The two main pipelines above still run both
# flavors — that is what makes the solver strictly additive — but by the time
# restarts begin, the heuristic opening has already been explored twice, and
# measurement says re-running it under a fresh seed is where the time goes and
# not where the boards come from: on the 80-job battery, dropping it costs
# nothing on any job and removes a third of the remaining runtime. The
# solver-seeded opening is kept because it demonstrably *does* still win boards
# at this stage (a 62-piece pool that ends at 3 boards instead of 4).
_RESTART_FLAVORS = (True,)

# --- Relaxed-kerf repair ----------------------------------------------------
#
# The beam can open with a board so dense that the tail it strands no longer
# recomposes, and it then closes on a nearly empty sheet. The partition that
# would have worked is *even* rather than front-loaded, so its opening board
# ranks worse and gets evicted long before the cost of that choice is visible.
# LNS does not save it: on the case that motivated this (pre-order 3's white
# pool, pinned in ``tests/unit/test_cutting_search.py``) no ruin of two or three
# bins reaches the answer, and neither does 6x the budget, 8 variants or CP-SAT
# unmetered.
#
# What does reach it is solving a RELAXATION and repairing it. A thinner blade
# is a true relaxation -- every plan feasible at kerf k is feasible at kerf
# k - d -- so the relaxed search explores partitions this one evicts. Its answer
# is only a *hint*: it may not be cuttable at the real kerf, so every board is
# re-packed at the real kerf before the plan is adopted, and the repair is
# dropped whole if any board fails. That re-validation is what keeps the result
# honest; without it this would emit layouts the saw cannot cut.
#
# It is GATED because it is not free: it costs one extra search plus one
# single-board re-pack per sheet. Measured over the shop's 584-pool corpus, the
# repair wins 2 boards of 1528.5, and firing it on every pool costs 2.6x the
# engine. The gate is the symptom itself -- a plan that ends on a sheet filled
# below ``_REPAIR_FILL_GATE`` -- which fires on 12% of pools (19% of the CPU,
# they are the expensive ones) and catches all four wins. Denis took that trade
# on 2026-08-29 knowing the shape of it: +30% engine time for ~1 board in 760.
#
# Both numbers are load-bearing. At a 20% gate the cost halves but two of the
# four wins are lost; the delta of 1mm is what the four winning pools needed
# (three at -1mm, one at -2mm), and -2mm alone finds fewer.
_REPAIR_FILL_GATE = 0.30
_REPAIR_KERF_DELTAS = (1.0, 2.0)

_LEGACY_CONFIG = {
    PackingStrategy.MAX_EFFICIENCY: GreedyConfig(
        sort="area",
        split=SplitRule.SHORTER_LEFTOVER_AXIS,
        selection=PackingStrategy.MAX_EFFICIENCY,
    ),
    PackingStrategy.LONG_OFFCUTS: GreedyConfig(
        sort="height",
        split=SplitRule.LONGER_AXIS,
        selection=PackingStrategy.LONG_OFFCUTS,
    ),
}


@dataclass(frozen=True)
class SearchBudget:
    """Deterministic effort knobs (counted in candidate fills, not seconds)."""

    tries_per_board: int = 48
    iterations: int = 40
    beam_width: int = 8

    @classmethod
    def scaled(
        cls,
        n_pieces: int,
        tries_per_board: int = 48,
        iterations: int = 40,
        beam_width: int = 8,
    ) -> "SearchBudget":
        """Shrinks the budget as the job grows to keep latency ~flat.

        Deterministic in the piece count only, so it never breaks the cache
        contract.
        """
        factor = min(1.0, 140.0 / max(1, n_pieces))
        return cls(
            tries_per_board=max(8, int(tries_per_board * factor)),
            iterations=max(4, int(iterations * factor)),
            beam_width=max(3, round(beam_width * factor)),
        )


@dataclass(frozen=True)
class ExactConfig:
    """When the CP-SAT endgame (``exact.py``) may be consulted.

    The exact model answers one question — "does this whole pool close in ONE
    more bin?" — that the heuristics provably get wrong on very full packs. It
    is expensive, so it is fired only where it pays: a *tight* pool (the
    heuristics already failed on it) of bounded size, and never more than
    ``max_calls`` times per search, which keeps the worst-case latency bounded.

    ``deterministic_time`` is OR-Tools' work-based budget, not seconds: the same
    model always stops at the same point, whatever the machine is doing.

    **The two entry points get different budgets, because they are different
    problems.** ``fits_one_bin`` asks a *decision* question ("does this whole
    tail close in one bin?"): it terminates on a proof, so it is either quick or
    hopeless, and in practice it costs ~0.02s a call. ``exact_best_fill``
    *optimizes* (maximize placed area) and on a real pool never proves
    optimality, so it always runs to exhaustion — whatever budget it is handed
    is a budget it spends. Measured at kerf 4: one ``exact_best_fill`` on a
    98-piece pool burned **13.79s of a 15.18s run** without changing the bill.

    **The defaults are a priced decision, not a safe minimum.** Measured at the
    production kerf of 4: 0.5/2 makes the 13 real pools 2.5x faster (51.3s ->
    20.6s, worst pool 14.4s -> 2.3s) with **every real bill identical**, both
    solver wins included. The price shows up only in the randomized battery: 4 of
    its 80 jobs bill more (+$107 total, +0.15%) and one bills less, which across
    the whole population is **one extra board in 1159** plus three half-to-full
    swaps. Pre-order 21 also stops winning its fifth board *at kerf 5* (at kerf 4
    it keeps it). Denis took that trade on 2026-08-13 — a shop quotes with the
    client sitting there, so 19s of compute on the worst job becoming 4s is worth
    more than a board per thousand.

    **The frontier, if it ever needs walking back.** Each bound is set by one
    concrete job, not by a round number: battery job 16 needs
    ``root_deterministic_time >= 3.0``, and pre-order 21 at kerf 5 needs
    ``>= 1.0`` *and* ``root_patience >= 6``. The cheapest configuration that
    regresses nothing anywhere is **3.0/6** (13 real pools 51.3s -> 40.2s,
    battery cpu -21%, every bill identical) — that is the value to restore if the
    +0.15% ever turns out to matter. Whatever is changed here, re-run **both**
    bars: they catch different regressions, and this change had the parity
    fixtures red while the real pools looked perfect, then the battery red while
    the fixtures were green.
    """

    enabled: bool = True
    max_pieces: int = 120
    # 20, not 40, and the corpus is what set it. Over the shop's 584 real pools
    # the solver is **84% of the engine's CPU** (1716s of it; 271s with
    # ``enabled=False``) and buys **10 boards** -- 145 seconds per board. The
    # allowance is where that curve bends: 40 -> 20 bills the identical
    # 1528.5 boards with no job worse, for 18% less CPU, and halves the tail
    # that the seller actually feels (worst pool 62.6s -> 30.1s, p99 36.0s ->
    # 20.7s). Below that it starts costing material: 10 calls is -40% CPU for
    # +2 boards, 5 calls -45% for +3. The wins come from the early cheap asks
    # (the half-board downgrade closing a board on a pool that packs in under a
    # second); what 21..40 buys is LNS re-asking a neighborhood it has already
    # exhausted. Note ``max_pieces`` gates the **sub-pool**, not the job, so a
    # 348-piece export still reaches the solver through its LNS neighborhoods --
    # which is why the allowance, and not the piece cap, is the knob that bounds
    # the worst case.
    max_calls: int = 20
    deterministic_time: float = 6.0
    # Budget of the maximizing entry (``exact_best_fill``) alone. See above: it
    # buys a marginally denser opening board, never a proof.
    root_deterministic_time: float = 0.5
    # Consecutive LNS rounds whose solver-seeded sub-search fails to improve the
    # incumbent before the run stops asking for dense openings altogether (see
    # ``ExactBudget``). 0 disables the LNS seeding; a large value restores the
    # old "ask until the call budget runs out" behavior.
    root_patience: int = 2
    # Below this pool-area / bin-area ratio a failed heuristic pack means the
    # pieces genuinely don't fit, not that the greedy gave up.
    min_fill_ratio: float = 0.7
    # Seed a beam's FIRST move with the densest exact fill of each bin type
    # (the main search's first board, and the first board of every LNS
    # sub-search). Optimizing costs orders of magnitude more solver work than
    # deciding feasibility, so it runs once per (pool, bin) — memoized — and
    # never deeper in the frontier loop.
    seed_root_fill: bool = True


class ExactBudget:
    """Call allowance + memo + stagnation state, shared by every searcher of one
    ``optimize_bins`` run.

    Seeded restarts build fresh ``_Searcher``s; a per-searcher counter would let
    them multiply the worst-case solver time by the restart count, and they all
    re-ask the same root question. One shared allowance keeps the ceiling flat;
    the memo (keyed by piece-type multiset) keeps restarts from paying twice.
    Counting calls, never seconds, keeps the whole thing deterministic.

    **The stagnation cutoff.** The flat call allowance treats every solver
    question as equally worth asking, and measurement says it is not: on the 13
    real pools, two of them get a board out of the solver and the other eleven
    bill exactly the same with it off, after 26-37 root asks apiece. There is no
    known way to *predict* which pool is which — both piece count and the
    baseline's gap to the area lower bound were tried as pre-gates and falsified
    (the same 9.9% gap is a win on one pool and pure waste on another). So this
    stops predicting and starts observing: rounds that consulted the solver and
    improved nothing are evidence the neighborhood is exhausted, and after
    ``root_patience`` of them in a row the run stops asking for dense openings.
    Any improvement resets it. Same shape as ``_RESTART_PATIENCE``, and counted
    in rounds for the same reason: the payload is cached by input hash, so a
    stopping rule may never read a clock.
    """

    def __init__(self, max_calls: int, root_patience: int = 2):
        self.remaining = max_calls
        self.root_memo: Dict[tuple, Optional[BinFill]] = {}
        self.root_patience = root_patience
        self.root_misses = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def root_allowed(self) -> bool:
        """Is the maximizing entry still earning its keep this run?"""
        return self.root_misses < self.root_patience

    def note_root(self, improved: bool) -> None:
        """Records the outcome of a round that consulted the maximizing entry."""
        self.root_misses = 0 if improved else self.root_misses + 1


def _usable_area(spec: BinSpec, params: CuttingParameters) -> float:
    w = spec.width - max(0, params.left_trim) - max(0, params.right_trim)
    h = spec.height - max(0, params.top_trim) - max(0, params.bottom_trim)
    return max(0.0, w) * max(0.0, h)


def _piece_fits_spec(piece: Piece, spec: BinSpec, params: CuttingParameters) -> bool:
    w = spec.width - max(0, params.left_trim) - max(0, params.right_trim)
    h = spec.height - max(0, params.top_trim) - max(0, params.bottom_trim)
    if piece.width <= w and piece.height <= h:
        return True
    return piece.can_rotate and piece.height <= w and piece.width <= h


def _cost_lower_bound(
    rem_area: float, specs: Sequence[BinSpec], params: CuttingParameters
) -> float:
    """Valid lower bound on the extra cost needed to place ``rem_area``.

    For the dominant single-material case (one full spec + optional half
    sibling) it enumerates full/half combos over the area relaxation, which is
    tight enough to prove optimality and stop early. With zero-cost bins
    (offcuts, manual at cost 0) the bound degrades to 0 — the search then relies
    on the bin-count/waste tiebreaks.
    """
    if rem_area <= 0:
        return 0.0
    usable = {id(s): _usable_area(s, params) for s in specs}
    if any(s.cost_per_unit <= 0 and usable[id(s)] > 0 for s in specs):
        return 0.0
    fulls = [s for s in specs if not s.half_board]
    halves = [s for s in specs if s.half_board]
    if len(fulls) == 1 and len(halves) <= 1 and fulls[0].count is None:
        full = fulls[0]
        uf = usable[id(full)]
        if uf <= 0:
            return 0.0
        half = halves[0] if halves else None
        uh = usable[id(half)] if half else 0.0
        best = math.inf
        n_max = math.ceil(rem_area / uf)
        for k in range(n_max + 1):
            residual = rem_area - k * uf
            cost = k * full.cost_per_unit
            if residual > 0:
                if half is None or uh <= 0:
                    continue
                cost += math.ceil(residual / uh) * half.cost_per_unit
            best = min(best, cost)
        return best if best is not math.inf else 0.0
    densities = [s.cost_per_unit / usable[id(s)] for s in specs if usable[id(s)] > 0]
    if not densities:
        return 0.0
    return rem_area * min(densities)


def _bins_lower_bound(
    rem_area: float, specs: Sequence[BinSpec], params: CuttingParameters
) -> int:
    if rem_area <= 0:
        return 0
    biggest = max((_usable_area(s, params) for s in specs), default=0.0)
    if biggest <= 0:
        return 0
    return math.ceil(rem_area / biggest)


def _proven_optimal(
    solution: "_Solution",
    lb: float,
    rem_area: float,
    specs: Sequence[BinSpec],
    params: CuttingParameters,
) -> bool:
    """Whether the incumbent is provably as good as the objective can get.

    The search's leading objective is cost, so matching the cost lower bound is
    normally the proof — and it is what makes the wins the fastest cases. But
    ``_cost_lower_bound`` collapses to 0 the moment ANY bin is free, so a pool
    cut entirely on the client's retazos "proved" optimality against a bound of
    zero before opening the beam: the plan came straight out of the sequential
    greedy. That is exactly the job where the packing is the whole value.

    With no cost to minimize, the leading term is the sheet count, so the proof
    moves to ``_bins_lower_bound``. Still a bound, still counted in candidates
    rather than wall clock, so determinism is unaffected.
    """
    if solution.cost() > lb + 1e-6:
        return False
    if solution.cost() > 1e-6:
        return True
    return len(solution.fills) <= _bins_lower_bound(rem_area, specs, params)


def _pool_signature(pool: Sequence[Piece]) -> tuple:
    """Type-multiset identity of a piece pool (identical pools interchange)."""
    counts: Dict[tuple, int] = {}
    for p in pool:
        t = piece_type(p)
        counts[t] = counts.get(t, 0) + 1
    return tuple(sorted(counts.items()))


def _rebind_fill(fill: BinFill, pool: Sequence[Piece]) -> Optional[BinFill]:
    """Re-binds a fill's placements onto the concrete pieces of ``pool``.

    Pools that share a type signature are interchangeable geometrically, but a
    memoized fill carries the *objects* of the pool it was computed from — its
    piece ids may not exist in the current pool. Swapping each placement onto a
    same-type piece of the current pool (deterministic id order) keeps piece
    conservation exact. Returns ``None`` if the pool can't cover the fill.
    """
    by_type: Dict[tuple, List[Piece]] = {}
    for p in sorted(pool, key=lambda p: p.id):
        by_type.setdefault(piece_type(p), []).append(p)
    placed: List[PlacedPiece] = []
    for pp in fill.placed:
        candidates = by_type.get(piece_type(pp.piece))
        if not candidates:
            return None
        piece = candidates.pop(0)
        placed.append(
            PlacedPiece(
                piece=piece,
                x=pp.x,
                y=pp.y,
                width=pp.width,
                height=pp.height,
                rotated=pp.rotated,
            )
        )
    return BinFill(
        spec=fill.spec,
        placed=placed,
        remainders=fill.remainders,
        cuts=fill.cuts,
    )


@dataclass
class _Solution:
    fills: List[BinFill]
    unplaced: List[Piece]

    def cost(self) -> float:
        return sum(f.spec.cost_per_unit for f in self.fills)

    def waste(self) -> float:
        return sum(f.waste_area for f in self.fills)

    def cut_length(self) -> float:
        return sum(c.length for f in self.fills for c in f.cuts)

    def objective(self) -> tuple:
        # ``len(self.unplaced)`` is inert here, and deliberately kept for shape:
        # ``register`` only ever builds complete solutions (``unplaced=[]``), an
        # LNS candidate inherits ``best.unplaced``, and the search only runs when
        # the baseline stranded nothing -- so this term is identically 0 in every
        # comparison the engine makes. Ranking incomplete plans is a different
        # question with a different answer: ``finite_plan_objective``.
        return (
            round(self.cost(), 6),
            len(self.fills),
            len(self.unplaced),
            round(self.waste(), 3),
            round(self.cut_length(), 3),
        )


def finite_plan_objective(
    layouts: Sequence[CuttingLayout], unplaced: Sequence[Piece]
) -> tuple:
    """Quality of a plan over a FINITE bin set (lower is better).

    ``_Solution.objective`` ranks plans that place everything and asks what they
    cost. On a pool of the client's retazos the supply can genuinely run out, so
    the first question is how much of the cut list got cut -- and the cost term
    collapses to zero anyway, which is what let a plan using fewer sheets beat
    one that stranded fewer pieces.

    The terms, in order:

    - ``len(unplaced)`` -- the seller's actual complaint.
    - ``cost`` -- a company offcut may carry a price; a client's never does.
    - sheet count, then **consumed sheet area**: two plans that open one sheet
      each are not equal when one of them burned the big retazo.
    - ``rotated`` before ``waste``: between plans that cut the same pieces from
      the same sheets, the one that turns fewer pieces wins. Rotation is a
      permission the seller grants, not an outcome he asked for, and on plain
      melamine turning a piece buys nothing physical -- so it is only worth
      spending when it places pieces or saves a sheet, both of which outrank it
      here.
    """
    placed = [pp for layout in layouts for pp in layout.placed_pieces]
    return (
        len(unplaced),
        round(sum(layout.material.cost_per_unit for layout in layouts), 6),
        len(layouts),
        round(sum(layout.material.area for layout in layouts), 3),
        sum(1 for pp in placed if pp.rotated),
        round(sum(layout.waste_area for layout in layouts), 3),
        round(sum(c.length for layout in layouts for c in layout.cuts), 3),
    )


class _Searcher:
    """Holds the shared context of one ``optimize_bins`` run."""

    def __init__(
        self,
        specs: List[BinSpec],
        params: CuttingParameters,
        budget: SearchBudget,
        seed: int,
        min_rect_size: float,
        max_sheets: int,
        exact_config: Optional[ExactConfig] = None,
        exact_budget: Optional[ExactBudget] = None,
    ):
        self.specs = specs
        self.params = params
        self.budget = budget
        self.min_rect_size = min_rect_size
        self.max_sheets = max_sheets
        self.seed = seed
        self.exact = exact_config or ExactConfig()
        self.exact_budget = exact_budget or ExactBudget(
            self.exact.max_calls, self.exact.root_patience
        )
        self.rng = random.Random(1_000_003 * (seed + 1))
        # Exploration order of the greedy portfolio; the seed (request
        # ``variant`` or a restart offset) reshuffles it to surface
        # alternative solutions.
        self.portfolio: List[GreedyConfig] = list(GREEDY_PORTFOLIO)
        if seed:
            self.rng.shuffle(self.portfolio)
        # Native kernel, snapshotted so it cannot change mid-search. The
        # geometry is identical either way (see ``rust_backend``), so this
        # decides speed only — never the answer, and never the cache hash.
        self.rust = rust_backend.available()
        self._portfolio_codes = (
            rust_backend.encode_portfolio(self.portfolio) if self.rust else ()
        )
        # Memo of completion probes / failed LNS sub-searches, keyed by the
        # piece-type multiset (identical pools are interchangeable).
        self._probe_memo: Dict[tuple, Optional[BinFill]] = {}
        self._lns_failed: set = set()

    # ---- candidate generation -------------------------------------------

    def _strip_seeds(self, pool: Sequence[Piece], spec: BinSpec) -> List[float]:
        dims = sorted(
            {p.width for p in pool} | {p.height for p in pool if p.can_rotate},
            reverse=True,
        )
        usable_w = spec.width - self.params.left_trim - self.params.right_trim
        return [d for d in dims if d <= usable_w][:4]

    def gen_fills(self, pool: List[Piece], spec: BinSpec, tries: int) -> List[BinFill]:
        """Candidate fills of ``spec`` from ``pool``, deduped by piece multiset."""
        if self.rust:
            # Same loop, one FFI crossing instead of ~60 (see ``rust_backend``).
            return rust_backend.gen_fills(
                pool,
                spec,
                self.params,
                self._portfolio_codes,
                tries,
                self.min_rect_size,
            )

        fills: List[BinFill] = []
        seen = set()
        spent = 0

        def push(fill: Optional[BinFill]) -> None:
            if fill is None or not fill.placed:
                return
            sig = fill.type_signature()
            if sig in seen:
                return
            seen.add(sig)
            fills.append(fill)

        strip_budget = max(6, tries // 2)
        for horizontal in (False, True):
            for first_dim in [None] + self._strip_seeds(pool, spec):
                if spent >= strip_budget:
                    break
                push(
                    strip_fill(
                        pool,
                        spec,
                        self.params,
                        horizontal=horizontal,
                        first_dim=first_dim,
                        min_rect_size=self.min_rect_size,
                    )
                )
                spent += 1
        # Repeat-capped variants: spread near-perfect single-type strips across
        # boards instead of hoarding them into one (see ``strip_fill``).
        for horizontal in (False, True):
            for repeat_cap in (1, 2):
                if spent >= strip_budget + 4:
                    break
                push(
                    strip_fill(
                        pool,
                        spec,
                        self.params,
                        horizontal=horizontal,
                        max_repeat=repeat_cap,
                        min_rect_size=self.min_rect_size,
                    )
                )
                spent += 1

        for config in self.portfolio:
            if spent >= tries:
                break
            push(greedy_fill(pool, spec, self.params, config, self.min_rect_size))
            spent += 1
        return fills

    def _probe_fill(
        self, pool: List[Piece], spec: BinSpec, target: int
    ) -> Optional[BinFill]:
        """First probe constructor that packs the WHOLE pool into one ``spec``.

        The order is the contract: the first eight greedy configs of the
        (possibly reshuffled) portfolio, then the two strip orientations.
        Stopping at the first complete fill is equivalent to building all ten
        and scanning — the constructors share no state — and it is most of what
        makes the native path's single FFI crossing worth having.
        """
        if self.rust:
            return rust_backend.probe_fill(
                pool,
                spec,
                self.params,
                self._portfolio_codes[:8],
                target,
                self.min_rect_size,
            )

        for config in self.portfolio[:8]:
            fill = greedy_fill(pool, spec, self.params, config, self.min_rect_size)
            if fill is not None and len(fill.placed) == target:
                return fill
        for horizontal in (False, True):
            fill = strip_fill(
                pool,
                spec,
                self.params,
                horizontal=horizontal,
                min_rect_size=self.min_rect_size,
            )
            if fill is not None and len(fill.placed) == target:
                return fill
        return None

    def exact_fill(self, pool: Sequence[Piece], spec: BinSpec) -> Optional[BinFill]:
        """CP-SAT attempt at closing ``pool`` into ONE ``spec``; ``None`` if skipped.

        Every gate here exists to keep the exact model where it earns its cost:
        it is asked only about pools the heuristics just failed on, that are
        dense enough for the failure to be a *packing* problem rather than a
        capacity one, and only while the per-search call budget lasts.
        """
        cfg = self.exact
        if not cfg.enabled or not exact.is_available():
            return None
        if not pool or len(pool) > cfg.max_pieces:
            return None
        usable = _usable_area(spec, self.params)
        if usable <= 0:
            return None
        if sum(p.area for p in pool) < usable * cfg.min_fill_ratio:
            return None
        if not self.exact_budget.take():
            return None
        return exact.fits_one_bin(
            pool,
            spec,
            self.params,
            deterministic_time=cfg.deterministic_time,
            seed=abs(self.seed) % 1_000_000,
            min_rect_size=self.min_rect_size,
        )

    def exact_best_fill(
        self, pool: Sequence[Piece], spec: BinSpec
    ) -> Optional[BinFill]:
        """Densest exact fill of ``spec`` from ``pool`` (CP-SAT maximizing area).

        A candidate no greedy can match on tight jobs, but paid for with real
        optimization work — hence the first-move-only usage (of the main beam
        *and* of each LNS sub-search, which is where it most often turns two
        half-empty boards into one), the memo shared across restarts, and the
        separate ``root_deterministic_time`` allowance: this call never proves
        anything, so it spends every unit it is given (see ``ExactConfig``).
        """
        cfg = self.exact
        if not cfg.enabled or not cfg.seed_root_fill or not exact.is_available():
            return None
        if not pool or len(pool) > cfg.max_pieces:
            return None
        key = (_pool_signature(pool), spec.key, spec.width, spec.height)
        if key in self.exact_budget.root_memo:
            cached = self.exact_budget.root_memo[key]
            return _rebind_fill(cached, pool) if cached is not None else None
        if not self.exact_budget.take():
            return None
        best: Optional[BinFill] = None
        for transposed in (False, True):
            candidate = exact.solve_bin(
                pool,
                spec,
                self.params,
                require_all=False,
                transposed=transposed,
                deterministic_time=cfg.root_deterministic_time,
                seed=abs(self.seed) % 1_000_000,
                min_rect_size=self.min_rect_size,
            )
            if candidate is None or not candidate.placed:
                continue
            if best is None or candidate.used_area > best.used_area:
                best = candidate
        self.exact_budget.root_memo[key] = best
        return _rebind_fill(best, pool) if best is not None else None

    def open_specs(
        self, used_counts: Dict[int, int], rem_area: float
    ) -> List[Tuple[int, BinSpec]]:
        """Spec indices still available, half bins gated by remaining area."""
        out = []
        for i, spec in enumerate(self.specs):
            if spec.count is not None and used_counts.get(i, 0) >= spec.count:
                continue
            if (
                spec.half_board
                and rem_area > _usable_area(spec, self.params) * _HALF_GATE
            ):
                continue
            out.append((i, spec))
        return out

    def complete_probe(
        self, pool: List[Piece], used_counts: Dict[int, int]
    ) -> Optional[BinFill]:
        """Cheap test: does the whole ``pool`` fit ONE more bin? Which/cheapest?

        Runs a handful of diverse fills per open spec and keeps the cheapest
        spec that places everything. Memoized by pool type-multiset so the
        beam can probe every near-final candidate without burning the budget.
        """
        key = (_pool_signature(pool), tuple(sorted(used_counts.items())))
        if key in self._probe_memo:
            cached = self._probe_memo[key]
            # The memoized fill belongs to another pool with the same type
            # signature: re-bind it onto this pool's concrete pieces.
            return _rebind_fill(cached, pool) if cached is not None else None
        target = len(pool)
        rem_area = sum(p.area for p in pool)
        best: Optional[BinFill] = None
        for _idx, spec in sorted(
            self.open_specs(used_counts, 0.0),
            key=lambda item: (item[1].cost_per_unit, item[0]),
        ):
            if rem_area > _usable_area(spec, self.params):
                continue
            if best is not None and spec.cost_per_unit >= best.spec.cost_per_unit:
                break
            found = self._probe_fill(pool, spec, target)
            if found is None:
                # The heuristics gave up on a pool that still fits by area:
                # exactly the >=94% pack the audit measured them failing on.
                found = self.exact_fill(pool, spec)
            if found is not None:
                best = found
        self._probe_memo[key] = best
        return _rebind_fill(best, pool) if best is not None else None

    # ---- beam search -----------------------------------------------------

    def beam(
        self,
        pieces: List[Piece],
        used_counts: Optional[Dict[int, int]] = None,
        beam_width: Optional[int] = None,
        tries: Optional[int] = None,
        max_bins: Optional[int] = None,
        upper_objective: Optional[tuple] = None,
        root_exact: bool = False,
    ) -> Optional[_Solution]:
        """Beam search over partitions; returns the best complete solution.

        ``upper_objective`` is both a pruning bound and a contract: the search
        only ever returns something strictly better than it, so a caller can
        chain passes and never lose the incumbent. ``root_exact`` adds the
        solver's densest fill to the first move's candidates.
        """
        beam_width = beam_width or self.budget.beam_width
        tries = tries or self.budget.tries_per_board
        base_counts = dict(used_counts or {})
        max_usable = max(
            (_usable_area(s, self.params) for s in self.specs), default=0.0
        )
        # "Tight" pieces nearly span a bin's long axis: they only fit fresh
        # full-height gaps, so states that defer them are dead ends the area
        # lower bound can't see. The beam prefers states that consume them.
        long_axis = max(
            (
                max(
                    s.width - self.params.left_trim - self.params.right_trim,
                    s.height - self.params.top_trim - self.params.bottom_trim,
                )
                for s in self.specs
            ),
            default=0.0,
        )
        tight_threshold = 0.85 * long_axis

        def tight_count(pool: List[Piece]) -> int:
            return sum(1 for p in pool if max(p.width, p.height) >= tight_threshold)

        # state: (pool, fills, counts, cost)
        frontier = [(pieces, [], dict(base_counts), 0.0)]
        best: Optional[_Solution] = None
        best_obj = upper_objective
        depth_cap = min(
            self.max_sheets, max_bins if max_bins is not None else self.max_sheets
        )

        def register(candidate: _Solution) -> None:
            nonlocal best, best_obj
            if best_obj is None or candidate.objective() < best_obj:
                best = candidate
                best_obj = candidate.objective()

        # Does the WHOLE pool close in a single bin? Cheap (memoized) and the
        # move that lets LNS collapse two half-empty boards into one.
        single = self.complete_probe(pieces, base_counts)
        if single is not None:
            register(_Solution(fills=[single], unplaced=[]))

        for depth in range(depth_cap):
            if not frontier:
                break
            successors: Dict[tuple, tuple] = {}
            # States opened by an exact fill get a guaranteed beam slot. Their
            # f-score is often mediocre — a very dense first board leaves a
            # residual whose lower bound looks no better — so plain ranking
            # evicts them at depth 0 and the whole point of the solver is lost.
            reserved: set = set()
            for pool, fills, counts, cost in frontier:
                rem_area = sum(p.area for p in pool)
                for spec_idx, spec in self.open_specs(counts, rem_area):
                    candidates = [
                        (fill, False) for fill in self.gen_fills(pool, spec, tries)
                    ]
                    if root_exact and depth == 0:
                        # Root only: one densest-possible fill per bin type,
                        # a partition no sort-and-place order would produce.
                        densest = self.exact_best_fill(pool, spec)
                        if densest is not None:
                            candidates.append((densest, True))
                    for fill, from_exact in candidates:
                        placed_ids = set(fill.placed_ids())
                        new_pool = [p for p in pool if p.id not in placed_ids]
                        new_fills = fills + [fill]
                        new_counts = dict(counts)
                        new_counts[spec_idx] = new_counts.get(spec_idx, 0) + 1
                        new_cost = cost + spec.cost_per_unit
                        if not new_pool:
                            register(_Solution(fills=new_fills, unplaced=[]))
                            continue
                        new_rem = rem_area - fill.used_area
                        if new_rem <= max_usable and (
                            max_bins is None or len(new_fills) < max_bins
                        ):
                            # Endgame lookahead: can the whole tail close in
                            # one more bin? This is what turns "5 boards +
                            # orphans" states into complete 5-board solutions
                            # before pruning can drop them.
                            closing = self.complete_probe(new_pool, new_counts)
                            if closing is not None:
                                register(
                                    _Solution(fills=new_fills + [closing], unplaced=[])
                                )
                        lb_cost = new_cost + _cost_lower_bound(
                            new_rem, self.specs, self.params
                        )
                        bins_lb = _bins_lower_bound(new_rem, self.specs, self.params)
                        if best_obj is not None and (
                            round(lb_cost, 6),
                            len(new_fills) + bins_lb,
                        ) > (best_obj[0], best_obj[1]):
                            continue
                        key = (
                            _pool_signature(new_pool),
                            tuple(sorted(new_counts.items())),
                        )
                        f_score = (
                            round(lb_cost, 6),
                            len(new_fills) + bins_lb,
                            tight_count(new_pool),
                            round(new_rem, 3),
                        )
                        if from_exact:
                            reserved.add(key)
                        prev = successors.get(key)
                        if prev is None or f_score < prev[0]:
                            successors[key] = (
                                f_score,
                                new_pool,
                                new_fills,
                                new_counts,
                                new_cost,
                            )
            ranked = sorted(successors.items(), key=lambda kv: (kv[1][0], kv[0]))
            kept = ranked[:beam_width]
            kept_keys = {key for key, _ in kept}
            kept += [
                item
                for item in ranked[beam_width:]
                if item[0] in reserved and item[0] not in kept_keys
            ]
            frontier = [
                (pool, fills, counts, cost)
                for _key, (_f, pool, fills, counts, cost) in kept
            ]
        return best

    # ---- LNS repair ------------------------------------------------------

    def lns(
        self,
        solution: _Solution,
        lb: float,
        total_area: float,
        root_exact: bool = False,
    ) -> _Solution:
        """Ruin & recreate over the worst bins; accepts strict improvements.

        ``root_exact`` lets the sub-search open the ruined pool with the
        solver's densest fill. This is where orphan absorption actually
        happens: two half-empty boards get ruined and the exact packer decides
        whether their contents collapse into one — and, because every round
        ruins a different combo, it is also where nearly every solver call of a
        run is born. So it is here that the stagnation cutoff is applied and
        scored (see ``ExactBudget``).
        """
        best = solution
        stale = 0
        for iteration in range(self.budget.iterations):
            # Same proof as the caller's: with free bins a cost bound of 0 is
            # met by anything, and this loop is where orphan absorption happens
            # — exactly what a pool of retazos needs.
            if _proven_optimal(best, lb, total_area, self.specs, self.params):
                break
            n = len(best.fills)
            if n < 2:
                break
            order = sorted(range(n), key=lambda i: -best.fills[i].waste_area)
            combos: List[Tuple[int, ...]] = []
            for a in range(min(n, 4)):
                for b in range(a + 1, min(n, 5)):
                    combos.append((order[a], order[b]))
            if n >= 3:
                combos.append(tuple(order[:3]))
                combos.append(tuple(order[:2]) + (order[-1],))
            if stale >= len(combos):
                # Full cycle without improvement: this neighborhood is
                # exhausted; more iterations would just replay memoized
                # failures.
                break
            combo = combos[iteration % len(combos)]

            kept = [f for i, f in enumerate(best.fills) if i not in combo]
            ruined = [best.fills[i] for i in combo]
            pool = [pp.piece for f in ruined for pp in f.placed]
            counts: Dict[int, int] = {}
            spec_index = {id(s): i for i, s in enumerate(self.specs)}
            for f in kept:
                idx = spec_index[id(f.spec)]
                counts[idx] = counts.get(idx, 0) + 1

            ruined_cost = sum(f.spec.cost_per_unit for f in ruined)
            ruined_waste = sum(f.waste_area for f in ruined)
            # Upper bound for the sub-search: strictly better than the ruined
            # part (cost first, then fewer bins/waste).
            upper = (
                round(ruined_cost, 6),
                len(ruined),
                0,
                round(ruined_waste, 3),
                round(sum(c.length for f in ruined for c in f.cuts), 3),
            )
            memo_key = (
                _pool_signature(pool),
                tuple(sorted(counts.items())),
                upper[:2],
            )
            if memo_key in self._lns_failed:
                stale += 1
                # A replayed failure is not evidence about the solver: no call
                # was made, so it must not count against its patience.
                continue
            # This is the only place a solver ask can be scored: the sub-search
            # either beats the incumbent or it doesn't, and both the ruined pool
            # and the answer are right here.
            used_root = root_exact and self.exact_budget.root_allowed()
            sub = self.beam(
                pool,
                used_counts=counts,
                beam_width=max(3, self.budget.beam_width // 2),
                tries=max(8, self.budget.tries_per_board // 2),
                max_bins=len(combo),
                upper_objective=upper,
                root_exact=used_root,
            )
            if sub is None:
                self._lns_failed.add(memo_key)
                stale += 1
                if used_root:
                    self.exact_budget.note_root(False)
                continue
            candidate = _Solution(fills=kept + sub.fills, unplaced=best.unplaced)
            improved = candidate.objective() < best.objective()
            if improved:
                best = candidate
                stale = 0
            else:
                self._lns_failed.add(memo_key)
                stale += 1
            if used_root:
                # Deliberately generous attribution: a round that used the
                # solver and improved counts as a hit even if the winning fill
                # came from a greedy. Erring towards keeping the solver alive is
                # the safe direction — it costs seconds, the other costs boards.
                self.exact_budget.note_root(improved)
        return best

    # ---- half-board downgrade -------------------------------------------

    def half_downgrade(self, solution: _Solution) -> _Solution:
        """Swaps any full-bin fill whose content fits its cheaper half sibling."""
        halves: Dict[str, BinSpec] = {}
        for spec in self.specs:
            if spec.half_board:
                halves[spec.key] = spec
        if not halves:
            return solution
        fills = list(solution.fills)
        for i, fill in enumerate(fills):
            if fill.spec.half_board:
                continue
            half = halves.get(fill.spec.key)
            if half is None or half.cost_per_unit >= fill.spec.cost_per_unit:
                continue
            pool = [pp.piece for pp in fill.placed]
            target = len(pool)
            swapped = None
            for candidate in self.gen_fills(pool, half, self.budget.tries_per_board):
                if len(candidate.placed) == target:
                    swapped = candidate
                    break
            if swapped is None:
                # Half boards are where packs get tightest by construction —
                # the exact model earns its keep here more than anywhere.
                swapped = self.exact_fill(pool, half)
            if swapped is not None:
                fills[i] = swapped
        return _Solution(fills=fills, unplaced=solution.unplaced)


def downgrade_layout_to_half(
    layout: CuttingLayout,
    half_spec: BinSpec,
    cutting_params: CuttingParameters = None,
    budget: SearchBudget = None,
    seed: int = 0,
    min_rect_size: float = 0.1,
    exact_config: Optional[ExactConfig] = None,
) -> Optional[CuttingLayout]:
    """Re-packs a full-sheet layout into its half sibling if everything fits.

    Post-hoc variant of the search's internal half downgrade, for callers that
    orchestrate sheets themselves (the offcut pool). Returns ``None`` when the
    content doesn't fit the half board.
    """
    pool = [pp.piece for pp in layout.placed_pieces]
    if not pool:
        return None
    params = cutting_params or CuttingParameters()
    searcher = _Searcher(
        specs=[half_spec],
        params=params,
        budget=budget or SearchBudget.scaled(len(pool)),
        seed=seed,
        min_rect_size=min_rect_size,
        max_sheets=1,
        exact_config=exact_config,
    )
    fills = searcher.gen_fills(pool, half_spec, searcher.budget.tries_per_board)
    fit = next((f for f in fills if len(f.placed) == len(pool)), None)
    if fit is None:
        fit = searcher.exact_fill(pool, half_spec)
    if fit is None:
        return None
    return CuttingLayout(
        material=half_spec.to_material(),
        placed_pieces=fit.placed,
        remainders=fit.remainders,
        sheet_number=layout.sheet_number,
        cuts=fit.cuts,
    )


def _sequential_fill(
    pieces: List[Piece],
    specs: Sequence[BinSpec],
    params: CuttingParameters,
    config: GreedyConfig,
    min_rect_size: float,
    max_sheets: int,
) -> Tuple[List[BinFill], List[Piece]]:
    """Legacy behavior: one bin at a time, single greedy config, specs in order.

    Finite ``count``s are honored; half-board specs are skipped (they enter via
    the search / the downgrade pass, never as a baseline sheet) unless no full
    spec exists at all.
    """
    fills: List[BinFill] = []
    remaining = list(pieces)
    counts: Dict[int, int] = {}
    has_full = any(not s.half_board for s in specs)
    for _ in range(max_sheets):
        if not remaining:
            break
        fill = None
        for idx, spec in enumerate(specs):
            if has_full and spec.half_board:
                continue
            if spec.count is not None and counts.get(idx, 0) >= spec.count:
                continue
            backend = rust_backend if rust_backend.available() else _py_constructors
            fill = backend.greedy_fill(remaining, spec, params, config, min_rect_size)
            if fill is not None:
                counts[idx] = counts.get(idx, 0) + 1
                break
        if fill is None:
            break
        placed_ids = set(fill.placed_ids())
        remaining = [p for p in remaining if p.id not in placed_ids]
        fills.append(fill)
    return fills, remaining


def _to_layouts(fills: Sequence[BinFill]) -> List[CuttingLayout]:
    """Materializes fills as numbered layouts, in opening order."""
    return [
        CuttingLayout(
            material=fill.spec.to_material(),
            placed_pieces=fill.placed,
            remainders=fill.remainders,
            sheet_number=i + 1,
            cuts=fill.cuts,
        )
        for i, fill in enumerate(fills)
    ]


def _plan_cost(layouts: Sequence[CuttingLayout]) -> float:
    return sum(layout.material.cost_per_unit for layout in layouts)


def _emptiest_sheet(layouts: Sequence[CuttingLayout]) -> float:
    """Fill of the least-used sheet -- the symptom the repair reacts to."""
    return min(
        (
            layout.used_area / layout.material.area
            for layout in layouts
            if layout.material.area > 0
        ),
        default=1.0,
    )


def _recut(
    hint: Sequence[CuttingLayout],
    params: CuttingParameters,
    *,
    budget: SearchBudget,
    min_rect_size: float,
    exact_config: ExactConfig,
) -> Optional[List[CuttingLayout]]:
    """Re-packs every board of a relaxed plan at the REAL parameters.

    All or nothing, and each board into a sheet of exactly the kind the hint
    used: a half that no longer closes must never be quietly promoted to a full
    one, or the repair would return a plan costing more than it claims. Returns
    ``None`` the moment one board fails, because a partition is only a
    certificate if every part of it is.
    """
    rebuilt: List[CuttingLayout] = []
    for layout in hint:
        spec = BinSpec(
            key=layout.material.id,
            width=layout.material.width,
            height=layout.material.height,
            thickness=layout.material.thickness,
            cost_per_unit=layout.material.cost_per_unit,
            half_board=layout.material.half_board,
        )
        group = [placed.piece for placed in layout.placed_pieces]
        packed, spilled = optimize_bins(
            group,
            [spec],
            cutting_params=params,
            budget=SearchBudget.scaled(
                len(group),
                tries_per_board=budget.tries_per_board,
                iterations=budget.iterations,
            ),
            min_rect_size=min_rect_size,
            exact_config=exact_config,
            _repair=False,
        )
        if spilled or len(packed) != 1:
            return None
        rebuilt.append(packed[0])
    return [
        CuttingLayout(
            material=layout.material,
            placed_pieces=layout.placed_pieces,
            remainders=layout.remainders,
            sheet_number=i + 1,
            cuts=layout.cuts,
        )
        for i, layout in enumerate(rebuilt)
    ]


def _relaxed_kerf_repair(
    pieces: List[Piece],
    bins: List[BinSpec],
    params: CuttingParameters,
    incumbent: Sequence[CuttingLayout],
    *,
    budget: SearchBudget,
    seed: int,
    min_rect_size: float,
    max_sheets: int,
    exact_config: ExactConfig,
) -> Optional[List[CuttingLayout]]:
    """Solve a thinner-blade relaxation, then re-cut it at the real blade.

    Returns a cheaper, fully re-validated plan, or ``None`` to keep the
    incumbent. See ``_REPAIR_FILL_GATE`` for why this is gated and what it buys.
    """
    if _emptiest_sheet(incumbent) >= _REPAIR_FILL_GATE:
        return None
    target = _plan_cost(incumbent)
    for delta in _REPAIR_KERF_DELTAS:
        if params.kerf - delta < 0:
            continue
        relaxed = CuttingParameters(
            kerf=params.kerf - delta,
            top_trim=params.top_trim,
            bottom_trim=params.bottom_trim,
            left_trim=params.left_trim,
            right_trim=params.right_trim,
        )
        hint, spilled = optimize_bins(
            pieces,
            bins,
            cutting_params=relaxed,
            budget=budget,
            seed=seed,
            min_rect_size=min_rect_size,
            max_sheets=max_sheets,
            exact_config=exact_config,
            _repair=False,
        )
        # A relaxation that strands a piece says nothing, and one that does not
        # beat the incumbent is not worth re-cutting.
        if spilled or _plan_cost(hint) >= target - 1e-6:
            continue
        rebuilt = _recut(
            hint,
            params,
            budget=budget,
            min_rect_size=min_rect_size,
            exact_config=exact_config,
        )
        if rebuilt is not None:
            return rebuilt
    return None


def optimize_bins(
    pieces: List[Piece],
    bins: List[BinSpec],
    cutting_params: CuttingParameters = None,
    strategy: PackingStrategy = PackingStrategy.MAX_EFFICIENCY,
    budget: SearchBudget = None,
    seed: int = 0,
    min_rect_size: float = 0.1,
    max_sheets: int = 100,
    exact_config: ExactConfig = None,
    _repair: bool = True,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Packs ``pieces`` into the cheapest set of bins drawn from ``bins``.

    Returns ``(layouts, unplaced)``; ``unplaced`` holds pieces that fit no bin
    at all (same silent contract as the legacy multi-sheet optimizer). Layouts
    are numbered sequentially in opening order.

    ``_repair`` is private and exists only so the relaxed-kerf repair can call
    back in without recursing: the relaxed pass and each single-board
    re-validation run with it off.
    """
    if not pieces or not bins:
        return [], []
    params = cutting_params or CuttingParameters()
    expanded = expand_pieces(pieces)
    budget = budget or SearchBudget.scaled(len(expanded))

    placeable: List[Piece] = []
    unplaced: List[Piece] = []
    for piece in expanded:
        if any(_piece_fits_spec(piece, s, params) for s in bins):
            placeable.append(piece)
        else:
            unplaced.append(piece)

    exact_config = exact_config or ExactConfig()
    exact_budget = ExactBudget(exact_config.max_calls, exact_config.root_patience)
    searcher = _Searcher(
        specs=list(bins),
        params=params,
        budget=budget,
        seed=seed,
        min_rect_size=min_rect_size,
        max_sheets=max_sheets,
        exact_config=exact_config,
        exact_budget=exact_budget,
    )

    solution: Optional[_Solution] = None
    if placeable:
        legacy_fills, legacy_rest = _sequential_fill(
            placeable,
            bins,
            params,
            _LEGACY_CONFIG[strategy],
            min_rect_size,
            max_sheets,
        )
        solution = _Solution(fills=legacy_fills, unplaced=legacy_rest)

        if strategy == PackingStrategy.MAX_EFFICIENCY and not legacy_rest:
            total_area = sum(p.area for p in placeable)
            lb = _cost_lower_bound(total_area, bins, params)

            def proven(candidate: _Solution) -> bool:
                return _proven_optimal(candidate, lb, total_area, bins, params)

            def pipeline(baseline: _Solution, root_exact: bool) -> _Solution:
                """One beam + repair run, guaranteed no worse than ``baseline``."""
                out = baseline
                improved = searcher.beam(
                    placeable,
                    upper_objective=out.objective(),
                    root_exact=root_exact,
                )
                if improved is not None:
                    out = improved
                if not proven(out):
                    out = searcher.lns(out, lb, total_area, root_exact=root_exact)
                return out

            if not proven(solution):
                # The exact-seeded run goes first: when the solver's dense first
                # board is the right opening it usually proves the lower bound
                # outright, and the heuristic run below is skipped entirely.
                solution = pipeline(solution, root_exact=True)
            if not proven(solution):
                # A denser first board is NOT always the globally cheaper
                # opening, and the beam only keeps so many states. So unless
                # optimality is already proven, the pure-heuristic run happens
                # too and the better of the two wins — that is what makes the
                # solver strictly additive: it can never lose a board the
                # heuristics would have saved. The two runs share the searcher's
                # memos, so the second is much cheaper than the first.
                alternative = pipeline(
                    _Solution(fills=legacy_fills, unplaced=legacy_rest),
                    root_exact=False,
                )
                if alternative.objective() < solution.objective():
                    solution = alternative
            # Seeded restarts: a different exploration order often lands a
            # different partition; keep the best. Bounded and deterministic.
            # Each restart is bounded by the incumbent, so it can only help,
            # and which openings it tries is ``_RESTART_FLAVORS``. The proven
            # lower bound stops the whole thing early, which is why the wins
            # are also the fastest cases.
            #
            # When it is NOT provable, though, that exit never fires: the area
            # lower bound ignores geometry, so on most pools it is unreachable
            # and the engine used to run every restart to the end for nothing.
            # Measured on a 153-piece pool: identical cost at every budget, with
            # the restarts adding ~30s of pure waste. Hence the patience
            # counter below — restarts that find nothing are evidence the
            # neighborhood is exhausted, not that the next seed will differ.
            # It is counted in *restarts*, never in wall clock, so the search
            # stays deterministic and the payload cache stays valid.
            restarts = min(3, budget.iterations // 10)
            stale_restarts = 0
            for k in range(1, restarts + 1):
                if stale_restarts >= _RESTART_PATIENCE:
                    break
                alt = _Searcher(
                    specs=list(bins),
                    params=params,
                    budget=budget,
                    seed=seed + 7919 * k,
                    min_rect_size=min_rect_size,
                    max_sheets=max_sheets,
                    exact_config=exact_config,
                    exact_budget=exact_budget,
                )
                before = solution.objective()
                for root_exact in _RESTART_FLAVORS:
                    if proven(solution):
                        break
                    improved = alt.beam(
                        placeable,
                        upper_objective=solution.objective(),
                        root_exact=root_exact,
                    )
                    if improved is not None:
                        solution = improved
                    refined = alt.lns(solution, lb, total_area, root_exact=root_exact)
                    if refined.objective() < solution.objective():
                        solution = refined
                # A whole restart that failed to beat the incumbent counts
                # against the patience budget; any improvement resets it.
                stale_restarts = (
                    0 if solution.objective() < before else stale_restarts + 1
                )
        solution = searcher.half_downgrade(solution)

    if solution is None:
        return [], unplaced

    layouts = _to_layouts(solution.fills)

    if _repair and placeable and strategy == PackingStrategy.MAX_EFFICIENCY:
        repaired = _relaxed_kerf_repair(
            placeable,
            bins,
            params,
            layouts,
            budget=budget,
            seed=seed,
            min_rect_size=min_rect_size,
            max_sheets=max_sheets,
            exact_config=exact_config,
        )
        if repaired is not None:
            layouts = repaired

    return layouts, unplaced + solution.unplaced


# Bin orders the max-yield pass tries. Four cheap passes over the same
# candidate generator: which retazo gets filled first is a real decision when
# the stock is finite, and no single rule wins everywhere -- ``request`` is the
# order the seller listed them, ``smallest`` saves the big sheet for last,
# ``largest`` gets the awkward pieces placed while there is room, and
# ``best_next`` re-decides at every sheet.
_MAX_YIELD_BIN_ORDERS: Tuple[str, ...] = ("request", "smallest", "largest", "best_next")


def _yield_key(fill: BinFill) -> tuple:
    """Ranks candidate fills of ONE bin when the stock has run out (lower first).

    Area before piece count, deliberately: whatever does not fit the client's
    retazo gets cut from a board he buys, and a board is priced by area. Chasing
    the count instead fills the sheet with the small pieces and strands the big
    ones -- more rows cut, more material to purchase.

    ``priority`` is not a term here: it already leads every comparator in
    ``SORT_KEYS``, so a high-priority piece is offered to each constructor
    first, and turning it into a hard constraint at this level would waste
    retazo whenever the priority pieces happen to pack badly.
    """
    return (
        -round(fill.used_area, 3),
        -len(fill.placed),
        sum(1 for pp in fill.placed if pp.rotated),
    )


def _best_yield_fill(
    searcher: "_Searcher", pool: List[Piece], spec: BinSpec, tries: int
) -> Optional[BinFill]:
    """Densest fill of one bin, over the whole candidate portfolio.

    No new constructor: ``gen_fills`` is the same portfolio x strip generator the
    beam uses, native kernel included, and the choice between its candidates is
    ``_yield_key``.

    **CP-SAT is deliberately not consulted here**, and the number is the reason.
    ``exact_best_fill`` maximizes placed area, so it looks like the obvious
    candidate to add; measured over twelve finite pools it places 2.7% more
    pieces (382 vs 372) for **82x the time** -- 133s against 1.6s, worst pool
    24.4s against 0.8s -- on a path that answers in milliseconds today and that a
    seller hits with the client at the counter. Metering it the way the beam does
    (opening sheet only, so the four bin orders share one memoized answer) does
    not rescue the trade: it lands back on 372 pieces, exactly the no-solver
    yield, while still costing 43s. The 10 pieces come precisely from the
    mid-pass calls, i.e. from the shape that cannot be afforded. Same conclusion
    ``ExactConfig`` reached for the beam, reached again here with its own numbers.
    """
    best: Optional[Tuple[tuple, BinFill]] = None
    candidates = searcher.gen_fills(pool, spec, tries)
    for index, fill in enumerate(candidates):
        key = (_yield_key(fill), index)
        if best is None or key < best[0]:
            best = (key, fill)
    return best[1] if best is not None else None


def _bin_order(
    specs: Sequence[BinSpec], params: CuttingParameters, order: str
) -> List[int]:
    """Spec indices in the visiting order named by ``order`` (stable)."""
    indices = list(range(len(specs)))
    if order == "smallest":
        return sorted(indices, key=lambda i: (_usable_area(specs[i], params), i))
    if order == "largest":
        return sorted(indices, key=lambda i: (-_usable_area(specs[i], params), i))
    return indices


def _max_yield_pass(
    searcher: "_Searcher",
    pieces: List[Piece],
    specs: Sequence[BinSpec],
    params: CuttingParameters,
    order: str,
    tries: int,
) -> Tuple[List[BinFill], List[Piece]]:
    """One sequential pass: open sheets until nothing more can be cut."""
    remaining = list(pieces)
    used: Dict[int, int] = {}
    fills: List[BinFill] = []
    has_full = any(not spec.half_board for spec in specs)
    visiting = _bin_order(specs, params, order)

    def available(i: int) -> bool:
        spec = specs[i]
        if has_full and spec.half_board:
            return False
        return spec.count is None or used.get(i, 0) < spec.count

    for _ in range(searcher.max_sheets):
        if not remaining:
            break
        chosen: Optional[Tuple[tuple, int, BinFill]] = None
        if order == "best_next":
            # Re-decide every sheet: take the bin that cuts the most, breaking
            # ties towards the SMALLER one so the big retazo survives for the
            # pieces that will need it.
            for i in visiting:
                if not available(i):
                    continue
                fill = _best_yield_fill(searcher, remaining, specs[i], tries)
                if fill is None:
                    continue
                key = (_yield_key(fill), _usable_area(specs[i], params), i)
                if chosen is None or key < chosen[0]:
                    chosen = (key, i, fill)
        else:
            # The fixed orders commit to the first bin that cuts anything; the
            # order itself is the decision, so there is nothing left to rank.
            for i in visiting:
                if not available(i):
                    continue
                fill = _best_yield_fill(searcher, remaining, specs[i], tries)
                if fill is not None:
                    chosen = ((), i, fill)
                    break
        if chosen is None:
            break
        _, index, fill = chosen
        used[index] = used.get(index, 0) + 1
        placed_ids = set(fill.placed_ids())
        remaining = [piece for piece in remaining if piece.id not in placed_ids]
        fills.append(fill)
    return fills, remaining


def fill_finite_bins_max_yield(
    pieces: List[Piece],
    bins: List[BinSpec],
    cutting_params: CuttingParameters = None,
    *,
    budget: SearchBudget = None,
    seed: int = 0,
    min_rect_size: float = 0.1,
    max_sheets: int = 100,
    exact_config: ExactConfig = None,
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Cuts as much of ``pieces`` as a FINITE bin set can hold.

    A different question from ``optimize_bins``, which asks what the cheapest
    complete plan costs. It cannot answer this one: ``register`` only ever builds
    complete solutions, so the beam is structurally unable to rank a plan that
    strands a piece, and the whole search is skipped the moment the sequential
    baseline strands one -- which is exactly the job where the packing IS the
    product, because the client is paying for the decision of what comes out of
    his own retazos.

    No new heuristic. Pre-order 7 (two client retazos, 20 pieces of 200x700) is
    cut 15 -> 18 by ``GreedyConfig(sort="area", split=LONGER_AXIS)``, a point of
    the portfolio the engine has generated all along and never consulted here.

    Determinism: the bin orders are a constant tuple, ``gen_fills`` is already
    ordered, and the effort is counted in orders x sheets x ``tries`` plus the
    shared ``ExactBudget`` -- never in wall clock.
    """
    if not pieces or not bins:
        return [], []
    params = cutting_params or CuttingParameters()
    expanded = expand_pieces(pieces)
    budget = budget or SearchBudget.scaled(len(expanded))
    # Carried only because ``_Searcher`` is constructed with one: this pass never
    # asks the solver anything (see ``_best_yield_fill``).
    exact_config = exact_config or ExactConfig()

    placeable: List[Piece] = []
    unplaced: List[Piece] = []
    for piece in expanded:
        if any(_piece_fits_spec(piece, spec, params) for spec in bins):
            placeable.append(piece)
        else:
            unplaced.append(piece)
    if not placeable:
        return [], unplaced

    searcher = _Searcher(
        specs=list(bins),
        params=params,
        budget=budget,
        seed=seed,
        min_rect_size=min_rect_size,
        max_sheets=max_sheets,
        exact_config=exact_config,
    )

    best: Optional[Tuple[tuple, List[CuttingLayout], List[Piece]]] = None
    for order in _MAX_YIELD_BIN_ORDERS:
        fills, rest = _max_yield_pass(
            searcher, placeable, bins, params, order, budget.tries_per_board
        )
        layouts = _to_layouts(fills)
        score = finite_plan_objective(layouts, rest)
        if best is None or score < best[0]:
            best = (score, layouts, rest)

    _, layouts, rest = best
    return layouts, unplaced + rest


class MultiSheetGuillotineOptimizer:
    """Back-compat wrapper: repeated single template = one infinite bin."""

    def __init__(
        self,
        material_template,
        cutting_params: CuttingParameters = None,
        split_rule: SplitRule = None,
        max_sheets: int = 100,
        min_rect_size: float = 0.1,
        strategy: PackingStrategy = PackingStrategy.MAX_EFFICIENCY,
        budget: SearchBudget = None,
        seed: int = 0,
        exact_config: ExactConfig = None,
    ):
        self.material_template = material_template
        self.strategy = strategy
        # Kept for API compatibility: the search explores many split rules, but
        # the derived/explicit one still drives the legacy sequential pass.
        self.split_rule = (
            split_rule
            if split_rule is not None
            else PACKING_STRATEGY_SPLIT_RULE[strategy]
        )
        self.cutting_params = cutting_params
        self.max_sheets = max_sheets
        self.min_rect_size = min_rect_size
        self.budget = budget
        self.seed = seed
        self.exact_config = exact_config
        self.layouts: List[CuttingLayout] = []

    def optimize(self, pieces: List[Piece]) -> Tuple[List[CuttingLayout], List[Piece]]:
        spec = BinSpec(
            key=self.material_template.id,
            width=self.material_template.width,
            height=self.material_template.height,
            thickness=self.material_template.thickness,
            cost_per_unit=self.material_template.cost_per_unit,
        )
        layouts, unplaced = optimize_bins(
            pieces,
            [spec],
            cutting_params=self.cutting_params,
            strategy=self.strategy,
            budget=self.budget,
            seed=self.seed,
            min_rect_size=self.min_rect_size,
            max_sheets=self.max_sheets,
            exact_config=self.exact_config,
        )
        self.layouts = layouts
        return layouts, unplaced
