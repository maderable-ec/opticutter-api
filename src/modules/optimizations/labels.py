"""Workshop notation for a piece's edge banding (tapacantos)."""

from typing import Iterable, Optional

# Edge-type abbreviation: Soft→CS, Hard→CD (BandType canonical values).
_BAND_TYPE_ABBR = {"Soft": "CS", "Hard": "CD"}

# Readable edge-type label for tables/legends (BandType canonical values).
BAND_TYPE_LABEL = {"Soft": "Suave", "Hard": "Duro"}


def edge_banding_notation(
    sides: Iterable[str],
    band_type: Optional[str] = None,
    family: Optional[str] = None,
) -> str:
    """Workshop notation for the banded sides: ``'2L1C CS CSH'``.

    Three parts, each omitted when unknown: the sides count, the edge **type**
    (``CS``/``CD``) and the banding's design **family** (``CSH``). The family is
    what tells two banded designs apart on the dispatch sheet, the only document
    without a banding summary table.

    ``family`` doubles as the board↔tapacanto coordination key, so it's already
    on the product and already user-editable — keep it short (a 3-letter design
    code) precisely because it gets printed here. It's uppercased for the tag,
    which is free: the coordination match is case-insensitive anyway.

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
    tag = (family or "").strip().upper()
    suffixes = [s for s in (_BAND_TYPE_ABBR.get(band_type), tag) if s]
    return " ".join([parts, *suffixes])
