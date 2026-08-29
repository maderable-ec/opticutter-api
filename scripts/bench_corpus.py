"""Runs the engine against the commercial program over the shop's whole 2026 corpus.

``scripts/bench_shopfiles.py`` does this for **8 files**, with the board counts
typed by hand into an ``EXPECTED`` dict. Eight jobs cannot tell a real gain from
noise, and every added file costs a manual entry. This runs the same comparison
over the ~860 exports the shop accumulated in 2026, reading each label off the
filename (``scripts/corpus_labels``) and each sheet off a pinned map
(``scripts/corpus_sheets``).

**It reports two columns, not one.** Boards and seconds are one trade-off, not
two subjects: ``SearchBudget.scaled`` buys flat latency by shrinking the search
on big jobs (``factor = 140/n_pieces``), and big jobs are exactly where the
commercial program still beats us. A change that wins boards is only a win if
the clock says what it cost, so both are on the same screen and both are in the
baseline diff.

Not every export can be scored, and the ones that cannot are counted out loud --
how much of the corpus we refuse to grade is a property of the benchmark, not a
detail. A job is dropped when its name is a leftover cut, when the name does not
state quantities we can attribute, when a material has no pinned sheet, or when
the label is **physically impossible**: ``"1 CASHMERE RH 15MM"`` over 12.72 m² of
parts needs about three boards, so that name was pasted from another job and
scoring it would quietly credit us with two boards we never saved.

**The headline is two numbers, not one**, and ``corpus_scoring`` explains why:
the corpus splits into pools whose commercial label sits above the pure-area
bound -- where their number paid for a cutting decision and ours can be graded
against it -- and pools whose label *is* that bound, where the gap measures our
distance to a minimum almost nobody reaches. Summed together they read as a 3.1%
deficit; apart, we are ahead on the first population and the whole deficit lives
in the second.

Usage::

    python scripts/bench_corpus.py --refresh-sheets      # build the sheet map
    python scripts/bench_corpus.py                       # score pruebas/
    python scripts/bench_corpus.py --corpus ~/Downloads/1.CORTES\\ SUCUA\\ 2026
    python scripts/bench_corpus.py --only BLANCO         # one slice
    python scripts/bench_corpus.py --save-baseline benchmarks/baseline-corpus.json
    python scripts/bench_corpus.py --baseline benchmarks/baseline-corpus.json
    python scripts/bench_corpus.py --trims 0/0/0/0        # sensibilidad al desbaste
"""

import argparse
import hashlib
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault(
    "DATABASE_URL", "postgresql://cutter:cutter@localhost:5433/cutter_db"
)

import corpus_scoring  # noqa: E402
import corpus_sheets  # noqa: E402
from corpus_labels import label, material_key  # noqa: E402

from src.cutting import (  # noqa: E402
    BinSpec,
    CuttingParameters,
    ExactConfig,
    Piece,
    SearchBudget,
    optimize_bins,
)
from src.cutting.search import ENGINE_VERSION  # noqa: E402
from src.shared.config import config  # noqa: E402
from tests.unit.cutting_invariants import assert_valid_layouts  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CORPUS = os.path.join(REPO, "pruebas")

IMPOSSIBLE = "etiqueta imposible en esa lámina"
BELOW_FLOOR = "etiqueta bajo el piso de área útil"
NO_SHEET = "material sin lámina en el mapa"
UNPLACED = "el motor dejó piezas sin ubicar"


@dataclass
class Pool:
    """One material of one export, with the boards the commercial billed for it."""

    job: str
    material: str
    key: str
    pieces: List[Piece]
    instances: int
    theirs: float
    sheet: dict


def parse_parts(path: str) -> "OrderedDict[str, List[Piece]]":
    """Groups an export's parts into one piece pool per material.

    ``<length>`` is the grain axis and maps to our ``height``; ``<width>`` to
    ``width``; ``allow_rotation`` to ``can_rotate``. Verified against pre-order
    24, whose stored requirements reproduce this file field for field. Rows
    flagged ``useit=0`` are the ones the operator switched off before cutting.
    """
    pools: "OrderedDict[str, List[Piece]]" = OrderedDict()
    for index, row in enumerate(ET.parse(path).getroot().find("parts")):
        cell = {child.tag: child.text for child in row}
        if cell.get("useit") == "0":
            continue
        material = (cell.get("material") or "").strip()
        pools.setdefault(material, []).append(
            Piece(
                id=f"{index}-{(cell.get('label') or '').strip()}",
                width=float(cell["width"]),
                height=float(cell["length"]),
                quantity=int(cell["quantity"]),
                can_rotate=cell.get("allow_rotation") == "1",
            )
        )
    return pools


