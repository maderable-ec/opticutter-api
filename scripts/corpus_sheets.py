"""Pins each shop material name to the catalog sheet the benchmark cuts it on.

The exports say ``<material>BLANCO RH</material>`` and nothing else -- no
dimensions, no code. Something has to turn that string into *2070x2440 at
$69.50*, and that something decides the benchmark: **the board count moves ~14%
between two sheets of one decor**, so a comparison that has not named the sheet
is not a comparison.

Why the map is a file and not a query
-------------------------------------
``products`` is synced from the external inventory database, so a sheet's
dimensions or price can change between two runs of the benchmark. Resolving
live -- what ``bench_shopfiles.py`` does -- means a sync silently rewrites the
yardstick:

    28-ago:  BLANCO -> 2070x2440   our total 512 boards
      (catalog sync)
    04-sep:  BLANCO -> 2150x2440   our total 489 boards

Did the engine improve? Unanswerable, which is exactly the question the
benchmark exists to answer. So the map is resolved once, written here, and read
back on every run. ``drift()`` still compares it to the live catalog and reports
what moved, without changing the numbers of the run in progress.

Why there are overrides
-----------------------
Name matching cannot settle every decor, and the ones it cannot settle are the
big ones. ``"BLANCO RH"`` fits nine 15mm catalog boards; four of them are
``MDP RH BLANCO``, ``MDP RH BLANCO NIEVE``, ``MDP RH BLANCO 2C`` and
``PELIKANO RH BLANCO``, all at $69.50, with sheets from 1850x2440 to 2150x2440.
No rule reads the shop's mind there -- somebody who buys the boards has to say
which one. Those answers live in ``overrides`` and survive ``--refresh-sheets``;
everything else is re-derived.

A material that resolves to nothing is **left out of the map and not scored**.
An earlier harness fell back to "some sheet of the same thickness", which kept
the run going and quietly compared jobs against a board the shop never bought.
"""

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from corpus_labels import material_key, overlap, strip_accents

SHEETS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "corpus_sheets.json"
)

# Decided by the shop, not by a rule. Keys are ``material_key()`` output, values
# are catalog codes.
DEFAULT_OVERRIDES = {
    # Denis, 2026-08-28: the shop's "BLANCO" is Blanco Nieve. Both finishes are
    # 2070x2440, which is NOT the sheet name matching would have picked (it
    # scores ``MDP RH BLANCO``, 2150x2440, on the shorter name).
    "BLANCO RH@15": "155",
    "BLANCO@15": "155",
    "BLANCO NORMAL@15": "381",
}

# Words that say the finish, not the decor. Scored separately: they decide
# *which* BLANCO, never *whether* a board is a BLANCO at all.
_FINISH = frozenset({"RH", "RANURADO", "NORMAL", "MATE", "MT"})
# ``1C``/``2C`` is how many faces are finished -- vendor boilerplate that
# appears on unrelated decors, so scoring it matches ``FOLIO 1C BL`` to
# ``HDF CEDRO 1C`` on the one word they share.
_NOISE = frozenset(
    {
        "MDP",
        "MDF",
        "MEL",
        "M",
        "MM",
        "DE",
        "LA",
        "EL",
        "Y",
        "CON",
        "HR",
        "FANT",
        "1C",
        "2C",
        "3C",
    }
)


def _words(text: str) -> frozenset:
    """Decor words of a name, with the vendor's boilerplate removed."""
    cleaned = re.sub(r"\(.*?\)", " ", strip_accents(text).upper())
    out = set()
    for word in re.split(r"[^A-Z0-9]+", cleaned):
        if not word or word in _NOISE or word in _FINISH:
            continue
        if word.replace(".", "").isdigit():
            continue
        if len(word) > 4 and word.endswith("S"):
            word = word[:-1]
        out.add(word)
    return frozenset(out)


def _has_grain(text: str) -> bool:
    """Woodgrain (``RH``) -- the axis that moves the price 15-20%."""
    return bool(re.search(r"\bR(?:H|ANURADO)\b", strip_accents(text).upper()))


def _entry(board, candidates: int, source: str) -> dict:
    attributes = board.attributes or {}
    return {
        "code": board.code,
        "name": board.name,
        "width": float(attributes["width"]),
        "height": float(attributes["height"]),
        "thickness": float(attributes["thickness"]),
        "price": float(board.price),
        "candidates": candidates,
        "source": source,
    }


