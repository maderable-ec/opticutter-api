"""Diferencial de punta a punta: `optimize_bins` con y sin el kernel Rust.

`diff_rust_parity.py` compara los constructores uno por uno; esto corre el motor
COMPLETO (beam + LNS + CP-SAT + medio tablero) sobre los pools reales embebidos
en `bench_prod.py`, una vez por backend, y compara el sha256 de la geometria —
el mismo digest que usa `scripts/bench_battery.py`.

El puerto es aritmeticamente neutro por definicion: un digest que se mueve es un
bug, no una mejora. De paso mide el speedup real del motor (no del kernel), que
es el numero que importa para el presupuesto de latencia.

Uso:  python scripts/diff_rust_engine.py [--all]
      --all corre los 6 trabajos del taller; por defecto solo la pre-orden 24.
"""

import argparse
import hashlib
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import bench_prod  # noqa: E402

from src.cutting import (  # noqa: E402
    CuttingParameters,
    ExactConfig,
    SearchBudget,
    optimize_bins,
    rust_backend,  # noqa: E402
)

PARAMS = CuttingParameters(
    kerf=bench_prod.KERF,
    top_trim=bench_prod.TRIM,
    bottom_trim=bench_prod.TRIM,
    left_trim=bench_prod.TRIM,
    right_trim=bench_prod.TRIM,
)


def run_pool(pool_spec):
    """(digest, tableros, segundos) de un pool con el backend ya fijado."""
    pieces, bins = bench_prod.build_pool(pool_spec)
    instances = sum(p.quantity for p in pieces)
    budget = SearchBudget.scaled(
        instances,
        tries_per_board=bench_prod.TRIES_PER_BOARD,
        iterations=bench_prod.SEARCH_ITERATIONS,
    )
    started = time.perf_counter()
    layouts, unplaced = optimize_bins(
        pieces,
        bins,
        cutting_params=PARAMS,
        budget=budget,
        exact_config=ExactConfig(),
    )
    elapsed = time.perf_counter() - started
    digest = hashlib.sha256(
        json.dumps(
            [lay.to_dict() for lay in layouts], sort_keys=True, default=str
        ).encode()
    ).hexdigest()
    halves = sum(1 for la in layouts if la.material.half_board)
    boards = (len(layouts) - halves) + 0.5 * halves
    return digest, boards, elapsed, len(unplaced), instances


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if rust_backend.opticutter_core is None:
        sys.exit("opticutter_core no esta instalado: nada que comparar")

    jobs = bench_prod.JOBS if args.all else bench_prod.JOBS[:1]

    print(
        f"{'pool':<32}{'pzs':>5}{'tabl':>7}{'python':>9}{'rust':>9}"
        f"{'spd':>7}  digest"
    )
    tp = tr = 0.0
    mismatches = []
    for job in jobs:
        for pool_spec in job["pools"]:
            rust_backend.set_enabled(False)
            d_py, boards_py, t_py, unplaced_py, n = run_pool(pool_spec)
            rust_backend.set_enabled(True)
            d_rs, boards_rs, t_rs, unplaced_rs, _ = run_pool(pool_spec)

            ok = (d_py, boards_py, unplaced_py) == (d_rs, boards_rs, unplaced_rs)
            if not ok:
                mismatches.append(
                    f"{job['job']}/{pool_spec['material']}: "
                    f"py={d_py[:12]} {boards_py} tabl / "
                    f"rust={d_rs[:12]} {boards_rs} tabl"
                )
            tp += t_py
            tr += t_rs
            name = f"{job['job'][:15]}/{pool_spec['material'][:15]}"
            print(
                f"{name:<32}{n:>5}{boards_py:>7}{t_py:>8.2f}s{t_rs:>8.2f}s"
                f"{t_py / t_rs:>6.1f}x  {d_py[:12]}{'' if ok else '  DISTINTO'}"
            )

    print(f"\n{'TOTAL':<32}{'':>5}{'':>7}{tp:>8.2f}s{tr:>8.2f}s{tp / tr:>6.1f}x")
    if mismatches:
        print("\nGEOMETRIA DISTINTA:")
        for m in mismatches:
            print(f"  {m}")
        sys.exit(1)
    print("\nGEOMETRIA IDENTICA en todos los pools")


if __name__ == "__main__":
    main()
