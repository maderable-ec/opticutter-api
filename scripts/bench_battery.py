"""Randomized cost-regression battery for the cutting search.

The engine's own tests pin *validity* (pieces inside the board, no overlaps,
kerf respected) and *parity* on two real audit jobs (pre-orders 20 and 21,
14 piece types, one material). Neither answers the question a change to the
search's stopping rule actually raises:

    "on some other job shape, does this now bill the client an extra board?"

That is a question about money across many job shapes, so this script generates
a fixed population of furniture-shaped jobs, runs the engine over all of them
and records, per job, the **total material cost**, the board count, the wall
time and a sha256 of the produced geometry. Re-run it after a change and
``--compare`` prints the diff.

Two acceptance rules, depending on what is being changed:

- **Arithmetically neutral change** (a fast path, a precomputed attribute):
  every ``digest`` must be **identical**. Anything else means the refactor
  moved geometry and is not neutral after all.
- **Change to the search itself** (stopping rule, budget, candidate order):
  digests are expected to move; the bar is **no job may get more expensive**.

The population is deterministic: job ``k`` derives its own RNG from
``seed + k``, so it is the same job whatever ``--jobs`` is set to, and jobs can
be regenerated or bisected individually.

Usage::

    python scripts/bench_battery.py --out baseline.json
    # ... make the change ...
    python scripts/bench_battery.py --compare baseline.json

``--workers`` runs jobs in parallel (default: most of the box). Cost, boards and
digest are unaffected by it; **wall times are inflated by contention**, so pass
``--workers 1`` when the timing numbers themselves are the point.

``--kerf`` picks the blade the jobs are cut with (default 5). The shop's saw was
confirmed at **4mm**, which is what production runs, so that is the kerf a
baseline meant to gate a change should use. It only affects the packing: the job
population itself is kerf-independent (pieces are clamped by the trims, not the
blade), so a kerf-4 run and a kerf-5 run compare the same 80 jobs.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cutting import (  # noqa: E402
    BinSpec,
    CuttingParameters,
    ExactConfig,
    Piece,
    SearchBudget,
    optimize_bins,
)

# Mirrors the production defaults: MDP sheet, 10mm trims, and the half board the
# catalog sells at price/2 plus a 10% markup. ``PARAMS`` keeps the historical
# 5mm blade: it is what the generator clamps piece sizes with (kerf plays no part
# in that) and what the pinned ceilings in ``test_cutting_search.py`` import, so
# the job population stays fixed while ``--kerf`` moves only the packing.
SHEET_W, SHEET_H = 2070.0, 2800.0
HALF_MARKUP = 0.10
PARAMS = CuttingParameters(
    kerf=5, top_trim=10, bottom_trim=10, left_trim=10, right_trim=10
)


def params_for(kerf: float) -> CuttingParameters:
    """``PARAMS`` with the blade swapped (trims and sheet are fixed)."""
    return CuttingParameters(
        kerf=kerf,
        top_trim=PARAMS.top_trim,
        bottom_trim=PARAMS.bottom_trim,
        left_trim=PARAMS.left_trim,
        right_trim=PARAMS.right_trim,
    )


# Same knobs as ``config.OPT_TRIES_PER_BOARD`` / ``OPT_SEARCH_ITERATIONS``.
TRIES_PER_BOARD = 48
SEARCH_ITERATIONS = 40

# (name, width range, height range, quantity range) — the panel archetypes a
# melamine shop actually cuts. Ranges overlap on purpose: real jobs mix a few
# large panels with a long tail of small repeated parts, which is exactly the
# shape that strands pieces and forces the search to work.
ARCHETYPES = [
    ("lateral", (300, 600), (1800, 2400), (2, 8)),
    ("puerta", (300, 600), (700, 2000), (2, 10)),
    ("entrepano", (250, 580), (400, 1200), (3, 14)),
    ("fondo", (300, 1200), (400, 2000), (1, 6)),
    ("frente-cajon", (150, 400), (300, 600), (4, 16)),
    ("zocalo", (70, 150), (400, 2780), (2, 16)),
    ("tapa", (400, 900), (900, 1800), (1, 4)),
]

# Catalog-ish board prices per thickness.
PRICES = {15: [48.0, 56.0, 58.0, 62.0], 36: [96.0, 112.0]}

# Size range of a generated job, in piece instances. Bounded on purpose: the
# search cost grows steeply with pool size, so an unbounded generator spends the
# whole battery inside a handful of giant outliers instead of covering shapes.
MIN_JOB_PIECES = 20
MAX_JOB_PIECES = 280


@dataclass
class JobResult:
    job: int
    pools: int
    pieces: int
    cost: float
    boards: int
    seconds: float
    digest: str


def _round5(value: float) -> float:
    """Snaps to 5mm. Shops quote in round numbers; unique float dims would make
    every piece its own type and hide the symmetry the search exploits."""
    return float(int(round(value / 5.0) * 5))


def build_job(index: int, seed: int) -> List[Tuple[BinSpec, BinSpec, List[Piece]]]:
    """Builds job ``index`` as a list of (full bin, half bin, pieces) pools.

    One pool per material, mirroring how ``OptimizationService`` groups
    requirements by ``materialKey`` and optimizes each independently.

    The **total** piece count is drawn first and then split across the pools,
    rather than emerging from per-pool draws. Letting it emerge produces jobs of
    700+ pieces that no shop ever quotes and that dominate the battery's runtime;
    drawing it up front keeps the population inside the range real quotes live in
    (pre-order 24, the job that motivated all this, is 248 pieces over 4 pools).
    """
    rng = random.Random(seed + index)
    n_pools = rng.choice([1, 1, 2, 2, 3, 4])
    target_total = rng.randint(MIN_JOB_PIECES, MAX_JOB_PIECES)
    # Split the budget across pools, leaving each at least a handful of pieces.
    weights = [rng.uniform(0.5, 1.5) for _ in range(n_pools)]
    scale = target_total / sum(weights)
    pool_targets = [max(4, int(w * scale)) for w in weights]

    pools = []
    for pool_i, pool_target in enumerate(pool_targets):
        thickness = 36 if rng.random() < 0.15 else 15
        price = rng.choice(PRICES[thickness])
        full = BinSpec(
            key=f"m{pool_i}",
            width=SHEET_W,
            height=SHEET_H,
            thickness=float(thickness),
            cost_per_unit=price,
        )
        half = BinSpec(
            key=f"m{pool_i}",
            width=SHEET_W / 2.0,
            height=SHEET_H,
            thickness=float(thickness),
            cost_per_unit=round(price / 2.0 * (1 + HALF_MARKUP), 2),
            half_board=True,
        )
        # Grain-locked boards (woodgrain) forbid rotation; plain colours allow it.
        can_rotate = rng.random() < 0.5
        pieces: List[Piece] = []
        placed = 0
        type_i = 0
        while placed < pool_target:
            name, wr, hr, qr = rng.choice(ARCHETYPES)
            w = _round5(rng.uniform(*wr))
            h = _round5(rng.uniform(*hr))
            # Keep every piece physically cuttable from the sheet.
            w = min(w, SHEET_W - PARAMS.left_trim - PARAMS.right_trim)
            h = min(h, SHEET_H - PARAMS.top_trim - PARAMS.bottom_trim)
            quantity = min(rng.randint(*qr), pool_target - placed)
            pieces.append(
                Piece(
                    id=f"p{pool_i}-{type_i}-{name}",
                    width=w,
                    height=h,
                    quantity=quantity,
                    can_rotate=can_rotate,
                )
            )
            placed += quantity
            type_i += 1
        pools.append((full, half, pieces))
    return pools


def run_job(index: int, seed: int, kerf: float = PARAMS.kerf) -> JobResult:
    """Optimizes every pool of a job and aggregates cost, boards and geometry."""
    pools = build_job(index, seed)
    params = params_for(kerf)
    exact_config = ExactConfig()
    total_cost = 0.0
    total_boards = 0
    total_pieces = 0
    hasher = hashlib.sha256()
    started = time.perf_counter()
    for full, half, pieces in pools:
        n_instances = sum(p.quantity for p in pieces)
        total_pieces += n_instances
        budget = SearchBudget.scaled(
            n_instances,
            tries_per_board=TRIES_PER_BOARD,
            iterations=SEARCH_ITERATIONS,
        )
        layouts, unplaced = optimize_bins(
            pieces,
            [full, half],
            cutting_params=params,
            budget=budget,
            exact_config=exact_config,
        )
        if unplaced:
            raise AssertionError(
                f"job {index}: {len(unplaced)} unplaced pieces — the generator "
                f"emitted something that fits no bin"
            )
        total_cost += sum(lay.material.cost_per_unit for lay in layouts)
        total_boards += len(layouts)
        hasher.update(
            json.dumps(
                [lay.to_dict() for lay in layouts], sort_keys=True, default=str
            ).encode()
        )
    return JobResult(
        job=index,
        pools=len(pools),
        pieces=total_pieces,
        cost=round(total_cost, 2),
        boards=total_boards,
        seconds=round(time.perf_counter() - started, 3),
        digest=hasher.hexdigest()[:16],
    )


def _run_one(args: Tuple[int, int, float]) -> JobResult:
    return run_job(*args)


def run_battery(n_jobs: int, seed: int, workers: int, kerf: float) -> List[JobResult]:
    # The kerf travels in the task tuple, not in a module global: the executor
    # re-imports this module in every child, so a global set in ``main`` would
    # silently snap back to ``PARAMS.kerf`` in the workers.
    tasks = [(i, seed, kerf) for i in range(n_jobs)]
    results: List[JobResult] = []
    if workers <= 1:
        for task in tasks:
            results.append(_report(_run_one(task), n_jobs))
        return results
    with ProcessPoolExecutor(max_workers=workers) as pool:
        # chunksize=1: results stream back as each job lands instead of waiting
        # for a whole chunk, which matters when single jobs run for a minute.
        for result in pool.map(_run_one, tasks, chunksize=1):
            results.append(_report(result, n_jobs))
    return sorted(results, key=lambda r: r.job)


def _report(result: JobResult, total: int) -> JobResult:
    print(
        f"  [{result.job + 1:>3}/{total}] pools={result.pools} "
        f"pieces={result.pieces:>4} boards={result.boards:>3} "
        f"cost=${result.cost:>9.2f} {result.seconds:>7.2f}s",
        flush=True,
    )
    return result


def compare(current: List[JobResult], baseline_path: str, kerf: float) -> int:
    """Prints the diff and returns a process exit code (non-zero = regression)."""
    with open(baseline_path) as fh:
        raw = json.load(fh)
    base = {job["job"]: job for job in raw["jobs"]}
    # A baseline cut with another blade is a different experiment: every job
    # would read as "moved" and the cost bar would compare two populations.
    base_kerf = raw.get("kerf", PARAMS.kerf)
    if abs(base_kerf - kerf) > 1e-9:
        print(
            f"\n  ❌ la línea base es de kerf {base_kerf:g} y esta corrida es de "
            f"kerf {kerf:g}: no son comparables."
        )
        return 2

    worse: List[Tuple[JobResult, dict]] = []
    better: List[Tuple[JobResult, dict]] = []
    moved: List[JobResult] = []
    same_cost = 0

    for result in current:
        prior = base.get(result.job)
        if prior is None:
            continue
        if result.cost > prior["cost"] + 1e-6:
            worse.append((result, prior))
        elif result.cost < prior["cost"] - 1e-6:
            better.append((result, prior))
        else:
            same_cost += 1
        if result.digest != prior["digest"]:
            moved.append(result)

    print("\n" + "=" * 78)
    print(
        f"{'job':>4} {'pieces':>7} {'cost antes':>12} {'cost después':>13} "
        f"{'t antes':>9} {'t después':>10}  geometría"
    )
    for result in current:
        prior = base.get(result.job)
        if prior is None:
            continue
        delta = result.cost - prior["cost"]
        if abs(delta) <= 1e-6 and result.digest == prior["digest"]:
            continue
        flag = (
            "REGRESIÓN" if delta > 1e-6 else ("mejora" if delta < -1e-6 else "movida")
        )
        print(
            f"{result.job:>4} {result.pieces:>7} {prior['cost']:>12.2f} "
            f"{result.cost:>13.2f} {prior['seconds']:>8.2f}s "
            f"{result.seconds:>9.2f}s  {flag}"
        )

    t_before = sum(base[r.job]["seconds"] for r in current if r.job in base)
    t_after = sum(r.seconds for r in current if r.job in base)
    c_before = sum(base[r.job]["cost"] for r in current if r.job in base)
    c_after = sum(r.cost for r in current if r.job in base)

    print("=" * 78)
    print(f"  costo igual : {same_cost}")
    print(f"  más barato  : {len(better)}")
    print(f"  MÁS CARO    : {len(worse)}")
    print(f"  geometría movida: {len(moved)} de {len(current)}")
    print(
        f"  costo total : ${c_before:.2f} -> ${c_after:.2f} "
        f"({(c_after - c_before) / c_before * 100:+.2f}%)"
    )
    print(
        f"  tiempo total: {t_before:.1f}s -> {t_after:.1f}s "
        f"({(t_after - t_before) / t_before * 100:+.1f}%)"
    )

    if worse:
        print("\n  ❌ RECHAZADO: hay trabajos donde el cambio factura más material.")
        return 1
    print("\n  ✅ Sin regresiones de costo.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=80)
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2)
    )
    parser.add_argument("--out", default=None, help="write results to this JSON file")
    parser.add_argument("--compare", default=None, help="diff against this JSON file")
    parser.add_argument(
        "--kerf",
        type=float,
        default=PARAMS.kerf,
        help=f"blade width in mm (default {PARAMS.kerf:g}; production cuts at 4)",
    )
    args = parser.parse_args()

    if args.workers > 1:
        print(
            f"running {args.jobs} jobs at kerf {args.kerf:g} on {args.workers} "
            f"workers (times are contention-inflated; use --workers 1 for timing)\n"
        )
    else:
        print(f"running {args.jobs} jobs at kerf {args.kerf:g} serially\n")

    started = time.perf_counter()
    results = run_battery(args.jobs, args.seed, args.workers, args.kerf)
    elapsed = time.perf_counter() - started

    print(
        f"\n{args.jobs} jobs in {elapsed:.1f}s wall "
        f"({sum(r.seconds for r in results):.1f}s cpu), "
        f"total ${sum(r.cost for r in results):.2f}, "
        f"{sum(r.boards for r in results)} boards"
    )

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(
                {
                    "seed": args.seed,
                    "kerf": args.kerf,
                    "jobs": [r.__dict__ for r in results],
                },
                fh,
                indent=1,
            )
        print(f"wrote {args.out}")

    if args.compare:
        return compare(results, args.compare, args.kerf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
