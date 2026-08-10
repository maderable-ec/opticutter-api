"""Parity harness over the shop's real cut lists exported by the commercial program.

``pruebas/*.xml`` are the actual jobs Maderable ran through the commercial
optimizer, and each **filename records how many boards that program billed**
("3 JAPANDI Y 6 BLANCO RH15MM_3JAPANDI 36MM_1 BLNORMAL" = 3 + 6 + 3 + 1). That
makes them the only benchmark that matters commercially: our engine must use
the same number of boards or fewer, on every material of every job.

Unlike ``bench_battery.py`` (synthetic jobs, guards against *regressions*), this
one measures us against the competitor on real work.

The XML carries only ``<parts>`` — sheet size, kerf and trims come from the
app's own settings, so this script reads them from the database exactly as
``OptimizationService`` would, and resolves each part's ``<material>`` name
against the local product catalog. Names that have no catalog entry fall back to
a same-thickness stand-in: only geometry and price matter here, and every MDP
sheet in the catalog is 2070x2800.

Usage::

    python scripts/bench_shopfiles.py                  # all files
    python scripts/bench_shopfiles.py --only JAPANDI   # substring filter
"""

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "DATABASE_URL", "postgresql://cutter:cutter@localhost:5433/cutter_db"
)

from src.cutting import (  # noqa: E402
    BinSpec,
    CuttingParameters,
    ExactConfig,
    Piece,
    SearchBudget,
    optimize_bins,
)
from src.modules.products.model import ProductModel  # noqa: E402
from src.modules.settings.service import SettingsService  # noqa: E402
from src.shared.config import config  # noqa: E402
from src.shared.database import SessionLocal  # noqa: E402
from tests.unit.cutting_invariants import assert_valid_layouts  # noqa: E402

PRUEBAS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pruebas"
)

# Boards the commercial program billed, per file, as encoded in its name.
# "8 Y MEDIO" = 8 full boards + 1 half, hence the .5.
EXPECTED: Dict[str, "OrderedDict[str, float]"] = {
    "9 BLANCOS RH 15MM_03-06-2026.xml": OrderedDict([("BLANCO RH", 9)]),
    "4 RISTRETTOS Y 11 BLANCOS RH 15MM-MACAS_08-07-2026.xml": OrderedDict(
        [("RISTRETTO RH 15MM", 4), ("BLANCO RH 15MM", 11)]
    ),
    "5JAPANDI Y 4 BLANCO RH 15MM-28-07-2026.xml": OrderedDict(
        [("JAPANDI 15MM", 5), ("BLANCO RH 15MM", 4)]
    ),
    "8 Y MEDIO RISTRETTO RH15MM_16-06-2026.xml": OrderedDict(
        [("RISTRETO RH 15MM", 8.5)]
    ),
    "3JAPANDI_2CASHMERE_5BLANCOS RH 15MM-2-07-2026.xml": OrderedDict(
        [("JAPANDI RH 15MM", 3), ("CASHMERE RH 15MM", 2), ("BLANCO RH 15MM", 5)]
    ),
    "3 JAPANDI Y 6 BLANCO RH15MM_3JAPANDI 36MM_1 BLNORMAL_03-08-2026.xml": OrderedDict(
        [
            ("JAPANDI RH 15MM", 3),
            ("BLANCO RH 15MM", 6),
            ("JAPANDI RH 36MM", 3),
            ("BLANCO NORMAL", 1),
        ]
    ),
}

# The shop's material names vs the catalog's ``family`` attribute. "BLANCO" is
# the shop's shorthand for Blanco Nieve; "RISTRETTO" (also spelled RISTRETO in
# one export) is the Barroco Ristretto decor.
FAMILY_ALIASES = [
    ("JAPANDI", "Japandi"),
    ("CASHMERE", "Cashmere"),
    ("BLANCO", "Blanco Nieve"),
    ("RISTRETTO", "Barroco Ristretto"),
    ("RISTRETO", "Barroco Ristretto"),
]


