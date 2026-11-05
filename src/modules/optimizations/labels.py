"""Workshop notation for a piece: its edge banding (tapacantos) and its codes."""

from typing import Dict, Iterable, List, Mapping, Optional, Tuple

# Edge-type abbreviation: Soft→CS, Hard→CD (BandType canonical values).
_BAND_TYPE_ABBR = {"Soft": "CS", "Hard": "CD"}

# The two pairs of sides of a piece, in its own (nominal) frame: ``left``/``right``
# run along ``height`` (the first measure) and ``top``/``bottom`` along ``width``.
_ALONG_HEIGHT = ("left", "right")
_ALONG_WIDTH = ("top", "bottom")


def long_sides(height: Optional[float], width: Optional[float]) -> Tuple[str, str]:
    """The sides the notation calls ``L``: the pair along the LONGER measure.

    ``L`` is the long side and ``C`` the short one, the shop's own reading --
    its files carry ``90×1200 1L CS`` for a strip banded along its 1200. Which
    measure the seller typed first does not decide it: that one is the grain
    axis, and a piece with the grain across (300×600) is common. A square piece
    keeps ``left``/``right`` as ``L``, and so does one whose measures are
    unknown, which is the reading from before the rule looked at them. Always
    the FINAL measures (what the seller typed), never the cut ones: a hard tape
    taking 2 mm off must not flip a near-square piece.
    """
    if height is not None and width is not None and height < width:
        return _ALONG_WIDTH
    return _ALONG_HEIGHT


def _short_sides(long: Tuple[str, str]) -> Tuple[str, str]:
    return _ALONG_WIDTH if long == _ALONG_HEIGHT else _ALONG_HEIGHT


def edge_banding_notation(
    sides: Iterable[str],
    band_type: Optional[str] = None,
    alias: Optional[str] = None,
    height: Optional[float] = None,
    width: Optional[float] = None,
) -> str:
    """Workshop notation for the banded sides: ``'2L1C CS CSH'``.

    Three parts, each omitted when unknown: the sides count, the edge **type**
    (``CS``/``CD``) and the banding's short **alias** (``CSH``). The alias is
    what tells two banded designs apart on the thermal label and on the cut
    diagram, neither of which carries a banding summary table.

    ``alias`` is a separate, purely cosmetic field (max 20 chars) — it plays no
    role in board↔tapacanto coordination, which runs on ``products.family_id``.
    It's uppercased for the tag, which is free: nothing downstream depends on
    its case.

    Counts ``L`` (largo/long) on the sides along the piece's LONGER measure and
    ``C`` (corto/short) on the other two (``long_sides``): on 600×300 ``left``
    is a ``1L``, on 300×600 it is a ``1C``. A zero count is omitted (``1L``,
    ``2C``). When all 4 sides are banded it's noted as ``4L``. Returns ``''`` if
    no sides are banded — with no sides there is nothing to qualify, so the
    suffixes are dropped too.

    Important: always use the **nominal** sides (the ones from
    ``EdgeBandingSpec``) and the piece's own measures, not the geometric ones
    remapped by rotation; this keeps the count stable under rotation.
    """
    sides = list(sides or [])
    if not sides:
        return ""
    long = long_sides(height, width)
    short = _short_sides(long)
    long_count = sum(1 for s in sides if s in long)
    short_count = sum(1 for s in sides if s in short)
    if long_count == 2 and short_count == 2:
        parts = "4L"
    else:
        parts = ""
        if long_count:
            parts += f"{long_count}L"
        if short_count:
            parts += f"{short_count}C"
    if not parts:
        return ""
    tag = (alias or "").strip().upper()
    suffixes = [s for s in (_BAND_TYPE_ABBR.get(band_type), tag) if s]
    return " ".join([parts, *suffixes])


# Order the cantos especiales are listed in: the sides along the height first,
# then the ones along the width, each pair in the order the web fills it. The
# notation groups them long sides first instead (``_side_order``), which is
# this same order on every piece whose height is the longer measure.
SPECIAL_SIDE_ORDER = ("left", "right", "top", "bottom")


def _side_order(height: Optional[float], width: Optional[float]) -> Tuple[str, ...]:
    long = long_sides(height, width)
    return long + _short_sides(long)


