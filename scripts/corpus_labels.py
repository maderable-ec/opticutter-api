"""Reads the commercial program's board count out of an export's filename.

The shop's exports (``*.xml``) carry only ``<parts>``: no result, no board
count. What records the commercial optimizer's answer is the **filename** --
``"3 JAPANDI Y 6 BLANCO RH15MM_3JAPANDI 36MM_1 BLNORMAL"`` means it billed 3 +
6 + 3 + 1 boards. That makes the name the label of a benchmark, and this module
the thing that reads it.

It is deliberately pure text: no database, no catalog, no engine. Whether a
label is *physically possible* (do the parts even fit in that many boards?) is
geometry and belongs to the runner; all this module decides is what the name
says and whether it says it unambiguously.

Two traps, both paid for in lost labels:

**Thickness cannot be stripped with a trailing ``\\b``.** In ``"15MM_03-06"``
the underscore is a word character, so the boundary after ``MM`` never closes,
``15MM`` survives the strip, and ``15`` is read as a quantity. That silently
cost 405 of 485 labels when it was written the obvious way -- and it fails
*open*, producing a plausible wrong number rather than an error.

**Quantities cannot capture their own trailing text.** ``"1 BLANCO RH Y MEDIO
BLANCO NORMAL"`` has two quantities, and a regex that grabs "everything after
the digits that is not a digit" swallows the ``MEDIO`` into the first segment's
text. Quantities are therefore matched first, and each segment's text is the
gap between one match and the next.

Matching a quantity to a pool is by **token overlap, never by position**. The
XML lists materials in the order the parts happen to appear, which is not the
order the name lists them:

    4 BLANCOS RH 15MM_1 CATANIA RH 15MM_COCINA CAGUANA.xml
      XML:    ['CATANIA', 'BLANCO RH']     <- CATANIA first
      name:   [4, 1]                       <- BLANCO first

Positional assignment gets that backwards, and did on 6 of 48 files. Token
overlap also beats a hard-coded decor list, which cannot cover the 140 distinct
material strings the corpus actually contains.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence

# Rejection reasons. Constants because the runner groups the corpus by them and
# prints the tally: how many files we cannot label is a quality measure of the
# benchmark, not a footnote.
LEFTOVER = "corte de sobras"
NO_QUANTITY = "el nombre no dice cantidades"
COUNT_MISMATCH = "cantidades != materiales"
NO_UNIQUE_MATCH = "una cantidad no empareja con un solo material"

# A job cut from offcuts has no whole sheet to count, so "how many boards did
# the commercial program bill" has no answer for it.
#
# Spelled with an explicit separator class instead of ``\b``: filenames join
# words with underscores, which are word characters, so ``\bSOBRA\b`` never
# opens inside ``"MARCIA JARA_SOBRA DEL TALLER"``. That one file then read its
# ``237X91CM`` as 237 boards and by itself accounted for -236 of a -250 result.
_LEFTOVER_MARKER = re.compile(r"(?:^|[^A-Z])SOBRAS?(?:[^A-Z]|$)")

# ``237X91CM``, ``2.44X2.15`` -- a measurement, not two quantities.
_DIMENSIONS = re.compile(r"\d+(?:[.,]\d+)?\s*X\s*\d+(?:[.,]\d+)?\s*(?:CM|MM|M)?")

# Dates written out: "14 MAYO 2026", "23 JULIO26", "5 DE ABRIL".
_MONTHS = (
    "ENERO|FEBRERO|MARZO|ABRIL|MAYO|JUNIO|JULIO|AGOSTO|SEPTIEMBRE|SETIEMBRE"
    "|OCTUBRE|NOVIEMBRE|DICIEMBRE"
)
_WORD_DATE = re.compile(
    rf"(?:\d{{1,2}}\s*)?(?:DE\s+)?(?:{_MONTHS})\w*\s*(?:DE\s+)?\d{{0,4}}"
)

_DATE = re.compile(r"\d{1,2}\s*[-_/]\s*\d{1,2}\s*[-_/]\s*\d{2,4}")
# NOT anchored with \b -- see the module docstring. ``(?![A-Z])`` is the
# boundary that actually holds, because the character after a thickness is
# usually ``_`` or end-of-string.
_THICKNESS = re.compile(r"(\d+(?:[.,]\d+)?)\s*M{1,2}(?![A-Z])")

# The house thickness, assumed wherever nobody wrote one. Kept as tenths of a
# millimetre so 5.5mm is one alphanumeric token and not two.
DEFAULT_THICKNESS = 15.0

# Thicknesses the vendor actually sells, from the board catalog. This is what
# tells a thickness typed without its unit from a pool counter: the shop writes
# both, as ``"VISÓN RH 15"`` (a 15mm sheet) and ``"AMARETTO RH 1"`` (its first
# AMARETTO pool). Reading the second as a 1mm board splits one product into
# several catalog keys and loses every job that used it.
KNOWN_THICKNESSES = frozenset(
    {
        3,
        3.6,
        4,
        5,
        5.2,
        5.5,
        6,
        6.5,
        8,
        9,
        9.5,
        10,
        11.1,
        12,
        13,
        15,
        15.1,
        16,
        18,
        18.3,
        19,
        20,
        25,
        36,
        40,
        45,
    }
)

# A number standing on its own inside a material string, unit omitted.
_BARE_NUMBER = re.compile(r"(?:^|\s)(\d+(?:[.,]\d+)?)(?=\s|$)")
_STRAY_YEAR = re.compile(r"\b20\d\d\b")

# ``MEDI[OA]?`` rather than ``MEDI[OA]``: one export writes "2 Y MEDI BLANCO".
# ``(?<![A-Z0-9])`` keeps the digits of a thickness token (``T150``) from being
# read as a quantity now that thicknesses survive the clean as tokens.
_QUANTITY = re.compile(
    r"(?<![A-Z0-9])(\d+)\s*Y?\s*MEDI[OA]?\b"
    r"|\bMEDI[OA]?\b(?=\s|$)"
    r"|(?<![A-Z0-9])(\d+)"
)

# Dropped before comparing a segment's words to a material's. "RH" and "NORMAL"
# are NOT here: they are the finish, and telling "BLANCO RH" from "BLANCO
# NORMAL" is the whole job on 166 of the corpus's files.
_STOPWORDS = frozenset(
    {"Y", "E", "DE", "DEL", "LA", "EL", "LOS", "LAS", "CON", "M", "MM", "CM"}
)


def strip_accents(text: str) -> str:
    """``VISÓN`` and ``VISON`` are the same decor typed by two people."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _thickness_token(match: "re.Match") -> str:
    """``15MM`` -> `` T150 ``: no longer a number, still the same fact.

    Erasing thicknesses outright loses the only thing that separates two pools
    of one decor. ``"3 JAPANDI Y 6 BLANCO RH15MM_3JAPANDI 36MM"`` has a
    ``JAPANDI RH 15MM`` pool and a ``JAPANDI RH 36MM`` pool, and with the
    millimetres gone both claims match both pools equally.
    """
    millimetres = float(match.group(1).replace(",", "."))
    return f" T{round(millimetres * 10)} "