def parse_parts(path: str) -> "OrderedDict[str, List[Piece]]":
    """Groups a commercial export's parts into one piece pool per material.

    ``<length>`` is the grain axis and maps to our ``height``; ``<width>`` to
    ``width``; ``allow_rotation`` to ``can_rotate``. Verified against pre-order
    24, whose stored requirements reproduce this file field for field.
    """
    root = ET.parse(path).getroot()
    pools: "OrderedDict[str, List[Piece]]" = OrderedDict()
    for index, row in enumerate(root.find("parts")):
        get = {child.tag: child.text for child in row}
        if get.get("useit") == "0":
            continue
        material = (get["material"] or "").strip()
        pools.setdefault(material, []).append(
            Piece(
                id=f"{index}-{(get.get('label') or '').strip()}",
                width=float(get["width"]),
                height=float(get["length"]),
                quantity=int(get["quantity"]),
                can_rotate=get.get("allow_rotation") == "1",
            )
        )
    return pools


def resolve_board(db, material: str) -> Tuple[ProductModel, bool]:
    """Catalog board for a shop material name; ``False`` if it is a stand-in.

    Matches on the catalog ``family`` plus the two axes the name really encodes:
    thickness (``36MM``) and whether it is a woodgrain (``RH``) sheet, which is
    what makes the price differ.
    """
    upper = material.upper()
    thickness = 36 if "36" in upper else 15
    grain = bool(re.search(r"\bRH\b", upper))

    # ``attributes`` is a plain JSON column, so the thickness filter happens in
    # Python; the board catalog is a few dozen rows.
    boards = [
        b
        for b in db.query(ProductModel).filter(ProductModel.type == "board").all()
        if int((b.attributes or {}).get("thickness", 0)) == thickness
    ]
    grained = [b for b in boards if b.code.endswith("R")]
    plain = [b for b in boards if not b.code.endswith("R")]
    candidates = (grained if grain else plain) or boards

    for token, family in FAMILY_ALIASES:
        if token in upper:
            for board in candidates:
                if (board.attributes or {}).get("family") == family:
                    return board, True
    # No catalog entry for this decor: any same-thickness sheet has the same
    # geometry, so the board count is unaffected.
    return candidates[0], False


