"""Mide el optimizador EN PRODUCCIÓN con los despieces reales del taller.

Por qué existe
-------------
La latencia de las cotizaciones grandes es CPU del optimizador, no
infraestructura: la pre-orden 24 (248 piezas, 4 materiales) tarda ~31 s en una
Mac M1 y tardaba 2 m 20 s en el VPS. De ahí salió un multiplicador estimado de
~3.2x, pero eso es una extrapolación de un solo dato. Este script lo **mide**:
corre exactamente los mismos pools en la máquina de producción y reporta el
cociente contra los tiempos de referencia locales, embebidos abajo.

De paso verifica lo único que importa comercialmente: que sigamos usando igual
o menos tableros que el programa comercial, cuyo resultado está grabado en el
nombre de cada archivo original del taller.

Autocontenido a propósito
-------------------------
No lee la base, ni Redis, ni los XML: los seis despieces van embebidos (fueron
extraídos de ``pruebas/*.xml`` y verificados pieza por pieza contra ellos). Sólo
necesita ``src.cutting``, que es dominio puro y ya está dentro de la imagen. Así
se corre en el VPS sin desplegar nada ni depender de archivos que no están en el
repo.

⚠️  CUIDADO EN PRODUCCIÓN
------------------------
Satura un core durante minutos en un VPS que tiene 2, y que además corre
Postgres, Redis y Caddy. Mientras corre, la API responde más lento. Por eso el
modo por defecto es sólo la pre-orden 24 (~1-3 min según la máquina); ``--all``
son los seis trabajos y puede superar los 10 minutos. **Correlo fuera del
horario del taller.**

Uso
---
Desde el directorio del stack (opticutter-infra), sin copiar nada::

    docker compose exec -T api python - < bench_prod.py

Las opciones van después del guión::

    docker compose exec -T api python - --all < bench_prod.py

O copiándolo primero, si preferís tenerlo dentro::

    docker compose cp bench_prod.py api:/tmp/bench_prod.py
    docker compose exec api python /tmp/bench_prod.py --all

Opciones: ``--all`` (los 6 trabajos), ``--kerf N`` (probar otro ancho de sierra:
en los trabajos apretados vale medio tablero), ``--json`` (salida cruda).
"""

import argparse
import json
import os
import platform
import sys
import time

# En el contenedor de producción el código está en /src; en el repo, un nivel
# arriba de scripts/. Se resuelve solo para que el mismo archivo sirva en ambos.
# ``__file__`` no existe cuando el script llega por stdin (``python - < ...``),
# que es justamente la forma recomendada de correrlo en el VPS.
_here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else None
for _candidate in ("/src", os.path.dirname(_here) if _here else None, os.getcwd()):
    if _candidate and os.path.isdir(os.path.join(_candidate, "src", "cutting")):
        sys.path.insert(0, _candidate)
        break
else:
    sys.exit(
        "No encuentro src/cutting. Corré esto dentro del contenedor de la API "
        "(docker compose exec api ...) o desde la raíz del repo."
    )

from src.cutting import (  # noqa: E402
    BinSpec,
    CuttingParameters,
    ExactConfig,
    Piece,
    SearchBudget,
    exact_available,
    optimize_bins,
    rust_backend,  # noqa: E402
)
from src.cutting.search import ENGINE_VERSION  # noqa: E402

# Parámetros de corte de producción (settings.kerf y los cuatro trims), fijos
# aquí para no depender de la base: si se cambian en la app, pasar --kerf.
# 4.0 desde 2026-08-13: se confirmó con el operario que la sierra corta a 4mm,
# y medir a 5 da números que no son los de producción.
KERF = 4.0
TRIM = 10.0
HALF_MARKUP = 0.10
SHEET_W, SHEET_H = 2070.0, 2800.0
# Mismos knobs que config.OPT_TRIES_PER_BOARD / OPT_SEARCH_ITERATIONS.
TRIES_PER_BOARD = 48
SEARCH_ITERATIONS = 40