def _fits(pieces: List[Piece], sheet: dict, params: CuttingParameters) -> bool:
    """Every part fits the sheet's *usable* area, in an orientation it allows.

    Both qualifiers are load-bearing. Trims cost 20mm of each axis, and a part
    cut to the full sheet width fails on them alone. Rotation is a property of
    the part: 65% of the corpus is ``allow_rotation=0`` because the decor has a
    grain, so a part that would fit sideways still does not fit.

    Getting this wrong does not raise -- the engine simply returns the part as
    unplaced, and a pool that left parts on the floor *looks like a win*: fewer
    boards for less work. That is how this run first reported -14.1% against
    the commercial program while dropping 164 parts.
    """
    usable_width = sheet["width"] - params.left_trim - params.right_trim
    usable_height = sheet["height"] - params.top_trim - params.bottom_trim
    for piece in pieces:
        upright = piece.width <= usable_width and piece.height <= usable_height
        sideways = (
            piece.can_rotate
            and piece.height <= usable_width
            and piece.width <= usable_height
        )
        if not (upright or sideways):
            return False
    return True


# How far above the area floor a believable bill can sit. The commercial
# program packs in the 70-90% range, so even a bad plan lands near 1.4x the
# floor; 4x is not a tolerance, it is a nonsense detector.
_ABSURD = 4.0


def _reject(
    pieces: List[Piece], sheet: dict, theirs: float, params: CuttingParameters
) -> Optional[str]:
    """Why the commercial program's count could not have held these parts, or None.

    Gross geometry only -- no kerf, no packing. Anything this rejects is a
    label that does not belong to this file, not a plan we happened to beat.

    The test runs in **both directions**, and the upper one matters more.
    Too-few-boards is the obvious error: ``"1 CASHMERE RH 15MM"`` over 12.72 m²
    of parts needs about three. Too-many is the expensive one, because it
    scores as a landslide win -- ``"PUERTAS 36 BALNCO"`` is thirty-six *doors*
    cut from three parts, and reading it as thirty-six boards handed us a
    34-board victory over a job that never existed.

    The lower gate measures against the **usable** board, not the nominal one.
    The shop really does dress 10mm off each edge, so 1.8% of every sheet was
    never available to either program; against the raw sheet eleven labels in
    the 2026 corpus survive that cannot be cut at all, and they scored as
    5.5 boards of deficit. Its own reason code keeps that count visible instead
    of folding it into the generic one.
    """
    if not _fits(pieces, sheet, params):
        return IMPOSSIBLE
    area = sum(p.width * p.height * p.quantity for p in pieces)
    if area > corpus_scoring.capacity(theirs, sheet, params) + 1e-6:
        return BELOW_FLOOR
    if theirs > _ABSURD * max(1.0, area / corpus_scoring.raw_area(sheet)):
        return IMPOSSIBLE
    return None


def collect(
    corpus: str, sheets: Dict[str, dict], only: Optional[str], params: CuttingParameters
) -> Tuple[List[Pool], Counter]:
    """Every scorable pool in the corpus, plus a tally of why the rest are not."""
    skipped: Counter = Counter()
    pools: List[Pool] = []
    paths = []
    for root, _, names in os.walk(corpus):
        paths.extend(os.path.join(root, n) for n in sorted(names) if n.endswith(".xml"))
    for path in sorted(paths):
        name = os.path.basename(path)
        if only and only.lower() not in name.lower():
            continue
        parsed = parse_parts(path)
        labels = label(name, list(parsed.keys()))
        if not labels.ok:
            skipped[labels.reason] += 1
            continue
        keys = {m: material_key(m) for m in parsed}
        if any(k not in sheets for k in keys.values()):
            skipped[NO_SHEET] += 1
            continue
        reasons = [
            _reject(pieces, sheets[keys[material]], labels.boards[material], params)
            for material, pieces in parsed.items()
        ]
        # One unscorable material disqualifies the whole export: its pools share
        # a filename, and the label is read off that name as a single claim.
        refused = next((r for r in reasons if r is not None), None)
        if refused is not None:
            skipped[refused] += 1
            continue
        for material, pieces in parsed.items():
            pools.append(
                Pool(
                    job=name,
                    material=material,
                    key=keys[material],
                    pieces=pieces,
                    instances=sum(p.quantity for p in pieces),
                    theirs=labels.boards[material],
                    sheet=sheets[keys[material]],
                )
            )
    return pools, skipped