def clean_name(filename: str) -> str:
    """Filename minus everything that is not a quantity or a decor.

    Order matters: dates go before thicknesses, or ``15-04-26`` loses its
    ``15`` to the thickness pattern and the rest of the date turns into two
    bare quantities.
    """
    text = strip_accents(filename).upper()
    text = re.sub(r"\.XML$", " ", text)
    text = _DIMENSIONS.sub(" ", text)
    text = _WORD_DATE.sub(" ", text)
    text = _DATE.sub(" ", text)
    text = _THICKNESS.sub(_thickness_token, text)
    text = _STRAY_YEAR.sub(" ", text)
    return text


def thickness_of(material: str, default: float = DEFAULT_THICKNESS) -> float:
    """Millimetres named inside a material string, else ``default``.

    The shop writes the thickness on the material only when it is not the
    house default: ``"BARROCO DORADO 36MM"`` but plain ``"BLANCO RH"`` -- and
    often drops the unit, as ``"CASHMERE 36"``. A bare number only counts when
    the vendor sells a sheet that thick; otherwise it is a pool counter.
    """
    text = strip_accents(material).upper()
    found = _THICKNESS.search(text)
    if found:
        return float(found.group(1).replace(",", "."))
    for bare in _BARE_NUMBER.finditer(text):
        value = float(bare.group(1).replace(",", "."))
        if value in KNOWN_THICKNESSES:
            return value
    return default


def material_key(material: str) -> str:
    """Catalog-map key for a shop material string: ``"BLANCO RH@15"``.

    The corpus spells one decor several ways -- ``BLANCO RH``, ``BLANCO RH
    15MM``, ``BLANCO  RH`` -- and they are one product. Thickness stays in the
    key because it is *not* cosmetic: ``RISTRETTO 15MM`` and ``RISTRETTO 36MM``
    are different sheets at different prices.
    """
    text = _THICKNESS.sub(" ", strip_accents(material).upper())
    words = [
        w
        for w in re.split(r"[^A-Z0-9]+", text)
        if w and w not in _STOPWORDS and not w.replace(".", "").isdigit()
    ]
    return f"{' '.join(words)}@{thickness_of(material):g}"


