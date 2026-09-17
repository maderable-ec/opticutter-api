"""Workshop notation for a piece: its edge banding (tapacantos) and its codes."""

from typing import Iterable, Mapping, Optional

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
    role in board↔tapacanto coordination, which still uses the product's
    ``family`` attribute. It's uppercased for the tag, which is free: nothing
    downstream depends on its case.

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


# How each workshop code is named where it is printed, in printing order. Keyed
# by the requirement's own field names (``WORKSHOP_CODE_FIELDS``). The web's
# ``workshopCodesLine`` writes the same words, so the PDF, the order detail and
# the operator's board all read alike.
_WORKSHOP_CODE_ABBR = (
    ("hinging_code", "Abis"),
    ("assembly_code", "Ens"),
    ("grooving_code", "Ran"),
)


def workshop_codes_line(codes: Mapping[str, Optional[str]]) -> str:
    """The workshop codes of a piece as one line: ``'Abis X1 · Ens E3 · Ran R2'``.

    Only the codes present are written, each after the service it belongs to --
    a code alone would not say whether the piece is hinged or grooved. Returns
    ``''`` for a piece with none.
    """
    return " · ".join(
        f"{abbr} {codes[field]}"
        for field, abbr in _WORKSHOP_CODE_ABBR
        if codes.get(field)
    )