# Despieces reales exportados por el programa comercial (pruebas/*.xml).
# ``expected``      = tableros que facturó ese programa, según el nombre del
#                     archivo (8.5 = 8 enteros + 1 medio).
# ``local_seconds`` = medición de referencia en un Apple M1 con este mismo
#                     motor; es contra esto que se calcula el multiplicador.
JOBS = [
    {
        "job": "preorden-24",
        "file": "3 JAPANDI Y 6 BLANCO RH15MM_3JAPANDI 36MM_1 BLNORMAL_03-08-2026.xml",
        "pools": [
            {
                "material": "JAPANDI RH 15MM",
                "code": "JAP-15R",
                "thickness": 15,
                "price": 68.0,
                "expected": 3,
                "local_seconds": 8.52,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (615, 2000, 2, 0),
                    (615, 750, 2, 0),
                    (1385, 220, 2, 0),
                    (248, 530, 4, 0),
                    (248, 1893, 4, 0),
                    (646, 211, 6, 0),
                    (646, 186, 2, 0),
                    (321, 1059, 4, 0),
                    (321, 530, 4, 0),
                    (395, 443, 6, 0),
                    (493, 513, 3, 0),
                    (115, 2050, 2, 0),
                    (115, 880, 1, 0),
                    (115, 830, 1, 0),
                    (115, 2050, 2, 0),
                    (115, 670, 1, 0),
                    (115, 2050, 2, 0),
                    (125, 660, 2, 0),
                    (125, 2050, 2, 0),
                    (125, 860, 1, 0),
                    (125, 2050, 2, 0),
                    (70, 2780, 7, 0),
                ],
            },
            {
                "material": "BLANCO RH 15MM",
                "code": "BNV-15R",
                "thickness": 15,
                "price": 56.0,
                "expected": 6,
                "local_seconds": 16.56,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (505, 600, 4, 0),
                    (650, 600, 6, 0),
                    (600, 810, 4, 0),
                    (564, 170, 16, 0),
                    (540, 170, 16, 0),
                    (385, 2420, 4, 0),
                    (200, 370, 12, 0),
                    (200, 2420, 2, 0),
                    (70, 1870, 8, 0),
                    (70, 1045, 8, 0),
                    (70, 620, 4, 0),
                    (70, 475, 4, 0),
                    (70, 750, 8, 0),
                    (70, 620, 4, 0),
                    (70, 475, 4, 0),
                    (480, 500, 6, 0),
                    (480, 800, 3, 0),
                    (500, 635, 2, 0),
                    (500, 1900, 1, 0),
                    (500, 810, 1, 0),
                    (168, 648, 4, 0),
                    (170, 450, 8, 0),
                    (170, 564, 8, 0),
                    (500, 550, 1, 0),
                    (500, 1200, 1, 0),
                    (400, 1900, 1, 0),
                    (400, 1485, 2, 0),
                    (400, 985, 1, 0),
                    (70, 2420, 10, 0),
                ],
            },
            {
                "material": "JAPANDI RH 36MM",
                "code": "JAP-36R",
                "thickness": 36,
                "price": 132.0,
                "expected": 3,
                "local_seconds": 0.61,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (851, 2019, 1, 0),
                    (651, 2019, 1, 0),
                    (661, 2019, 1, 0),
                    (821, 2019, 1, 0),
                    (871, 2019, 1, 0),
                    (80, 2780, 16, 0),
                ],
            },
            {
                "material": "BLANCO NORMAL",
                "code": "BNV-15",
                "thickness": 15,
                "price": 48.0,
                "expected": 1,
                "local_seconds": 0.005,
                # (ancho, alto, cantidad, rotable)
                "parts": [(564, 420, 4, 1), (504, 564, 8, 0)],
            },
        ],
    },
    {
        "job": "japandi-cashmere-blanco",
        "file": "3JAPANDI_2CASHMERE_5BLANCOS RH 15MM-2-07-2026.xml",
        "pools": [
            {
                "material": "JAPANDI RH 15MM",
                "code": "JAP-15R",
                "thickness": 15,
                "price": 68.0,
                "expected": 3,
                "local_seconds": 5.75,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (452, 2147, 2, 0),
                    (600, 2250, 1, 0),
                    (997, 407, 1, 0),
                    (350, 800, 2, 0),
                    (250, 335, 4, 0),
                    (250, 800, 1, 0),
                    (282, 737, 1, 0),
                    (422, 737, 2, 0),
                    (572, 737, 3, 0),
                    (657, 737, 4, 0),
                    (340, 1650, 1, 0),
                    (340, 400, 5, 0),
                    (430, 1650, 1, 0),
                    (300, 1000, 1, 0),
                    (300, 135, 2, 0),
                    (150, 1000, 1, 0),
                    (70, 2720, 21, 0),
                ],
            },
            {
                "material": "CASHMERE RH 15MM",
                "code": "CSH-15R",
                "thickness": 15,
                "price": 56.0,
                "expected": 2,
                "local_seconds": 1.01,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (327, 797, 2, 0),
                    (350, 800, 2, 0),
                    (392, 757, 1, 0),
                    (272, 797, 1, 0),
                    (332, 797, 2, 0),
                    (347, 797, 3, 0),
                    (370, 2780, 2, 0),
                    (370, 320, 4, 0),
                    (370, 350, 1, 0),
                    (347, 822, 3, 0),
                    (70, 2720, 20, 0),
                ],
            },
            {
                "material": "BLANCO RH 15MM",
                "code": "BNV-15R",
                "thickness": 15,
                "price": 56.0,
                "expected": 5,
                "local_seconds": 28.89,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (600, 2150, 2, 1),
                    (600, 885, 2, 1),
                    (450, 885, 4, 1),
                    (445, 450, 1, 1),
                    (450, 680, 2, 1),
                    (410, 450, 1, 1),
                    (325, 370, 1, 1),
                    (160, 325, 6, 1),
                    (160, 400, 6, 1),
                    (177, 402, 3, 1),
                    (600, 1005, 2, 1),
                    (600, 385, 3, 1),
                    (200, 400, 2, 1),
                    (200, 385, 1, 1),
                    (350, 800, 8, 1),
                    (350, 635, 2, 1),
                    (250, 635, 2, 1),
                    (350, 735, 2, 1),
                    (350, 400, 2, 1),
                    (250, 350, 2, 1),
                    (250, 250, 2, 1),
                    (350, 675, 2, 1),
                    (250, 675, 2, 1),
                    (350, 340, 2, 1),
                    (250, 440, 2, 1),
                    (350, 1030, 2, 1),
                    (250, 1030, 2, 1),
                    (600, 735, 2, 1),
                    (600, 580, 1, 1),
                    (100, 495, 2, 1),
                    (100, 470, 2, 1),
                    (160, 495, 4, 1),
                    (160, 470, 4, 1),
                    (440, 495, 3, 1),
                    (1922, 1452, 1, 1),
                    (70, 2420, 11, 0),
                ],
            },
        ],
    },
    {
        "job": "macas-ristretto-blanco",
        "file": "4 RISTRETTOS Y 11 BLANCOS RH 15MM-MACAS_08-07-2026.xml",
        "pools": [
            {
                "material": "RISTRETTO RH 15MM",
                "code": "BRR-15R",
                "thickness": 15,
                "price": 72.0,
                "expected": 4,
                "local_seconds": 8.31,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (447, 587, 6, 0),
                    (447, 1887, 2, 0),
                    (447, 1072, 4, 0),
                    (447, 197, 16, 0),
                    (547, 1777, 2, 0),
                    (662, 197, 4, 0),
                    (662, 962, 1, 0),
                    (232, 417, 6, 0),
                    (387, 202, 4, 0),
                    (620, 2700, 2, 0),
                    (620, 1830, 1, 0),
                    (620, 1880, 1, 0),
                    (500, 450, 3, 0),
                    (70, 2780, 22, 0),
                ],
            },
            {
                "material": "BLANCO RH 15MM",
                "code": "BNV-15R",
                "thickness": 15,
                "price": 56.0,
                "expected": 11,
                "local_seconds": 16.57,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (600, 1900, 8, 0),
                    (600, 440, 4, 0),
                    (600, 790, 2, 0),
                    (600, 670, 2, 0),
                    (600, 440, 4, 0),
                    (600, 1665, 4, 0),
                    (600, 555, 2, 0),
                    (600, 455, 8, 0),
                    (600, 790, 8, 0),
                    (600, 1770, 1, 0),
                    (600, 555, 2, 0),
                    (480, 450, 9, 0),
                    (480, 435, 3, 0),
                    (480, 370, 2, 0),
                    (70, 2420, 32, 0),
                    (450, 368, 12, 0),
                    (450, 583, 12, 0),
                    (210, 450, 1, 0),
                    (370, 313, 4, 0),
                    (160, 480, 32, 0),
                    (150, 313, 8, 0),
                    (160, 368, 32, 0),
                    (160, 480, 32, 0),
                    (450, 440, 4, 0),
                    (450, 540, 2, 0),
                    (160, 583, 32, 0),
                    (480, 450, 2, 0),
                    (210, 465, 2, 0),
                    (150, 400, 8, 0),
                ],
            },
        ],
    },
    {
        "job": "japandi-blanco",
        "file": "5JAPANDI Y 4 BLANCO RH 15MM-28-07-2026.xml",
        "pools": [
            {
                "material": "JAPANDI 15MM",
                "code": "JAP-15",
                "thickness": 15,
                "price": 58.0,
                "expected": 5,
                "local_seconds": 8.91,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (422, 747, 6, 0),
                    (422, 1887, 6, 0),
                    (662, 747, 3, 0),
                    (662, 1072, 3, 0),
                    (662, 197, 12, 0),
                    (520, 2770, 1, 0),
                    (200, 470, 14, 0),
                    (200, 185, 4, 0),
                    (520, 2770, 4, 0),
                    (600, 2770, 1, 0),
                    (315, 2770, 1, 0),
                    (300, 450, 6, 0),
                    (300, 200, 2, 0),
                    (70, 2780, 16, 0),
                ],
            },
            {
                "material": "BLANCO RH 15MM",
                "code": "BNV-15R",
                "thickness": 15,
                "price": 56.0,
                "expected": 4,
                "local_seconds": 15.02,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (70, 2420, 16, 0),
                    (430, 500, 12, 1),
                    (670, 500, 3, 0),
                    (500, 670, 6, 0),
                    (500, 790, 6, 0),
                    (160, 585, 24, 1),
                    (160, 450, 24, 1),
                    (420, 585, 12, 1),
                    (400, 450, 6, 0),
                ],
            },
        ],
    },
    {
        "job": "ristretto-8y medio",
        "file": "8 Y MEDIO RISTRETTO RH15MM_16-06-2026.xml",
        "pools": [
            {
                "material": "RISTRETO RH 15MM",
                "code": "BRR-15R",
                "thickness": 15,
                "price": 72.0,
                "expected": 8.5,
                "local_seconds": 8.67,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (450, 1680, 12, 0),
                    (450, 1150, 6, 0),
                    (450, 1120, 6, 0),
                    (1120, 1570, 6, 0),
                    (434, 1570, 6, 0),
                    (434, 700, 6, 0),
                    (434, 404, 18, 0),
                    (150, 613, 24, 0),
                    (150, 400, 24, 0),
                    (369, 613, 12, 0),
                    (693, 193, 12, 0),
                    (90, 1120, 6, 0),
                ],
            },
        ],
    },
    {
        "job": "blanco-9",
        "file": "9 BLANCOS RH 15MM_03-06-2026.xml",
        "pools": [
            {
                "material": "BLANCO RH",
                "code": "BNV-15R",
                "thickness": 15,
                "price": 56.0,
                "expected": 9,
                "local_seconds": 15.19,
                # (ancho, alto, cantidad, rotable)
                "parts": [
                    (350, 1060, 6, 1),
                    (350, 770, 12, 1),
                    (800, 1060, 3, 1),
                    (800, 1275, 4, 1),
                    (800, 930, 4, 1),
                    (1245, 930, 2, 1),
                    (350, 615, 16, 1),
                    (1250, 1300, 1, 1),
                    (200, 1300, 2, 0),
                    (500, 1520, 2, 1),
                    (500, 885, 2, 1),
                    (500, 790, 2, 1),
                    (500, 470, 1, 1),
                    (500, 270, 1, 1),
                    (500, 130, 1, 1),
                    (100, 330, 4, 0),
                    (100, 400, 4, 0),
                    (100, 335, 2, 0),
                    (380, 345, 2, 1),
                    (147, 407, 2, 0),
                    (262, 432, 1, 0),
                    (262, 327, 1, 0),
                    (500, 1000, 2, 1),
                    (500, 810, 2, 1),
                    (380, 810, 2, 1),
                    (840, 1000, 1, 1),
                    (400, 1515, 2, 1),
                    (400, 845, 8, 1),
                    (400, 930, 5, 1),
                    (400, 1275, 2, 1),
                    (960, 1275, 1, 1),
                    (70, 2400, 22, 1),
                ],
            },
        ],
    },
]