def _tokens(text: str) -> frozenset:
    """Comparable words of a decor phrase, singularised and thickness-tagged.

    ``BLANCOS`` in the name has to match ``BLANCO`` in the XML, so a trailing
    ``S`` is dropped -- but only on words long enough that it is a plural and
    not the word itself.

    A phrase that names no thickness gets the house default, on both sides of
    the comparison. Without it ``"3 JAPANDI Y 6 BLANCO RH 15MM"`` -- where the
    millimetres are written once and meant for both -- leaves its first claim
    tied between the 15mm and the 36mm JAPANDI pools.
    """
    words = set()
    for word in re.split(
        r"[^A-Z0-9]+", _THICKNESS.sub(_thickness_token, strip_accents(text).upper())
    ):
        if not word or word in _STOPWORDS or word.isdigit():
            continue
        if len(word) > 4 and word.endswith("S"):
            word = word[:-1]
        words.add(word)
    if not any(w.startswith("T") and w[1:].isdigit() for w in words):
        words.add(f"T{round(thickness_of(text) * 10)}")
    return frozenset(words)


def overlap(claim: frozenset, material: frozenset) -> int:
    """Words two phrases share, counting near-misses as shared.

    The corpus is typed by hand at a saw: ``CASHEMERE`` for CASHMERE,
    ``RISTRETO`` for RISTRETTO, ``BARRROCO``, ``BALNCORH``. An exact-match score
    reads those as different decors and throws the file away. The floor is high
    (0.85) and only applies to words long enough for a ratio to mean something,
    so ``BLANCO``/``BLANCA`` -- 0.83 -- stays a miss.
    """
    score = 0
    for word in claim:
        if word in material:
            score += 1
        elif len(word) >= 5 and any(
            len(other) >= 5 and SequenceMatcher(None, word, other).ratio() >= 0.85
            for other in material
        ):
            score += 1
    return score


@dataclass(frozen=True)
class Segment:
    """One "N <decor>" claim read off a filename."""

    quantity: float
    text: str


def segments(filename: str) -> List[Segment]:
    """Quantity claims in a filename, in the order the name states them.

    Each segment's text is the gap between its quantity and the next one, so a
    standalone ``MEDIO`` later in the name cannot be swallowed by an earlier
    segment (see the module docstring).
    """
    cleaned = clean_name(filename)
    matches = list(_QUANTITY.finditer(cleaned))
    found = []
    for index, match in enumerate(matches):
        whole, bare = match.group(1), match.group(2)
        if whole is not None:
            quantity = float(whole) + 0.5
        elif bare is not None:
            quantity = float(bare)
        else:
            quantity = 0.5
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        found.append(Segment(quantity, cleaned[match.end() : end]))
    return found


@dataclass(frozen=True)
class Labeling:
    """What the filename claims, or why we refuse to guess."""

    boards: Dict[str, float] = field(default_factory=dict)
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def label(filename: str, materials: Sequence[str]) -> Labeling:
    """Boards the commercial program billed, per material of this export.

    ``materials`` are the pool names exactly as the XML spells them, so the
    result can be looked up against the parsed pools without further mapping.
    Anything short of a one-to-one reading is refused: a benchmark that guesses
    its own labels measures the guess.
    """
    if _LEFTOVER_MARKER.search(strip_accents(filename).upper()):
        return Labeling(reason=LEFTOVER)

    claims = segments(filename)
    if not claims:
        return Labeling(reason=NO_QUANTITY)
    if len(claims) != len(materials):
        return Labeling(reason=COUNT_MISMATCH)

    # One material and one claim is forced: there is nothing to disambiguate,
    # and requiring the decors to match would drop every pool the shop names
    # with a code rather than a decor.
    if len(materials) == 1:
        return Labeling(boards={materials[0]: claims[0].quantity})

    boards: Dict[str, float] = {}
    remaining = list(materials)
    for claim in claims:
        wanted = _tokens(claim.text)
        scored = [(overlap(wanted, _tokens(m)), m) for m in remaining]
        best = max(score for score, _ in scored)
        winners = [m for score, m in scored if score == best]
        if best == 0 or len(winners) != 1:
            return Labeling(reason=NO_UNIQUE_MATCH)
        boards[winners[0]] = claim.quantity
        remaining.remove(winners[0])
    return Labeling(boards=boards)