def run_file(path: str, params: CuttingParameters, markup: float, db) -> dict:
    name = os.path.basename(path)
    expected = EXPECTED.get(name, {})
    pools = parse_parts(path)

    print(f"\n\033[1m{name}\033[0m")
    print(
        f"  {'material':<20}{'piezas':>7}{'nuestro':>9}{'comercial':>11}"
        f"{'tiempo':>9}{'efic.':>8}  tablero"
    )

    rows = []
    total_ours = total_theirs = 0.0
    for material, pieces in pools.items():
        board, exact_match = resolve_board(db, material)
        attrs = board.attributes or {}
        full = BinSpec(
            key=material,
            width=float(attrs["width"]),
            height=float(attrs["height"]),
            thickness=float(attrs["thickness"]),
            cost_per_unit=float(board.price),
        )
        half = BinSpec(
            key=material,
            width=full.width / 2.0,
            height=full.height,
            thickness=full.thickness,
            cost_per_unit=round(full.cost_per_unit / 2.0 * (1 + markup), 2),
            half_board=True,
        )
        instances = sum(p.quantity for p in pieces)
        budget = SearchBudget.scaled(
            instances,
            tries_per_board=config.OPT_TRIES_PER_BOARD,
            iterations=config.OPT_SEARCH_ITERATIONS,
        )
        started = time.perf_counter()
        layouts, unplaced = optimize_bins(
            pieces,
            [full, half],
            cutting_params=params,
            budget=budget,
            exact_config=ExactConfig(
                enabled=config.OPT_EXACT_ENABLED,
                max_pieces=config.OPT_EXACT_MAX_PIECES,
                max_calls=config.OPT_EXACT_MAX_CALLS,
                deterministic_time=config.OPT_EXACT_DETERMINISTIC_TIME,
            ),
        )
        elapsed = time.perf_counter() - started

        # Board counts only mean something if the layouts are physically real:
        # same bounds/kerf/rotation/conservation check the engine's own tests use.
        assert_valid_layouts(layouts, unplaced, params, instances)

        halves = sum(1 for la in layouts if la.material.half_board)
        fulls = len(layouts) - halves
        ours = fulls + 0.5 * halves
        theirs = expected.get(material)
        used = sum(la.used_area for la in layouts)
        capacity = sum(la.material.area for la in layouts)

        verdict = "?"
        if theirs is not None:
            total_ours += ours
            total_theirs += theirs
            verdict = "OK" if ours <= theirs else "PEOR"

        label = f"{fulls}" + (f"+{halves}/2" if halves else "")
        print(
            f"  {material:<20}{instances:>7}{label:>9}"
            f"{('-' if theirs is None else _fmt(theirs)):>11}"
            f"{elapsed:>8.2f}s{used / capacity * 100:>7.1f}%  "
            f"{board.code}{'' if exact_match else ' (sustituto)'}  {verdict}"
        )
        rows.append(
            {
                "material": material,
                "pieces": instances,
                "ours": ours,
                "theirs": theirs,
                "seconds": elapsed,
                "unplaced": len(unplaced),
                "layouts": layouts,
                "params": params,
            }
        )
        if unplaced:
            print(f"    !! {len(unplaced)} piezas sin ubicar")

    return {"name": name, "rows": rows, "ours": total_ours, "theirs": total_theirs}


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default=None, help="substring filter on the filename")
    parser.add_argument(
        "--kerf",
        type=float,
        default=None,
        help="override the configured blade width (mm). On packs above ~94% "
        "this is worth whole boards, so it is worth confirming against the "
        "saw the shop actually runs.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    settings = SettingsService(db).get_or_init()
    params = CuttingParameters(
        kerf=settings.kerf if args.kerf is None else args.kerf,
        top_trim=settings.top_trim,
        bottom_trim=settings.bottom_trim,
        left_trim=settings.left_trim,
        right_trim=settings.right_trim,
    )
    print(
        f"kerf={params.kerf} trims={params.top_trim}/{params.bottom_trim}/"
        f"{params.left_trim}/{params.right_trim} "
        f"markup_medio_tablero={settings.half_board_markup_pct}"
    )

    files = sorted(f for f in os.listdir(PRUEBAS) if f.endswith(".xml"))
    if args.only:
        files = [f for f in files if args.only.lower() in f.lower()]

    results = [
        run_file(os.path.join(PRUEBAS, f), params, settings.half_board_markup_pct, db)
        for f in files
    ]

    print("\n" + "=" * 78)
    grand_ours = grand_theirs = 0.0
    worse = []
    for result in results:
        grand_ours += result["ours"]
        grand_theirs += result["theirs"]
        delta = result["ours"] - result["theirs"]
        flag = "OK" if delta <= 0 else "PEOR"
        if delta > 0:
            worse.append(result["name"])
        seconds = sum(r["seconds"] for r in result["rows"])
        print(
            f"  {_fmt(result['ours']):>6} vs {_fmt(result['theirs']):>4} tableros "
            f"({delta:+g})  {seconds:>6.1f}s  {flag}  {result['name'][:46]}"
        )
    print("=" * 78)
    print(
        f"  TOTAL: {_fmt(grand_ours)} tableros vs {_fmt(grand_theirs)} del comercial "
        f"({grand_ours - grand_theirs:+g})"
    )
    if worse:
        print(f"\n  ❌ Peores en: {', '.join(worse)}")
        return 1
    print("\n  ✅ Igual o mejor en todos los archivos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