def _bin_specs(pool: Pool, markup: float) -> List[BinSpec]:
    """The full catalog sheet plus the half sibling the business sells.

    Shared by the scoring run and the oracle so the two can never disagree about
    what a board of this pool costs -- a divergence here would show up as a fake
    win in exactly the pools the oracle exists to certify.
    """
    sheet = pool.sheet
    return [
        BinSpec(
            key=pool.material,
            width=sheet["width"],
            height=sheet["height"],
            thickness=sheet["thickness"],
            cost_per_unit=sheet["price"],
        ),
        BinSpec(
            key=pool.material,
            width=sheet["width"] / 2.0,
            height=sheet["height"],
            thickness=sheet["thickness"],
            cost_per_unit=round(sheet["price"] / 2.0 * (1 + markup), 2),
            half_board=True,
        ),
    ]


def _boards(layouts) -> float:
    """Board count in the shop's half-board granularity."""
    halves = sum(1 for layout in layouts if layout.material.half_board)
    return len(layouts) - halves + 0.5 * halves


def _optimize(task: Tuple[Pool, dict, float]) -> dict:
    """Packs one pool. Top-level so it survives the pickle to a worker process."""
    pool, params_kw, markup = task
    params = CuttingParameters(**params_kw)
    sheet = pool.sheet
    full, half = _bin_specs(pool, markup)
    budget = SearchBudget.scaled(
        pool.instances,
        tries_per_board=config.OPT_TRIES_PER_BOARD,
        iterations=config.OPT_SEARCH_ITERATIONS,
    )
    started = time.perf_counter()
    layouts, unplaced = optimize_bins(
        pool.pieces,
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

    # A board count means nothing unless the layouts are physically real: the
    # same bounds/kerf/rotation/conservation check the engine's own tests use.
    assert_valid_layouts(layouts, unplaced, params, pool.instances)

    boards = _boards(layouts)
    digest = hashlib.sha256(
        json.dumps(
            [lay.to_dict() for lay in layouts], sort_keys=True, default=str
        ).encode()
    ).hexdigest()[:16]
    used = sum(lay.used_area for lay in layouts)
    parts_area = sum(p.width * p.height * p.quantity for p in pool.pieces)
    return {
        "job": pool.job,
        "material": pool.material,
        "sheet": f"{sheet['width']:.0f}x{sheet['height']:.0f}",
        "code": sheet["code"],
        "pieces": pool.instances,
        "ours": boards,
        "theirs": pool.theirs,
        "seconds": round(elapsed, 3),
        "unplaced": len(unplaced),
        "used": used,
        "capacity": sum(lay.material.area for lay in layouts),
        # What the commercial's number is evidence OF -- see ``corpus_scoring``.
        # ``floor`` is the count area alone forces; ``kind`` says whether their
        # label paid for a cutting decision or sits exactly on that bound; and
        # ``their_eff`` is the share of the usable board their label implies
        # they filled, which is what separates a pack we lost from a number
        # somebody wrote down.
        "floor": corpus_scoring.area_floor(parts_area, sheet),
        "kind": corpus_scoring.classify(pool.theirs, parts_area, sheet),
        "their_eff": round(
            corpus_scoring.implied_efficiency(used, pool.theirs, sheet, params), 4
        ),
        "our_eff": round(
            corpus_scoring.implied_efficiency(used, boards, sheet, params), 4
        ),
        "digest": digest,
    }


# --- Oracle: which losses are PROVABLY winnable -----------------------------
#
# ``PLAUSIBLE_EFFICIENCY`` answers "could anyone have packed this?" with a
# statistic. The oracle answers it with a plan: it hunts for a cheaper solution
# for a pool we lose and then *verifies* it at the real kerf, so what it reports
# is engine debt we can chase, not a label we suspect.
#
# Two sources of candidates, and only the first earns its keep by default.
#
# Relaxing the blade is a genuine relaxation -- any plan that fits with kerf k
# fits with kerf k-1 -- so the relaxed search explores partitions the real search
# evicts, and a relaxed plan whose every board still closes at the real kerf is a
# certificate. That is how pre-order 3's 4-board white plan was found (pinned in
# ``tests/unit/test_cutting_search.py``), and it costs about one extra search per
# pool.
#
# The second is brute force at the real kerf: no re-validation needed, catches a
# loss that is only a budget away. It is behind ``--oracle-deep`` because it is
# ~20x the cost for very little: measured on 12 lost pools, 18x the CPU recovered
# 0.5 of 6.0 boards. Reach for it to ANSWER "is this just budget?", not to survey
# the corpus.
_ORACLE_RELAXATIONS = (1.0, 2.0)
_ORACLE_BUDGET_FACTOR = 6
_ORACLE_SEEDS = 3


def _revalidates(layouts, pieces_by_board, params) -> bool:
    """Does every board of a relaxed plan still close at the REAL kerf?

    Board by board, into one sheet of the same kind the relaxed plan used: a
    half that no longer closes must not be silently upgraded to a full one, or
    the certificate would be for a plan that costs more than it claims.
    """
    for layout, group in zip(layouts, pieces_by_board):
        one = BinSpec(
            key=layout.material.id,
            width=layout.material.width,
            height=layout.material.height,
            thickness=layout.material.thickness,
            cost_per_unit=layout.material.cost_per_unit,
            half_board=layout.material.half_board,
        )
        repacked, unplaced = optimize_bins(
            group,
            [one],
            cutting_params=params,
            budget=SearchBudget.scaled(
                len(group),
                tries_per_board=config.OPT_TRIES_PER_BOARD,
                iterations=config.OPT_SEARCH_ITERATIONS,
            ),
        )
        if unplaced or len(repacked) != 1:
            return False
        assert_valid_layouts(repacked, unplaced, params, len(group))
    return True


def _oracle(task: Tuple[Pool, dict, float, float, bool]) -> dict:
    """Certifies (or fails to certify) a cheaper plan for one lost pool."""
    pool, params_kw, markup, incumbent, deep = task
    params = CuttingParameters(**params_kw)
    bins = _bin_specs(pool, markup)
    started = time.perf_counter()
    best, via = incumbent, None
    # The question is "is this loss real debt", so a certificate that reaches
    # their label answers it: search past that and the expensive arm runs on
    # every pool the cheap one already settled.
    target = pool.theirs

    for delta in _ORACLE_RELAXATIONS:
        if best <= target:
            break
        kerf = params_kw["kerf"] - delta
        if kerf < 0:
            continue
        relaxed_params = CuttingParameters(**{**params_kw, "kerf": kerf})
        layouts, unplaced = optimize_bins(
            pool.pieces,
            bins,
            cutting_params=relaxed_params,
            budget=SearchBudget.scaled(
                pool.instances,
                tries_per_board=config.OPT_TRIES_PER_BOARD,
                iterations=config.OPT_SEARCH_ITERATIONS,
            ),
        )
        if unplaced or _boards(layouts) >= best:
            continue
        groups = [[pp.piece for pp in lay.placed_pieces] for lay in layouts]
        if _revalidates(layouts, groups, params):
            best, via = _boards(layouts), f"sierra -{delta:g}mm"

    for seed in range(_ORACLE_SEEDS if deep else 0):
        if best <= target:
            break
        layouts, unplaced = optimize_bins(
            pool.pieces,
            bins,
            cutting_params=params,
            budget=SearchBudget.scaled(
                pool.instances,
                tries_per_board=config.OPT_TRIES_PER_BOARD * _ORACLE_BUDGET_FACTOR,
                iterations=config.OPT_SEARCH_ITERATIONS * _ORACLE_BUDGET_FACTOR,
            ),
            seed=seed,
            exact_config=ExactConfig(
                enabled=config.OPT_EXACT_ENABLED,
                max_pieces=250,
                max_calls=400,
                deterministic_time=30.0,
                root_deterministic_time=30.0,
                root_patience=50,
            ),
        )
        if unplaced or _boards(layouts) >= best:
            continue
        assert_valid_layouts(layouts, unplaced, params, pool.instances)
        best, via = _boards(layouts), f"presupuesto x{_ORACLE_BUDGET_FACTOR}"

    return {
        "job": pool.job,
        "material": pool.material,
        "pieces": pool.instances,
        "ours": incumbent,
        "theirs": pool.theirs,
        "proven": best,
        "via": via,
        "seconds": round(time.perf_counter() - started, 3),
    }


def report_oracle(results: List[dict]) -> None:
    """The one headline the plausibility cutoff could only guess at."""
    won = [r for r in results if r["proven"] < r["ours"]]
    debt = sum(r["ours"] - r["proven"] for r in won)
    reached = [r for r in won if r["proven"] <= r["theirs"]]
    print()
    print("  ── ORÁCULO · derrotas con un plan mejor PROBADO " + "─" * 25)
    print(f"  {len(results)} pools perdidos examinados")
    print(
        f"     {len(won)} tienen un plan verificado más barato   "
        f"-{debt:g} tableros de deuda del motor"
    )
    print(f"     {len(reached)} de ellos alcanzan o superan la etiqueta comercial")
    for r in sorted(won, key=lambda r: r["proven"] - r["ours"])[:15]:
        print(
            f"      {r['job'][:46]:46} {r['material'][:14]:14} {r['pieces']:>3}pz  "
            f"{_fmt(r['ours'])} → {_fmt(r['proven'])}  (ellos {_fmt(r['theirs'])})"
            f"  vía {r['via']}"
        )
    if not won:
        print("      ninguno: lo que perdemos no es alcanzable por esta vía")


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def summarize(rows: List[dict]) -> List[dict]:
    """Pool rows folded into one row per export."""
    jobs: "OrderedDict[str, dict]" = OrderedDict()
    for row in rows:
        job = jobs.setdefault(
            row["job"],
            {
                "job": row["job"],
                "pools": 0,
                "pieces": 0,
                "ours": 0.0,
                "theirs": 0.0,
                "seconds": 0.0,
                "unplaced": 0,
                "digests": [],
            },
        )
        job["pools"] += 1
        job["pieces"] += row["pieces"]
        job["ours"] += row["ours"]
        job["theirs"] += row["theirs"]
        job["seconds"] += row["seconds"]
        job["unplaced"] += row["unplaced"]
        job["digests"].append(row["digest"])
    for job in jobs.values():
        job["seconds"] = round(job["seconds"], 3)
        job["digest"] = hashlib.sha256(
            "".join(job.pop("digests")).encode()
        ).hexdigest()[:16]
    return list(jobs.values())


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _report_populations(rows: List[dict]) -> None:
    """The two headlines, because the corpus is two populations (``corpus_scoring``).

    Adding them into one number is what produced "we are 3.1% behind": every
    board of that deficit comes from pools where the commercial's label is
    exactly the area bound, while on the pools where their label demonstrably
    cost them a cutting decision we are ahead. One of these grades the engine
    and the other grades our distance to a bound nobody reaches, so they are
    never summed here.
    """
    plan = [r for r in rows if r["kind"] == corpus_scoring.PLAN]
    floor = [r for r in rows if r["kind"] == corpus_scoring.FLOOR]

    def tally(group):
        won = sum(1 for r in group if r["ours"] < r["theirs"])
        tied = sum(1 for r in group if r["ours"] == r["theirs"])
        lost = sum(1 for r in group if r["ours"] > r["theirs"])
        return won, tied, lost, sum(r["ours"] - r["theirs"] for r in group)

    won, tied, lost, gap = tally(plan)
    print("  ── PLAN · lo que mide al motor " + "─" * 45)
    print(f"  {len(plan)} pools donde su etiqueta está POR ENCIMA del piso de área")
    print(
        f"     ganamos {won} · empatamos {tied} · perdemos {lost}   {gap:+g} tableros"
    )
    if plan:
        print(
            f"     empaque útil implícito · ellos {_mean([r['their_eff'] for r in plan]):.1%}"
            f" · nosotros {_mean([r['our_eff'] for r in plan]):.1%}"
        )
    print()

    _won, reached, missed, gap = tally(floor)
    print("  ── PISO · distancia al mínimo teórico " + "─" * 38)
    print(f"  {len(floor)} pools donde su etiqueta ES el piso de área")
    print(f"     alcanzado {reached} · no alcanzado {missed}   {gap:+g} tableros")
    short = [r["their_eff"] for r in floor if r["ours"] > r["theirs"]]
    if short:
        dubious = sum(1 for e in short if e > corpus_scoring.PLAUSIBLE_EFFICIENCY)
        print(
            f"     las {len(short)} no alcanzadas exigirían {_percentile(short, 50):.1%}"
            f" del tablero útil (p90 {_percentile(short, 90):.1%},"
            f" máx {max(short):.1%})"
        )
        print(
            f"     {dubious} de ellas exigen más que el mejor empaque que este"
            f" motor produjo jamás ({corpus_scoring.PLAUSIBLE_EFFICIENCY:.1%})"
            f" → etiqueta dudosa, no derrota medible"
        )
    print()


def report(
    jobs: List[dict], rows: List[dict], skipped: Counter, notes: List[str]
) -> None:
    ours = sum(j["ours"] for j in jobs)
    theirs = sum(j["theirs"] for j in jobs)
    won = sum(1 for j in jobs if j["ours"] < j["theirs"])
    tied = sum(1 for j in jobs if j["ours"] == j["theirs"])
    lost = sum(1 for j in jobs if j["ours"] > j["theirs"])
    seconds = [j["seconds"] for j in jobs]

    print("\n" + "=" * 78)
    print(f"  {len(jobs)} trabajos puntuados · {sum(skipped.values())} descartados")
    for reason, count in skipped.most_common():
        print(f"      {count:4d}  {reason}")
    if notes:
        print("\n  DERIVA DEL CATÁLOGO (la corrida sigue con el mapa fijado)")
        for note in notes:
            print(f"      ⚠ {note}")
    print()
    delta = ours - theirs
    pct = (delta / theirs * 100) if theirs else 0.0
    print(
        f"  TABLEROS   nuestro {_fmt(ours)}  vs comercial {_fmt(theirs)}   {delta:+g} ({pct:+.1f}%)"
    )
    print(f"             ganamos {won} · empatamos {tied} · perdemos {lost}")
    print()
    _report_populations(rows)
    print(
        f"  TIEMPO     total {sum(seconds):.0f}s · p50 {_percentile(seconds, 50):.2f}s · "
        f"p90 {_percentile(seconds, 90):.2f}s · p99 {_percentile(seconds, 99):.2f}s · "
        f"peor {max(seconds, default=0):.1f}s"
    )
    print()

    # A job whose every losing pool needs a pack beyond anything this engine has
    # ever produced is not a defect we can chase; marking it keeps the eye on
    # the ones that are.
    dubious = {
        r["job"]
        for r in rows
        if r["ours"] > r["theirs"]
        and r["their_eff"] > corpus_scoring.PLAUSIBLE_EFFICIENCY
    }
    worse = sorted(
        (j for j in jobs if j["ours"] > j["theirs"]),
        key=lambda j: (j["theirs"] - j["ours"], -j["pieces"]),
    )[:12]
    slow = sorted(jobs, key=lambda j: -j["seconds"])[:12]
    print(f"  {'PEORES POR TABLEROS  (? = etiqueta dudosa)':<44}{'MÁS LENTOS'}")
    for left, right in zip(worse + [None] * 12, slow + [None] * 12):
        if left is None and right is None:
            break
        cell = ""
        if left:
            mark = "?" if left["job"] in dubious else " "
            cell = f" {mark}{left['ours'] - left['theirs']:+.1f} {_fmt(left['ours']):>5}v{_fmt(left['theirs']):<5}{left['pieces']:>4}pz {left['job'][:20]}"
        right_cell = (
            f"{right['seconds']:>7.1f}s {right['pieces']:>4}pz {right['job'][:26]}"
            if right
            else ""
        )
        print(f"{cell:<44}{right_cell}")
    print("=" * 78)


def diff_baseline(jobs: List[dict], path: str) -> int:
    """Compares against a stored run. Non-zero only when something got worse.

    The gate is deliberately *not* "never worse than the commercial program":
    over 600 real labels that fails on every run and stops carrying information.
    What has to hold is that this build does not regress against the last one.
    """
    with open(path, encoding="utf-8") as handle:
        prior = {j["job"]: j for j in json.load(handle)["jobs"]}
    board_regressions, new_losses, time_regressions = [], [], []
    moved = 0
    for job in jobs:
        before = prior.get(job["job"])
        if before is None:
            continue
        if job["ours"] == before["ours"] and job["digest"] != before["digest"]:
            moved += 1
        if job["ours"] > before["ours"]:
            board_regressions.append((job, before))
        if before["ours"] <= before["theirs"] < job["ours"]:
            new_losses.append((job, before))
        if before["seconds"] > 0.5 and job["seconds"] > before["seconds"] * 1.5:
            time_regressions.append((job, before))

    total_now = sum(j["ours"] for j in jobs if j["job"] in prior)
    total_before = sum(prior[j["job"]]["ours"] for j in jobs if j["job"] in prior)
    print(
        f"\n  vs {os.path.basename(path)}: {_fmt(total_before)} -> {_fmt(total_now)} tableros "
        f"({total_now - total_before:+g})"
    )
    for label_, entries in (
        ("MÁS TABLEROS", board_regressions),
        ("EMPATABA Y AHORA PIERDE", new_losses),
        ("MÁS LENTO (>1.5x)", time_regressions),
    ):
        if not entries:
            continue
        print(f"\n  {label_}:")
        for job, before in entries[:15]:
            print(
                f"    {_fmt(before['ours']):>5} -> {_fmt(job['ours']):<5} "
                f"{before['seconds']:>6.1f}s -> {job['seconds']:<6.1f}s  {job['job'][:48]}"
            )
    if board_regressions or new_losses:
        return 1
    print("\n  ✅ sin regresiones de tableros.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", default=DEFAULT_CORPUS, help="carpeta con los XML del taller"
    )
    parser.add_argument("--only", default=None, help="filtro por substring del nombre")
    parser.add_argument(
        "--limit", type=int, default=None, help="corta tras N pools (humo)"
    )
    parser.add_argument("--workers", type=int, default=os.cpu_count(), help="procesos")
    parser.add_argument("--kerf", type=float, default=None, help="ancho de sierra (mm)")
    parser.add_argument(
        "--trims",
        default=None,
        metavar="T/B/L/R",
        help="desbaste por lado en mm (default: los settings). El export del "
        "comercial no declara ninguno, así que 0/0/0/0 mide cuánto del déficit "
        "es la suposición de desbaste",
    )
    parser.add_argument(
        "--refresh-sheets", action="store_true", help="re-resuelve el mapa y sale"
    )
    parser.add_argument(
        "--save-baseline", default=None, help="escribe el JSON de esta corrida"
    )
    parser.add_argument(
        "--baseline", default=None, help="compara contra un JSON guardado"
    )
    parser.add_argument("--json", default=None, help="vuelca las filas por pool")
    parser.add_argument(
        "--oracle",
        action="store_true",
        help="tras puntuar, busca y VERIFICA un plan más barato para cada pool "
        "que perdemos, y reporta cuántos tableros son deuda real del motor "
        "(caro: ~4 búsquedas extra por pool perdido)",
    )
    parser.add_argument(
        "--oracle-deep",
        action="store_true",
        help="añade al oráculo el brazo de fuerza bruta (presupuesto x6, CP-SAT "
        "sin freno). ~20x más caro y rinde poco: úsalo para responder '¿es solo "
        "presupuesto?' en pools puntuales, no para barrer el corpus",
    )
    parser.add_argument(
        "--oracle-json", default=None, help="vuelca el resultado del oráculo"
    )
    args = parser.parse_args()

    from src.modules.settings.service import SettingsService
    from src.shared.database import SessionLocal

    db = SessionLocal()
    try:
        if args.refresh_sheets:
            materials = set()
            for root, _, names in os.walk(args.corpus):
                for name in names:
                    if name.endswith(".xml"):
                        materials.update(parse_parts(os.path.join(root, name)).keys())
            sheets, unresolved = corpus_sheets.build(db, sorted(materials))
            corpus_sheets.save(sheets, corpus_sheets.DEFAULT_OVERRIDES)
            ambiguous = [k for k, v in sheets.items() if v["candidates"] > 1]
            print(f"{len(sheets)} materiales resueltos -> {corpus_sheets.SHEETS_PATH}")
            print(
                f"  {len(ambiguous)} con empate que el nombre no resolvió (revisar 'candidates')"
            )
            for key in ambiguous[:20]:
                print(
                    f"      {key:<34} -> {sheets[key]['code']:>5}  {sheets[key]['name'][:44]}"
                )
            print(f"  {len(unresolved)} sin lámina (no se puntúan):")
            for key in unresolved[:20]:
                print(f"      {key}")
            return 0

        settings = SettingsService(db).get_or_init()
        trims = (
            [float(v) for v in args.trims.split("/")]
            if args.trims
            else [
                settings.top_trim,
                settings.bottom_trim,
                settings.left_trim,
                settings.right_trim,
            ]
        )
        if len(trims) != 4:
            parser.error("--trims espera cuatro valores: T/B/L/R")
        params_kw = {
            "kerf": settings.kerf if args.kerf is None else args.kerf,
            "top_trim": trims[0],
            "bottom_trim": trims[1],
            "left_trim": trims[2],
            "right_trim": trims[3],
        }
        markup = settings.half_board_markup_pct
        sheets, _ = corpus_sheets.load()
        notes = corpus_sheets.drift(db, sheets)
    finally:
        db.close()

    pools, skipped = collect(
        args.corpus, sheets, args.only, CuttingParameters(**params_kw)
    )
    if args.limit:
        pools = pools[: args.limit]
    if not pools:
        print("nada que puntuar")
        return 1

    print(
        f"motor v{ENGINE_VERSION} · kerf={params_kw['kerf']} "
        f"trims={params_kw['top_trim']}/{params_kw['bottom_trim']}/"
        f"{params_kw['left_trim']}/{params_kw['right_trim']} · "
        f"{len(pools)} pools en {args.workers} procesos"
    )
    started = time.perf_counter()
    tasks = [(pool, params_kw, markup) for pool in pools]
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        rows = list(executor.map(_optimize, tasks, chunksize=1))
    wall = time.perf_counter() - started

    jobs = summarize(rows)
    # Belt and braces over the pre-gate: whatever reason a part went unplaced,
    # the job's board count is not comparable and must not be banked as a win.
    dropped = [j["job"] for j in jobs if j["unplaced"]]
    if dropped:
        skipped[UNPLACED] += len(dropped)
        jobs = [j for j in jobs if not j["unplaced"]]
        rows = [r for r in rows if r["job"] not in set(dropped)]
    report(jobs, rows, skipped, notes)
    print(f"  reloj: {wall:.0f}s con {args.workers} procesos")

    oracle_rows: List[dict] = []
    if args.oracle:
        by_key = {(p.job, p.material): p for p in pools}
        lost = [
            (
                by_key[(r["job"], r["material"])],
                params_kw,
                markup,
                r["ours"],
                args.oracle_deep,
            )
            for r in rows
            if r["ours"] > r["theirs"] and (r["job"], r["material"]) in by_key
        ]
        print(f"\n  oráculo sobre {len(lost)} pools perdidos...")
        started = time.perf_counter()
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            oracle_rows = list(executor.map(_oracle, lost, chunksize=1))
        report_oracle(oracle_rows)
        print(f"  reloj del oráculo: {time.perf_counter() - started:.0f}s")

    payload = {
        "engine": ENGINE_VERSION,
        "params": params_kw,
        "corpus": os.path.abspath(args.corpus),
        "jobs": jobs,
    }
    if args.save_baseline:
        with open(args.save_baseline, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
        print(f"  baseline -> {args.save_baseline}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
    if args.oracle_json and oracle_rows:
        with open(args.oracle_json, "w", encoding="utf-8") as handle:
            json.dump(oracle_rows, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
    if args.baseline:
        return diff_baseline(jobs, args.baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
