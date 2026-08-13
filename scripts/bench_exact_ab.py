"""A/B del solver por pool: que compra CP-SAT y cuanto cuesta, pool por pool.

Corre cada pool real dos veces, con ExactConfig(enabled=True) y con
enabled=False, y compara tableros, costo y tiempo. Los pools donde el
resultado es IDENTICO son la superficie del gate adaptativo (Fase B): ahi el
solver es coste puro. Los pools donde pierde tableros son los que el gate no
puede tocar.
"""

import argparse
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
)


def _params(kerf):
    return CuttingParameters(
        kerf=kerf,
        top_trim=bench_prod.TRIM,
        bottom_trim=bench_prod.TRIM,
        left_trim=bench_prod.TRIM,
        right_trim=bench_prod.TRIM,
    )


PARAMS = _params(bench_prod.KERF)


def run(pieces, bins, budget, enabled):
    t0 = time.perf_counter()
    layouts, unplaced = optimize_bins(
        pieces,
        bins,
        cutting_params=PARAMS,
        budget=budget,
        exact_config=ExactConfig(enabled=enabled),
    )
    assert not unplaced
    cost = sum(lay.material.cost_per_unit for lay in layouts)
    halves = sum(1 for lay in layouts if lay.material.half_board)
    return {
        "seconds": time.perf_counter() - t0,
        "boards": len(layouts),
        "halves": halves,
        "cost": round(cost, 2),
    }


def main():
    global PARAMS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kerf", type=float, default=bench_prod.KERF)
    PARAMS = _params(parser.parse_args().kerf)
    print(f"kerf={PARAMS.kerf:g}")
    print(f"{'pool':<34}{'pzs':>5}  {'CON solver':>22}  {'SIN solver':>22}   veredicto")
    print(f"{'':<34}{'':>5}  {'tab  costo    seg':>22}  {'tab  costo    seg':>22}")
    tot_on = tot_off = 0.0
    free = 0.0
    for job in bench_prod.JOBS:
        for pool in job["pools"]:
            pieces, bins = bench_prod.build_pool(pool)
            instances = sum(p.quantity for p in pieces)
            budget = SearchBudget.scaled(
                instances,
                tries_per_board=bench_prod.TRIES_PER_BOARD,
                iterations=bench_prod.SEARCH_ITERATIONS,
            )
            on = run(pieces, bins, budget, True)
            off = run(pieces, bins, budget, False)
            tot_on += on["seconds"]
            tot_off += off["seconds"]

            if off["cost"] == on["cost"]:
                verdict = f"IGUAL  -{on['seconds'] - off['seconds']:.2f}s gratis"
                free += on["seconds"] - off["seconds"]
            elif off["cost"] > on["cost"]:
                verdict = f"el solver GANA ${off['cost'] - on['cost']:.2f}"
            else:
                verdict = f"el solver PIERDE ${on['cost'] - off['cost']:.2f}"

            name = f"{job['job'][:16]}/{pool['material'][:16]}"
            print(
                f"{name:<34}{instances:>5}  "
                f"{on['boards']:>3}{'+½' if on['halves'] else '  '}"
                f"{on['cost']:>8.2f}{on['seconds']:>7.2f}s  "
                f"{off['boards']:>3}{'+½' if off['halves'] else '  '}"
                f"{off['cost']:>8.2f}{off['seconds']:>7.2f}s   {verdict}"
            )
            sys.stdout.flush()
    print(
        f"\nTOTAL  con solver {tot_on:.2f}s   sin solver {tot_off:.2f}s   "
        f"({tot_on / max(tot_off, 1e-9):.2f}x)"
    )
    print(
        f"Tiempo de solver en pools de resultado IDENTICO: {free:.2f}s "
        f"({100 * free / tot_on:.0f}% del total con solver)"
    )


if __name__ == "__main__":
    main()
