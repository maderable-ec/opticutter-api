"""Prueba diferencial: los constructores en Rust vs el oraculo Python.

Corre las 48 configuraciones de GREEDY_PORTFOLIO y las variantes de strip_fill
que usa `gen_fills` sobre cada pool real embebido en bench_prod.py, y compara
colocaciones, sobrantes y cortes EXACTAMENTE. La geometria se cachea por hash
de entrada, asi que la barra es igualdad bit a bit, no "parecido".

Uso:  python diff_rust_parity.py [--sub N] [--only greedy|strip]
      --sub N tambien prueba N sub-pools aleatorios (deterministas) por pool,
      que es como el beam llama al packer en la practica: con la cola de piezas
      que quedaron sin colocar.
"""

import argparse
import os
import random
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import bench_prod  # noqa: E402
import opticutter_core  # noqa: E402

from src.cutting import CuttingParameters  # noqa: E402
from src.cutting.constructors import (  # noqa: E402
    GREEDY_PORTFOLIO,
    greedy_fill,
    strip_fill,
)
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
SELECTION_CODES = {
    PackingStrategy.MAX_EFFICIENCY: 0,
    PackingStrategy.LONG_OFFCUTS: 1,
}
MIN_RECT = 0.1


PARAMS_TUPLE = (
    PARAMS.kerf,
    PARAMS.top_trim,
    PARAMS.bottom_trim,
    PARAMS.left_trim,
    PARAMS.right_trim,
)


def encode(pool):
    """El pool tal como cruza la frontera: id -> (indice, rango lexicografico)."""
    ranks = {pid: i for i, pid in enumerate(sorted(p.id for p in pool))}
    return [
        (i, ranks[p.id], p.width, p.height, bool(p.can_rotate), int(p.priority))
        for i, p in enumerate(pool)
    ]


def rust_greedy(pool, spec, config):
    return opticutter_core.greedy_fill(
        encode(pool),
        spec.width,
        spec.height,
        PARAMS_TUPLE,
        SORT_CODES[config.sort],
        SPLIT_CODES[config.split],
        SELECTION_CODES[config.selection],
        MIN_RECT,
    )


def rust_strip(pool, spec, horizontal, first_dim, max_repeat):
    return opticutter_core.strip_fill(
        encode(pool),
        spec.width,
        spec.height,
        PARAMS_TUPLE,
        horizontal,
        first_dim,
        max_repeat,
        MIN_RECT,
    )


def strip_seeds(pool, spec):
    """Las semillas `first_dim` que `_Searcher._strip_seeds` genera."""
    dims = sorted(
        {p.width for p in pool} | {p.height for p in pool if p.can_rotate},
        reverse=True,
    )
    usable_w = spec.width - PARAMS.left_trim - PARAMS.right_trim
    return [d for d in dims if d <= usable_w][:4]


def strip_variants(pool, spec):
    """(horizontal, first_dim, max_repeat) exactamente como los pide gen_fills."""
    for horizontal in (False, True):
        for first_dim in [None] + strip_seeds(pool, spec):
            yield horizontal, first_dim, None
    for horizontal in (False, True):
        for repeat_cap in (1, 2):
            yield horizontal, None, repeat_cap


def py_shape(fill):
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


def rust_shape(out, pool):
    if out is None:
        return None
    placed, rects, cuts = out
    return (
        [(pool[i].id, x, y, w, h, rot) for (i, x, y, w, h, rot) in placed],
        [tuple(r) for r in rects],
        [tuple(c) for c in cuts],
    )


def first_difference(a, b):
    names = ("placed", "remainders", "cuts")
    for name, xs, ys in zip(names, a, b):
        if len(xs) != len(ys):
            return f"{name}: {len(xs)} (py) vs {len(ys)} (rust)"
        for i, (x, y) in enumerate(zip(xs, ys)):
            if x != y:
                return f"{name}[{i}]: {x} (py) != {y} (rust)"
    return "?"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sub", type=int, default=3)
    parser.add_argument("--only", choices=("greedy", "strip"))
    args = parser.parse_args()

    stats = {"greedy": [0, 0], "strip": [0, 0]}  # [total, identicas]
    failures = []

    def compare(kind, where, py, rs):
        stats[kind][0] += 1
        if py == rs:
            stats[kind][1] += 1
        elif len(failures) < 5:
            if py is None or rs is None:
                failures.append(
                    f"{where}: py={py is None} " f"rust={rs is None} (None mismatch)"
                )
            else:
                failures.append(f"{where}: {first_difference(py, rs)}")

    for job in bench_prod.JOBS:
        for pool_spec in job["pools"]:
            pieces, bins = bench_prod.build_pool(pool_spec)
            expanded = expand_pieces(pieces)

            variants = [("full", expanded)]
            rng = random.Random(len(expanded))
            for k in range(args.sub):
                if len(expanded) < 8:
                    break
                keep = rng.sample(expanded, k=max(4, len(expanded) * (k + 1) // 4))
                variants.append((f"sub{k}", keep))

            for spec in bins:
                for vname, pool in variants:
                    base = (
                        f"{job['job']}/{pool_spec['material']} "
                        f"{vname} half={spec.half_board}"
                    )

                    if args.only != "strip":
                        for config in GREEDY_PORTFOLIO:
                            compare(
                                "greedy",
                                f"{base} greedy {config.sort}/"
                                f"{config.split.value}/{config.selection.value}",
                                py_shape(
                                    greedy_fill(pool, spec, PARAMS, config, MIN_RECT)
                                ),
                                rust_shape(rust_greedy(pool, spec, config), pool),
                            )

                    if args.only != "greedy":
                        for horizontal, first_dim, cap in strip_variants(pool, spec):
                            compare(
                                "strip",
                                f"{base} strip h={horizontal} "
                                f"first={first_dim} cap={cap}",
                                py_shape(
                                    strip_fill(
                                        pool,
                                        spec,
                                        PARAMS,
                                        horizontal=horizontal,
                                        first_dim=first_dim,
                                        max_repeat=cap,
                                        min_rect_size=MIN_RECT,
                                    )
                                ),
                                rust_shape(
                                    rust_strip(pool, spec, horizontal, first_dim, cap),
                                    pool,
                                ),
                            )

    total = sum(t for t, _ in stats.values())
    same = sum(s for _, s in stats.values())
    for kind, (t, s) in stats.items():
        if t:
            print(f"{kind:>7}: {s}/{t} identicas")
    print(f"comparaciones: {total}")
    print(f"identicas:     {same}")
    print(f"distintas:     {total - same}")
    if failures:
        print("\nprimeras diferencias:")
        for f in failures:
            print(f"  {f}")
    else:
        print("\nPARIDAD EXACTA en todas las comparaciones")


if __name__ == "__main__":
    main()
