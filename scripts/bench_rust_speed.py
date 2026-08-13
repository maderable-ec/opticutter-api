"""Donde se va el tiempo del lado Rust: marshalling vs nucleo.

Tres mediciones por pool, sobre las 48 configs del portfolio:
  A) python puro
  B) rust con payload construido UNA vez por pool (lo que haria el bridge)
  C) rust nucleo solo: payload prearmado, salida ignorada

B-C es el costo de cruzar. Si domina, la frontera tiene que subir a gen_fills.
"""

import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import bench_prod  # noqa: E402
import opticutter_core  # noqa: E402

from src.cutting import CuttingParameters  # noqa: E402
from src.cutting.constructors import GREEDY_PORTFOLIO, greedy_fill  # noqa: E402
from src.cutting.enums import PackingStrategy, SplitRule  # noqa: E402
from src.cutting.packer import expand_pieces  # noqa: E402

PARAMS = CuttingParameters(
    kerf=bench_prod.KERF,
    top_trim=bench_prod.TRIM,
    bottom_trim=bench_prod.TRIM,
    left_trim=bench_prod.TRIM,
    right_trim=bench_prod.TRIM,
)
SORT_CODES = {
    "area": 0,
    "maxdim": 1,
    "height": 2,
    "width": 3,
    "perimeter": 4,
    "mindim": 5,
}
SPLIT_CODES = {
    SplitRule.SHORTER_LEFTOVER_AXIS: 0,
    SplitRule.LONGER_LEFTOVER_AXIS: 1,
    SplitRule.MINIMIZE_AREA: 2,
    SplitRule.MAXIMIZE_AREA: 3,
    SplitRule.SHORTER_AXIS: 4,
    SplitRule.LONGER_AXIS: 5,
}
SELECTION_CODES = {PackingStrategy.MAX_EFFICIENCY: 0, PackingStrategy.LONG_OFFCUTS: 1}
PT = (
    PARAMS.kerf,
    PARAMS.top_trim,
    PARAMS.bottom_trim,
    PARAMS.left_trim,
    PARAMS.right_trim,
)
REPS = 5


def main():
    print(
        f"{'pool':<30}{'pzs':>5}{'python':>10}{'rust+FFI':>10}"
        f"{'nucleo':>10}{'cruce':>9}{'spd':>7}{'techo':>7}"
    )
    tp = tb = tc = 0.0
    for job in bench_prod.JOBS:
        for ps in job["pools"]:
            pieces, bins = bench_prod.build_pool(ps)
            pool = expand_pieces(pieces)
            if len(pool) < 20:
                continue
            spec = bins[0]
            codes = [
                (SORT_CODES[c.sort], SPLIT_CODES[c.split], SELECTION_CODES[c.selection])
                for c in GREEDY_PORTFOLIO
            ]

            t0 = time.perf_counter()
            for _ in range(REPS):
                for c in GREEDY_PORTFOLIO:
                    greedy_fill(pool, spec, PARAMS, c, 0.1)
            t_py = (time.perf_counter() - t0) / REPS

            t0 = time.perf_counter()
            for _ in range(REPS):
                ranks = {p: i for i, p in enumerate(sorted(x.id for x in pool))}
                payload = [
                    (
                        i,
                        ranks[p.id],
                        p.width,
                        p.height,
                        bool(p.can_rotate),
                        int(p.priority),
                    )
                    for i, p in enumerate(pool)
                ]
                for sc, spl, sel in codes:
                    out = opticutter_core.greedy_fill(
                        payload, spec.width, spec.height, PT, sc, spl, sel, 0.1
                    )
                    if out is not None:
                        placed, rects, cuts = out
                        _ = [
                            (pool[i].id, x, y, w, h, r) for (i, x, y, w, h, r) in placed
                        ]
            t_bridge = (time.perf_counter() - t0) / REPS

            ranks = {p: i for i, p in enumerate(sorted(x.id for x in pool))}
            payload = [
                (i, ranks[p.id], p.width, p.height, bool(p.can_rotate), int(p.priority))
                for i, p in enumerate(pool)
            ]
            t0 = time.perf_counter()
            for _ in range(REPS):
                for sc, spl, sel in codes:
                    opticutter_core.greedy_fill(
                        payload, spec.width, spec.height, PT, sc, spl, sel, 0.1
                    )
            t_core = (time.perf_counter() - t0) / REPS

            tp += t_py
            tb += t_bridge
            tc += t_core
            name = f"{job['job'][:14]}/{ps['material'][:14]}"
            print(
                f"{name:<30}{len(pool):>5}{t_py*1000:>9.1f}ms"
                f"{t_bridge*1000:>9.1f}ms{t_core*1000:>9.1f}ms"
                f"{(t_bridge-t_core)*1000:>8.1f}ms"
                f"{t_py/t_bridge:>6.1f}x{t_py/t_core:>6.1f}x"
            )

    print(
        f"\n{'TOTAL':<30}{'':>5}{tp*1000:>9.1f}ms{tb*1000:>9.1f}ms"
        f"{tc*1000:>9.1f}ms{(tb-tc)*1000:>8.1f}ms{tp/tb:>6.1f}x{tp/tc:>6.1f}x"
    )
    print(f"\ncruce = {100*(tb-tc)/tb:.0f}% del tiempo del lado Rust")


if __name__ == "__main__":
    main()