def build_pool(pool):
    """(piezas, [tablero entero, medio tablero]) para un pool embebido."""
    pieces = [
        Piece(
            id=f"p{i}",
            width=float(w),
            height=float(h),
            quantity=q,
            can_rotate=bool(r),
        )
        for i, (w, h, q, r) in enumerate(pool["parts"])
    ]
    full = BinSpec(
        key=pool["material"],
        width=SHEET_W,
        height=SHEET_H,
        thickness=float(pool["thickness"]),
        cost_per_unit=pool["price"],
    )
    half = BinSpec(
        key=pool["material"],
        width=SHEET_W / 2.0,
        height=SHEET_H,
        thickness=float(pool["thickness"]),
        cost_per_unit=round(pool["price"] / 2.0 * (1 + HALF_MARKUP), 2),
        half_board=True,
    )
    return pieces, [full, half]


def _fmt(value):
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def run(jobs, params):
    results = []
    for job in jobs:
        print(f"\n{job['job']}  ({sum(len(p['parts']) for p in job['pools'])} tipos)")
        print(
            f"  {'material':<20}{'piezas':>7}{'tableros':>10}{'comercial':>11}"
            f"{'M1':>8}{'aquí':>9}{'factor':>8}"
        )
        for pool in job["pools"]:
            pieces, bins = build_pool(pool)
            instances = sum(p.quantity for p in pieces)
            budget = SearchBudget.scaled(
                instances,
                tries_per_board=TRIES_PER_BOARD,
                iterations=SEARCH_ITERATIONS,
            )
            started = time.perf_counter()
            layouts, unplaced = optimize_bins(
                pieces,
                bins,
                cutting_params=params,
                budget=budget,
                exact_config=ExactConfig(),
            )
            elapsed = time.perf_counter() - started

            halves = sum(1 for la in layouts if la.material.half_board)
            fulls = len(layouts) - halves
            ours = fulls + 0.5 * halves
            local = pool["local_seconds"]
            # Pools de menos de un segundo no dicen nada sobre la CPU: el ruido
            # domina, así que no entran en el factor.
            ratio = elapsed / local if local >= 1.0 else None
            label = f"{fulls}" + (f"+{halves}/2" if halves else "")
            print(
                f"  {pool['material']:<20}{instances:>7}{label:>10}"
                f"{_fmt(pool['expected']):>11}{local:>7.1f}s{elapsed:>8.1f}s"
                f"{('-' if ratio is None else f'{ratio:.2f}x'):>8}"
                f"{'' if ours <= pool['expected'] else '  PEOR'}"
            )
            if unplaced:
                print(f"    !! {len(unplaced)} piezas SIN UBICAR")
            results.append(
                {
                    "job": job["job"],
                    "material": pool["material"],
                    "pieces": instances,
                    "boards": ours,
                    "expected": pool["expected"],
                    "local_seconds": local,
                    "seconds": round(elapsed, 2),
                    "counts_for_ratio": ratio is not None,
                    "unplaced": len(unplaced),
                }
            )
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark del optimizador contra los despieces reales."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="los 6 trabajos del taller (puede superar los 10 min); por defecto "
        "corre sólo la pre-orden 24",
    )
    parser.add_argument(
        "--kerf",
        type=float,
        default=KERF,
        help=f"ancho de sierra en mm (default {KERF}, la sierra real del "
        "taller); en los packs apretados, pasar a 5 cuesta medio tablero",
    )
    parser.add_argument("--json", action="store_true", help="salida JSON cruda")
    args = parser.parse_args()

    params = CuttingParameters(
        kerf=args.kerf,
        top_trim=TRIM,
        bottom_trim=TRIM,
        left_trim=TRIM,
        right_trim=TRIM,
    )

    print(
        f"máquina : {platform.machine()} · {os.cpu_count()} cores · "
        f"{platform.system()} · python {platform.python_version()}"
    )
    backend = rust_backend.status()
    packing = (
        f"{backend['effective']} (wheel {backend['wheel_version']})"
        if backend["wheel_importable"]
        else f"{backend['effective']} (sin wheel)"
    )
    print(
        f"motor   : packing={packing} (pedido={backend['requested']}) · "
        f"ENGINE_VERSION={ENGINE_VERSION} · "
        f"CP-SAT={'sí' if exact_available() else 'NO (sólo heurísticas)'}"
    )
    print(
        f"corte   : kerf={_fmt(params.kerf)} · trims={_fmt(TRIM)} · "
        f"tablero {SHEET_W:.0f}x{SHEET_H:.0f}"
    )

    jobs = JOBS if args.all else JOBS[:1]
    if not args.all:
        print("modo    : sólo pre-orden 24 (usá --all para los 6 trabajos)")

    started = time.perf_counter()
    results = run(jobs, params)
    wall = time.perf_counter() - started

    ours = sum(r["boards"] for r in results)
    theirs = sum(r["expected"] for r in results)
    here = sum(r["seconds"] for r in results)
    timed = [r for r in results if r["counts_for_ratio"]]
    worse = [r for r in results if r["boards"] > r["expected"]]
    lost = [r for r in results if r["unplaced"]]

    print("\n" + "=" * 74)
    print(
        f"  tableros : {_fmt(ours)} nuestros vs {_fmt(theirs)} del comercial "
        f"({ours - theirs:+g})"
    )
    print(f"  cómputo  : {here:.1f}s  (reloj total {wall:.1f}s)")
    if timed:
        factor = sum(r["seconds"] for r in timed) / sum(
            r["local_seconds"] for r in timed
        )
        print(
            f"  \033[1mfactor   : esta máquina es {factor:.2f}x más lenta "
            f"que la de desarrollo\033[0m"
        )
        if not args.all:
            print(
                f"             (la pre-orden 24 completa cuesta {here:.0f}s de "
                f"cómputo acá)"
            )

    if lost:
        print("\n  ❌ HAY PIEZAS SIN UBICAR — el resultado no es utilizable.")
    elif worse:
        print(
            "\n  ⚠️  Peor que el comercial en: "
            + ", ".join(f"{r['job']}/{r['material']}" for r in worse)
        )
    else:
        print("\n  ✅ Igual o mejor que el comercial en todos los materiales.")

    if args.json:
        print("\n" + json.dumps(results, ensure_ascii=False))
    return 1 if (worse or lost) else 0


if __name__ == "__main__":
    raise SystemExit(main())
