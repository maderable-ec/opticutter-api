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

from src.cutting import exact
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
# 3 = CP-SAT exact endgame (``exact.py``); also bump this when the pinned
# ortools version moves, since a solver upgrade can return a different solution.
ENGINE_VERSION = 3

# A half bin is only worth opening near the end of a job: gate it by remaining
# area so early states don't waste decodes on fills the cost objective would
# discard anyway.
_HALF_GATE = 1.25

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
    """

    enabled: bool = True
    max_pieces: int = 120
    max_calls: int = 40
    deterministic_time: float = 6.0
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
    """Call allowance + memo shared by every searcher of one ``optimize_bins`` run.

    Seeded restarts build fresh ``_Searcher``s; a per-searcher counter would let
    them multiply the worst-case solver time by the restart count, and they all
    re-ask the same root question. One shared allowance keeps the ceiling flat;
    the memo (keyed by piece-type multiset) keeps restarts from paying twice.
    Counting calls, never seconds, keeps the whole thing deterministic.
    """

    def __init__(self, max_calls: int):
        self.remaining = max_calls
        self.root_memo: Dict[tuple, Optional[BinFill]] = {}

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


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
        return (
            round(self.cost(), 6),
            len(self.fills),
            len(self.unplaced),
            round(self.waste(), 3),
            round(self.cut_length(), 3),
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
        self.exact_budget = exact_budget or ExactBudget(self.exact.max_calls)
        self.rng = random.Random(1_000_003 * (seed + 1))
        # Exploration order of the greedy portfolio; the seed (request
        # ``variant`` or a restart offset) reshuffles it to surface
        # alternative solutions.
        self.portfolio: List[GreedyConfig] = list(GREEDY_PORTFOLIO)
        if seed:
            self.rng.shuffle(self.portfolio)
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
        half-empty boards into one) and the memo shared across restarts.
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
                deterministic_time=cfg.deterministic_time,
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
            probes: List[Optional[BinFill]] = [
                greedy_fill(pool, spec, self.params, config, self.min_rect_size)
                for config in self.portfolio[:8]
            ]
            for horizontal in (False, True):
                probes.append(
                    strip_fill(
                        pool,
                        spec,
                        self.params,
                        horizontal=horizontal,
                        min_rect_size=self.min_rect_size,
                    )
                )
            found = None
            for fill in probes:
                if fill is not None and len(fill.placed) == target:
                    found = fill
                    break
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
        self, solution: _Solution, lb: float, root_exact: bool = False
    ) -> _Solution:
        """Ruin & recreate over the worst bins; accepts strict improvements.

        ``root_exact`` lets the sub-search open the ruined pool with the
        solver's densest fill. This is where orphan absorption actually
        happens: two half-empty boards get ruined and the exact packer decides
        whether their contents collapse into one.
        """
        best = solution
        stale = 0
        for iteration in range(self.budget.iterations):
            if best.cost() <= lb + 1e-6:
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
                continue
            sub = self.beam(
                pool,
                used_counts=counts,
                beam_width=max(3, self.budget.beam_width // 2),
                tries=max(8, self.budget.tries_per_board // 2),
                max_bins=len(combo),
                upper_objective=upper,
                root_exact=root_exact,
            )
            if sub is None:
                self._lns_failed.add(memo_key)
                stale += 1
                continue
            candidate = _Solution(fills=kept + sub.fills, unplaced=best.unplaced)
            if candidate.objective() < best.objective():
                best = candidate
                stale = 0
            else:
                self._lns_failed.add(memo_key)
                stale += 1
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
            fill = greedy_fill(remaining, spec, params, config, min_rect_size)
            if fill is not None:
                counts[idx] = counts.get(idx, 0) + 1
                break
        if fill is None:
            break
        placed_ids = set(fill.placed_ids())
        remaining = [p for p in remaining if p.id not in placed_ids]
        fills.append(fill)
    return fills, remaining


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
) -> Tuple[List[CuttingLayout], List[Piece]]:
    """Packs ``pieces`` into the cheapest set of bins drawn from ``bins``.

    Returns ``(layouts, unplaced)``; ``unplaced`` holds pieces that fit no bin
    at all (same silent contract as the legacy multi-sheet optimizer). Layouts
    are numbered sequentially in opening order.
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
    exact_budget = ExactBudget(exact_config.max_calls)
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
                if out.cost() > lb + 1e-6:
                    out = searcher.lns(out, lb, root_exact=root_exact)
                return out

            if solution.cost() > lb + 1e-6:
                # The exact-seeded run goes first: when the solver's dense first
                # board is the right opening it usually proves the lower bound
                # outright, and the heuristic run below is skipped entirely.
                solution = pipeline(solution, root_exact=True)
            if solution.cost() > lb + 1e-6:
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
            # Both flavors run for the same reason the two pipelines above do —
            # the solver-seeded opening and the heuristic one each find
            # partitions the other misses — and every restart is bounded by the
            # incumbent, so extra flavors can only help. The proven lower bound
            # stops the whole thing early, which is why the wins are also the
            # fastest cases.
            restarts = min(3, budget.iterations // 10)
            for k in range(1, restarts + 1):
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
                for root_exact in (False, True):
                    if solution.cost() <= lb + 1e-6:
                        break
                    improved = alt.beam(
                        placeable,
                        upper_objective=solution.objective(),
                        root_exact=root_exact,
                    )
                    if improved is not None:
                        solution = improved
                    refined = alt.lns(solution, lb, root_exact=root_exact)
                    if refined.objective() < solution.objective():
                        solution = refined
        solution = searcher.half_downgrade(solution)

    if solution is None:
        return [], unplaced

    layouts = [
        CuttingLayout(
            material=fill.spec.to_material(),
            placed_pieces=fill.placed,
            remainders=fill.remainders,
            sheet_number=i + 1,
            cuts=fill.cuts,
        )
        for i, fill in enumerate(solution.fills)
    ]
    return layouts, unplaced + solution.unplaced


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
