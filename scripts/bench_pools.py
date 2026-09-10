"""Cost benchmark for pools of a catalog board + the client's retazos.

``bench_battery.py`` builds only ``BinSpec(count=None)`` and calls
``optimize_bins`` directly, so it never enters ``src/modules/optimizations/
pool.py`` at all — which is precisely what makes its digests the cheap proof
that a change confined to that file did not leak. The flip side is that nothing
measured the money on the route that DOES change: a catalog board anchoring a
pool of finite offcuts, which is where the half board became a bin of the search
(``_fill_catalog``).

This is that measurement. Over the same furniture-shaped population the battery
generates, each pool is quoted twice **in one invocation**:

- **antes** — the contract that shipped before: search whole boards only, then
  rebate a sheet to a half if its content already fits one;
- **ahora** — whatever ``optimize_pool`` returns today.

Both arms run back to back on the same process, because seconds do not compare
across runs (ambient drift read the same config as wildly different times) while
boards and cost do.

Acceptance: **no pool may bill more than it did before**. A total that improves
while one job regresses is a rejection, not a trade — the seller who loses that
job is a real seller.

Usage::

    python scripts/bench_pools.py --kerf 4
    python scripts/bench_pools.py --kerf 4 --jobs 30
"""

import argparse
import os
import random
import sys
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench_battery import (  # noqa: E402
    SEARCH_ITERATIONS,
    TRIES_PER_BOARD,
    build_job,
    params_for,
)

from src.cutting import ExactConfig, SearchBudget  # noqa: E402
from src.cutting.models import BinSpec, Piece  # noqa: E402
from src.cutting.packer import expand_pieces  # noqa: E402
from src.modules.optimizations.materials import ResolvedMaterial  # noqa: E402
from src.modules.optimizations.pool import (  # noqa: E402
    _apply_half_downgrade,
    optimize_pool,
)
from src.modules.optimizations.schemas import PoolFillOrder  # noqa: E402

DEFAULT_SEED = 20260807
OFFCUT_SEED = 90000
# Above this the pool stops looking like a job somebody brings retazos to, and
# the sweep stops being affordable — both arms search every pool twice.
MAX_PIECES = 90


def _offcuts(job: int, pool: int, full: BinSpec) -> List[ResolvedMaterial]:
    """One or two retazos sized as fractions of the board, deterministically."""
    rng = random.Random(OFFCUT_SEED + job * 10 + pool)
    return [
        ResolvedMaterial(
            key=f"off{k}",
            width=float(int(full.width * rng.uniform(0.25, 0.6))),
            height=float(int(full.height * rng.uniform(0.3, 0.7))),
            thickness=full.thickness,
            cost_per_unit=0.0,
            source="clientOffcut",
            quantity=1,
            pool_key=full.key,
        )
        for k in range(rng.choice([1, 1, 2]))
    ]


def _primary(full: BinSpec) -> ResolvedMaterial:
    return ResolvedMaterial(
        key=full.key,
        width=full.width,
        height=full.height,
        thickness=full.thickness,
        cost_per_unit=full.cost_per_unit,
        source="catalog",
        product_id=1,
        fill_order=PoolFillOrder.auto,
    )


def _bill(layouts) -> float:
    return sum(layout.material.cost_per_unit for layout in layouts)


def _quote(
    pieces: List[Piece],
    primary: ResolvedMaterial,
    offcuts: List[ResolvedMaterial],
    half: Optional[BinSpec],
    params,
    exact_config: ExactConfig,
    post_hoc: Optional[BinSpec] = None,
) -> Tuple[float, int, int, float]:
    """One quote; returns ``(cost, whole sheets, half sheets, seconds)``.

    ``post_hoc`` reproduces the old contract: the search runs without the half
    bin and the rebate is applied to the finished sheets afterwards.
    """
    budget = SearchBudget.scaled(
        len(pieces), tries_per_board=TRIES_PER_BOARD, iterations=SEARCH_ITERATIONS
    )
    started = time.perf_counter()
    layouts, unplaced = optimize_pool(
        list(pieces),
        primary,
        list(offcuts),
        params,
        half_spec=half,
        budget=budget,
        exact_config=exact_config,
    )
    if post_hoc is not None:
        layouts = _apply_half_downgrade(layouts, post_hoc, params, 0, 0.1, exact_config)
    seconds = time.perf_counter() - started
    if unplaced:
        raise AssertionError(
            f"{len(unplaced)} piezas sin ubicar sobre un tablero infinito"
        )
    catalog = [layout for layout in layouts if layout.material.id == primary.key]
    return (
        _bill(layouts),
        sum(1 for layout in catalog if not layout.material.half_board),
        sum(1 for layout in catalog if layout.material.half_board),
        seconds,
    )


def run(jobs: int, seed: int, kerf: float) -> int:
    params = params_for(kerf)
    exact_config = ExactConfig()
    before_cost = after_cost = 0.0
    before_secs = after_secs = 0.0
    better = worse = 0
    pools = 0

    for index in range(jobs):
        for pool_i, (full, half, raw) in enumerate(build_job(index, seed)):
            pieces = expand_pieces(raw)
            if len(pieces) > MAX_PIECES:
                continue
            primary = _primary(full)
            offcuts = _offcuts(index, pool_i, full)
            pools += 1

            b_cost, b_full, b_half, b_secs = _quote(
                pieces, primary, offcuts, None, params, exact_config, post_hoc=half
            )
            a_cost, a_full, a_half, a_secs = _quote(
                pieces, primary, offcuts, half, params, exact_config
            )
            before_cost += b_cost
            after_cost += a_cost
            before_secs += b_secs
            after_secs += a_secs

            verdict = "   ="
            if a_cost > b_cost + 1e-6:
                verdict, worse = "PEOR", worse + 1
            elif a_cost < b_cost - 1e-6:
                verdict, better = "mejor", better + 1
            print(
                f"  j{index:02d}p{pool_i} n={len(pieces):3d} off={len(offcuts)} "
                f"{verdict} ${b_cost:8.2f} -> ${a_cost:8.2f}  "
                f"{b_full}e+{b_half}m -> {a_full}e+{a_half}m  "
                f"{b_secs:6.2f}s -> {a_secs:6.2f}s",
                flush=True,
            )

    delta = 100 * (after_cost - before_cost) / before_cost if before_cost else 0.0
    print(
        f"\nkerf {kerf:g} · {pools} pools (tablero de catálogo + retazos del cliente)"
    )
    print(f"  antes  ${before_cost:10.2f}   {before_secs:7.1f}s")
    print(f"  ahora  ${after_cost:10.2f}   {after_secs:7.1f}s   ({delta:+.2f}%)")
    print(f"  mejores {better} · peores {worse} · iguales {pools - better - worse}")

    if worse:
        print(f"\nRECHAZADO: {worse} pool(s) facturan más que antes")
        return 1
    print("\nACEPTADO: ningún pool factura más que antes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=22)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--kerf", type=float, default=4.0, help="blade width; 4 is the shop's real saw"
    )
    args = parser.parse_args()
    return run(args.jobs, args.seed, args.kerf)


if __name__ == "__main__":
    raise SystemExit(main())
