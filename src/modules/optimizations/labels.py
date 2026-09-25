"""Workshop notation for a piece: its edge banding (tapacantos) and its codes."""

from typing import Dict, Iterable, List, Mapping, Optional, Tuple

# Edge-type abbreviation: Soft→CS, Hard→CD (BandType canonical values).
_BAND_TYPE_ABBR = {"Soft": "CS", "Hard": "CD"}


def edge_banding_notation(
    sides: Iterable[str],
    band_type: Optional[str] = None,
    alias: Optional[str] = None,
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

    Classifies by the **nominal** side measurement: ``left``/``right`` are the
    height sides (first dimension) → ``L`` (largo/long); ``top``/``bottom`` are
    the width sides → ``C`` (corto/short). A zero count is omitted (``1L``,
    ``2C``). When all 4 sides are banded it's noted as ``4L``. Returns ``''`` if
    no sides are banded — with no sides there is nothing to qualify, so the
    suffixes are dropped too.

    Important: always use the **nominal** sides (the ones from
    ``EdgeBandingSpec``), not the geometric ones remapped by rotation; this keeps
    the count stable under rotation.
    """
    sides = list(sides or [])
    if not sides:
        return ""
    long_count = sum(1 for s in sides if s in ("left", "right"))  # height = Long
    short_count = sum(1 for s in sides if s in ("top", "bottom"))  # width = Short
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


# Order the cantos especiales are grouped and written in: the long sides first,
# then the short ones, each pair in the order the web fills it (``1L`` is
# ``left``, ``1C`` is ``top``, as in its ``NOTATION_TO_SIDES``).
SPECIAL_SIDE_ORDER = ("left", "right", "top", "bottom")


def edge_notation(
    sides: Iterable[str],
    band_type: Optional[str] = None,
    alias: Optional[str] = None,
    special: Iterable[Mapping[str, Optional[str]]] = (),
    sep: str = " · ",
) -> str:
    """The whole banding of a piece: the auto part, then each special tape.

    ``1L1C CS CSH · 1L CD BLN``. The cantos especiales speak the same notation
    as the auto banding, which is also how the seller types them: they are
    grouped by tape (band type + alias) and each group is written with
    ``edge_banding_notation`` -- two long sides on one tape read ``2L CD BLN``,
    two different tapes on them read ``1L CD BLN · 1L CS CHM``. The auto part
    counts only the sides no special edge took, so a ``2L1C`` whose long side
    went special reads ``1L1C``. ``special`` holds mappings with ``side``
    (nominal), ``band_type`` and ``alias``. With no special edge this IS
    ``edge_banding_notation``, byte for byte, which is what keeps every existing
    document and label unchanged. ``sep`` joins the groups; the piece export
    passes a space, the shop's own ``1L CD 1C CS``, to stay in ASCII.
    """
    special = list(special or ())
    if not special:
        return edge_banding_notation(sides, band_type, alias)
    taken = {s["side"] for s in special}
    auto = [s for s in (sides or []) if s not in taken]
    tapes: Dict[Tuple[Optional[str], Optional[str]], List[str]] = {}
    for edge in sorted(special, key=lambda s: SPECIAL_SIDE_ORDER.index(s["side"])):
        tapes.setdefault((edge.get("band_type"), edge.get("alias")), []).append(
            edge["side"]
        )
    parts = [edge_banding_notation(auto, band_type, alias)] + [
        edge_banding_notation(group, tape_type, tape_alias)
        for (tape_type, tape_alias), group in tapes.items()
    ]
    return sep.join(p for p in parts if p)


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