def edge_notation(
    sides: Iterable[str],
    band_type: Optional[str] = None,
    alias: Optional[str] = None,
    special: Iterable[Mapping[str, Optional[str]]] = (),
    sep: str = " · ",
    height: Optional[float] = None,
    width: Optional[float] = None,
) -> str:
    """The whole banding of a piece: the auto part, then each special tape.

    ``1L1C CS CSH · 1L CD BLN``. The cantos especiales speak the same notation
    as the auto banding, which is also how the seller types them: they are
    grouped by tape (band type + alias), long sides first, and each group is
    written with ``edge_banding_notation`` -- two long sides on one tape read
    ``2L CD BLN``, two different tapes on them read ``1L CD BLN · 1L CS CHM``.
    The two parts never share a side today, but an order frozen when a special
    edge still replaced the auto tape may: the auto part counts only the sides
    no special edge took, so its ``2L1C`` whose long side went special reads
    ``1L1C``. ``special`` holds mappings with ``side`` (nominal), ``band_type``
    and ``alias``. ``height``/``width`` are the piece's own final measures, which
    decide what ``L`` is (``long_sides``). With no special edge this IS
    ``edge_banding_notation``, byte for byte, which is what keeps every existing
    document and label unchanged. ``sep`` joins the groups; the piece export
    passes a space, the shop's own ``1L CD 1C CS``, to stay in ASCII.
    """
    special = list(special or ())
    if not special:
        return edge_banding_notation(sides, band_type, alias, height, width)
    taken = {s["side"] for s in special}
    auto = [s for s in (sides or []) if s not in taken]
    order = _side_order(height, width)
    tapes: Dict[Tuple[Optional[str], Optional[str]], List[str]] = {}
    for edge in sorted(special, key=lambda s: order.index(s["side"])):
        tapes.setdefault((edge.get("band_type"), edge.get("alias")), []).append(
            edge["side"]
        )
    parts = [edge_banding_notation(auto, band_type, alias, height, width)] + [
        edge_banding_notation(group, tape_type, tape_alias, height, width)
        for (tape_type, tape_alias), group in tapes.items()
    ]
    return sep.join(p for p in parts if p)


def length_changes_notation(
    sides: Iterable[str],
    special: Iterable[Mapping[str, Optional[str]]],
    height: Optional[float],
    width: Optional[float],
) -> bool:
    """Whether reading ``L`` by length writes this piece's banding otherwise.

    The notation once called ``L`` the pair along the height whichever measure
    was longer, so the two readings differ only on a piece whose width is the
    longer measure, and only when some tape bands the two pairs unevenly
    (``1L``, ``2C``, a ``1L1C`` split between the Canto and a canto especial).
    Compares the two notations outright rather than counting sides, so a tape
    order that would change is caught too. ``special`` as in ``edge_notation``;
    only its grouping matters here, so a caller may key the tapes by anything
    that tells them apart.
    """
    if long_sides(height, width) == _ALONG_HEIGHT:
        return False
    return edge_notation(sides, special=special) != edge_notation(
        sides, special=special, height=height, width=width
    )


# The payload's mark that its plan reads ``L`` by length on some piece where the
# old reading by height would differ. Absent on every other payload -- where the
# two readings agree -- and on every one computed before the rule, which is
# how an order frozen then keeps printing exactly what the shop already read:
# what rebuilds a notation from a payload's sides reads by length only with it.
EDGE_NOTATION_BY_LENGTH = "by_length"


def payload_reads_by_length(payload: Mapping) -> bool:
    """Whether a payload (or an order's snapshot) reads ``L`` by length."""
    return payload.get("edge_notation") == EDGE_NOTATION_BY_LENGTH


# How each workshop code is named where it is printed, in printing order. Keyed
# by the requirement's own field names (``WORKSHOP_CODE_FIELDS``). The web's
# ``workshopCodesLine`` writes the same words, so the PDF, the order detail and
# the operator's board all read alike.
_WORKSHOP_CODE_ABBR = (
    ("hinging_code", "Abis"),
    ("grooving_code", "Ran"),
    ("assembly_code", "Ens"),
    ("division_code", "Div"),
)


def workshop_codes_line(codes: Mapping[str, Optional[str]], sep: str = " · ") -> str:
    """The workshop codes of a piece as one line.

    ``'Abis X1 · Ran R2 · Ens E3 · Div D1'``: abisagrado, ranurado, ensamble,
    división, in that order. Only the codes present are written, each after the
    service it belongs to -- a code alone would not say whether the piece is
    hinged or grooved. Returns ``''`` for a piece with none. ``sep`` as in
    ``edge_notation``.
    """
    return sep.join(
        f"{abbr} {codes[field]}"
        for field, abbr in _WORKSHOP_CODE_ABBR
        if codes.get(field)
    )
