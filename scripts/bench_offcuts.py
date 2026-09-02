"""Offcut-quality benchmark for the cut-tree consolidation.

``bench_battery.py`` answers "did this cost a board?". It cannot answer the
question ``src/cutting/consolidate.py`` exists for, because board cost is exactly
what the consolidation is forbidden to touch: it runs on the winning layouts and
never moves a piece, so the battery's digests must stay **byte-identical** while
this script's numbers move. The two are complements, and a change to the
consolidation objective needs both.

What it measures, over the same fixed job population the battery generates, is
the shape of the waste rather than its size:

- **retazos** — how many leftover rectangles the shop ends up racking;
- **aprovechables** — how many clear ``--min-side`` on BOTH axes, i.e. how many
  are worth keeping at all;
- **área aprovechable** — the m2 inside those, which is the number that must not
  go down: a "consolidation" that merges two usable offcuts by shaving one into
  slivers is a loss dressed as a win;
- **corte** — total saw travel, which usually falls because the consolidated tree
  stops ripping full height through waste it does not need to separate.

Acceptance: **usable area never drops** and the retazo count does. Both are
printed as a verdict.

Usage::

    python scripts/bench_offcuts.py --kerf 4
    python scripts/bench_offcuts.py --kerf 4 --jobs 30 --min-side 200
"""

import argparse
import os
import statistics
import sys
import time
from dataclasses import dataclass
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench_battery import (  # noqa: E402
    SEARCH_ITERATIONS,
    TRIES_PER_BOARD,
    build_job,
    params_for,
)

from src.cutting import ExactConfig, SearchBudget, optimize_bins  # noqa: E402
from src.cutting.consolidate import consolidate_layout  # noqa: E402

DEFAULT_SEED = 20240101


@dataclass
class Totals:
    boards: int = 0
    unchanged: int = 0
    rects: int = 0
    usable: int = 0
    usable_area: float = 0.0
    largest: float = 0.0
    cut_length: float = 0.0

    def add(self, layout, min_side: float) -> None:
        self.boards += 1
        rects = layout.remainders
        self.rects += len(rects)
        self.cut_length += layout.cut_length
        self.largest += max((r.area for r in rects), default=0.0)
        for rect in rects:
            if rect.width >= min_side and rect.height >= min_side:
                self.usable += 1
                self.usable_area += rect.area


def _delta(before: float, after: float) -> str:
    if before == 0:
        return "  n/a"
    return f"{100 * (after - before) / before:+6.2f}%"


def run(jobs: int, seed: int, kerf: float, min_side: float) -> int:
    params = params_for(kerf)
    exact_config = ExactConfig()
    before = Totals()
    after = Totals()
    seconds: List[float] = []
    started = time.perf_counter()

    for index in range(jobs):
        for full, half, pieces in build_job(index, seed):
            instances = sum(piece.quantity for piece in pieces)
            layouts, unplaced = optimize_bins(
                pieces,
                [full, half],
                cutting_params=params,
                budget=SearchBudget.scaled(
                    instances,
                    tries_per_board=TRIES_PER_BOARD,
                    iterations=SEARCH_ITERATIONS,
                ),
                exact_config=exact_config,
            )
            if unplaced:
                raise AssertionError(f"job {index}: {len(unplaced)} unplaced pieces")
            for layout in layouts:
                before.add(layout, min_side)
                mark = time.perf_counter()
                consolidated = consolidate_layout(
                    layout, params, min_usable_offcut=min_side
                )
                seconds.append(time.perf_counter() - mark)
                if consolidated is layout:
                    after.unchanged += 1
                after.add(consolidated, min_side)
        print(f"  job {index + 1}/{jobs} — {before.boards} tableros", flush=True)

    print(
        f"\nkerf {kerf:g} · retazo aprovechable ≥ {min_side:g}mm de lado · "
        f"{before.boards} tableros · {time.perf_counter() - started:.1f}s"
    )
    print(f"{'':22} {'antes':>12} {'después':>12} {'':>7}")
    rows = [
        ("retazos", before.rects, after.rects),
        ("aprovechables", before.usable, after.usable),
        ("área aprov. (m2)", before.usable_area / 1e6, after.usable_area / 1e6),
        ("retazo mayor (m2)", before.largest / 1e6, after.largest / 1e6),
        ("corte (m)", before.cut_length / 1000, after.cut_length / 1000),
    ]
    for label, lhs, rhs in rows:
        print(f"{label:22} {lhs:12.2f} {rhs:12.2f} {_delta(lhs, rhs):>7}")

    print(
        f"\nsin cambio: {after.unchanged}/{after.boards} tableros · "
        f"consolidación {sum(seconds):.2f}s total, "
        f"{statistics.median(seconds) * 1000:.1f}ms mediana, "
        f"{max(seconds) * 1000:.1f}ms peor tablero"
    )

    lost_area = after.usable_area < before.usable_area - 1e-6
    more_rects = after.rects > before.rects
    if lost_area or more_rects:
        print(
            "\nRECHAZADO: "
            + ", ".join(
                filter(
                    None,
                    [
                        "cayó el área aprovechable" if lost_area else "",
                        "subió el conteo de retazos" if more_rects else "",
                    ],
                )
            )
        )
        return 1
    print("\nACEPTADO: menos retazos, sin perder área aprovechable")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=20)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--kerf",
        type=float,
        default=4.0,
        help="blade width; 4 is the shop's real saw",
    )
    parser.add_argument(
        "--min-side",
        type=float,
        default=150.0,
        help="mirror of OPT_MIN_USABLE_OFFCUT_MM",
    )
    args = parser.parse_args()
    return run(args.jobs, args.seed, args.kerf, args.min_side)


if __name__ == "__main__":
    raise SystemExit(main())