def resolve(material: str, boards: List) -> Tuple[Optional[dict], int]:
    """Best catalog sheet for one shop material string, and how close the race was.

    Matching is on the product **name**, not on ``family``: family is the
    board<->tapacanto coordination key and is edited for that purpose (the 15mm
    Blanco Nieve boards carry the family ``"Blanco"``), while the name is what
    the vendor ships and what a human recognises.
    """
    key = material_key(material)
    decor, thickness = key.rsplit("@", 1)
    wanted = _words(decor)
    if not wanted:
        return None, 0

    same_thickness = [
        b
        for b in boards
        if float((b.attributes or {}).get("thickness", 0) or 0) == float(thickness)
    ]
    scored = []
    for board in same_thickness:
        # Fuzzy, like the labeler: the shop writes RISTRETO for RISTRETTO and
        # CASHEMERE for CASHMERE. On an exact score those tie with every other
        # board of the family at zero and the shortest name wins the coin toss
        # -- which is how ``BARROCO RISTRETO`` resolved to BARROCO *DORADO*.
        score = overlap(wanted, _words(board.name))
        if score:
            # The finish is a tie-breaker, not a filter: a decor stocked in one
            # finish only should still match rather than fall out of the map.
            finish = 1 if _has_grain(board.name) == _has_grain(material) else 0
            scored.append((score, finish, -len(board.name), board))
    if not scored:
        return None, 0
    scored.sort(key=lambda row: row[:3], reverse=True)
    best = scored[0]
    tied = sum(1 for row in scored if row[:2] == best[:2])
    # A tie on a *partial* decor is not a near-miss, it is a different product.
    # ``"BARROCO ARTESANAL"`` shares exactly one word with BARROCO DORADO,
    # BARROCO RISTRETTO and BARROCO AMBAR alike, and the shortest name wins a
    # coin toss between three wrong answers; ``"HIGH GLOSS BLANCO"`` matches
    # plain BLANCO NIEVE on ``BLANCO`` alone. Both belong out of the map.
    if tied > 1 and best[0] < len(wanted):
        return None, tied
    return _entry(best[3], tied, "name"), tied


def build(
    db, materials, overrides: Optional[Dict[str, str]] = None
) -> Tuple[Dict[str, dict], List[str]]:
    """Resolve every material key once. Returns ``(sheets, unresolved)``."""
    from src.modules.products.model import ProductModel

    overrides = DEFAULT_OVERRIDES if overrides is None else overrides
    boards = db.query(ProductModel).filter(ProductModel.type == "board").all()
    by_code = {b.code: b for b in boards}

    sheets: Dict[str, dict] = {}
    unresolved: List[str] = []
    for material in materials:
        key = material_key(material)
        if key in sheets:
            continue
        if key in overrides:
            board = by_code.get(overrides[key])
            if board is None:
                raise SystemExit(
                    f"override {key} -> code {overrides[key]} no está en el catálogo"
                )
            sheets[key] = _entry(board, 1, "override")
            continue
        entry, _ = resolve(material, boards)
        if entry is None:
            unresolved.append(key)
        else:
            sheets[key] = entry
    return sheets, sorted(set(unresolved))


def save(
    sheets: Dict[str, dict], overrides: Dict[str, str], path: str = SHEETS_PATH
) -> None:
    payload = {
        "_comment": (
            "Generado por bench_corpus.py --refresh-sheets. 'overrides' es la parte "
            "escrita a mano y sobrevive al refresh; 'sheets' se re-deriva. "
            "'candidates' > 1 marca un empate que el nombre no resolvió."
        ),
        "overrides": overrides,
        "sheets": dict(sorted(sheets.items())),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load(path: str = SHEETS_PATH) -> Tuple[Dict[str, dict], Dict[str, str]]:
    if not os.path.exists(path):
        raise SystemExit(
            f"falta {path}. Generalo con: python scripts/bench_corpus.py --refresh-sheets"
        )
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["sheets"], payload.get("overrides", {})


def drift(db, sheets: Dict[str, dict]) -> List[str]:
    """What the live catalog now says that the pinned map does not.

    Reported, never applied: the run has to stay comparable to the last one.
    """
    from src.modules.products.model import ProductModel

    live = {
        b.code: b
        for b in db.query(ProductModel).filter(ProductModel.type == "board").all()
    }
    notes = []
    for key, entry in sorted(sheets.items()):
        board = live.get(entry["code"])
        if board is None:
            notes.append(f"{key}: el código {entry['code']} ya no está en el catálogo")
            continue
        attributes = board.attributes or {}
        width, height = float(attributes["width"]), float(attributes["height"])
        if (width, height) != (entry["width"], entry["height"]):
            notes.append(
                f"{key}: catálogo {width:.0f}x{height:.0f}, "
                f"el mapa fija {entry['width']:.0f}x{entry['height']:.0f}"
            )
        if float(board.price) != entry["price"]:
            notes.append(f"{key}: precio {entry['price']:g} -> {float(board.price):g}")
    return notes
