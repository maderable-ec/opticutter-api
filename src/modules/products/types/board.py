from enum import Enum
from typing import Optional

from pydantic import Field, PositiveFloat, PositiveInt

from src.shared.schemas import CamelModel

# The external inventory system's own ``TIPO`` column is in Spanish for some
# values; these normalize its text to the enum's canonical English value (see
# ``BoardSubtype._missing_``), same pattern as ``BandType``'s Spanish aliases.
_BOARD_SUBTYPE_SPANISH_ALIASES = {
    "pino": "Pine",
    "madera natural": "Natural Wood",
    "enchapado": "Veneer",
    "ranurado": "Grooved",
}


class BoardSubtype(str, Enum):
    """Board material subtype (closed set, case-insensitive input).

    Canonical values are English. The catalog sync (``products/catalog_sync.py``)
    feeds this enum the external inventory system's raw ``TIPO`` text, which is
    partly in Spanish (e.g. ``"Pino"``, ``"Enchapado"``); ``_missing_`` accepts
    that text case-insensitively via ``_BOARD_SUBTYPE_SPANISH_ALIASES`` and
    normalizes it to the canonical value, so the sync needs no translation
    table of its own.
    """

    MDP = "MDP"
    MDF = "MDF"
    HDF = "HDF"
    PLYWOOD = "Plywood"
    PINE = "Pine"
    NATURAL_WOOD = "Natural Wood"
    HIGH_GLOSS = "High Gloss"
    MATH_SOFT = "Math Soft"
    OSB = "OSB"
    VENEER = "Veneer"
    GROOVED = "Grooved"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            norm = value.strip().lower()
            for member in cls:
                if member.value.lower() == norm:
                    return member
            if norm in _BOARD_SUBTYPE_SPANISH_ALIASES:
                return cls(_BOARD_SUBTYPE_SPANISH_ALIASES[norm])
        return None


class BoardAttributes(CamelModel):
    """Board-specific attributes (input to the cutting optimizer)."""

    height: PositiveInt = Field(
        ..., description="Height (length, first dimension) in mm"
    )
    width: PositiveInt = Field(..., description="Width (second dimension) in mm")
    # Fractional, like the edge banding's: OSB, MDF fondo and thin plywood ship
    # in 5.5, 9.5, 11.1, 18.3mm and the vendor's catalog carries them. Only the
    # two dimensions the optimizer cuts along stay integral.
    thickness: PositiveFloat = Field(..., description="Thickness in mm")
    grain_direction: Optional[str] = Field(
        None, max_length=4, description="Grain direction"
    )
    subtype: Optional[BoardSubtype] = Field(
        None, description="Material subtype (MDP/MDF/Plywood/...)"
    )
    family: Optional[str] = Field(
        None,
        max_length=64,
        description="Familia/diseño para coordinar tapacantos (debe coincidir con el tapacanto)",
    )


class HalfBoardSplit(str, Enum):
    """How (and whether) a board is sold as a half board.

    The shop does not cut every material in two, and the ones it does cut are
    not all cut the same way: the split axis is a property of the MATERIAL, so
    it is derived from ``BoardAttributes.subtype`` (which the catalog sync fills
    from the vendor's own ``TIPO``) rather than stored per product.

    The names are the shop's: a cut *parallel to the long side* runs along the
    largo and therefore halves the ``width``; one *parallel to the short side*
    halves the ``height``. ``height`` is the largo by convention (the first
    dimension the vendor writes and the one ``BoardAttributes`` documents), and
    the sync warns when a row comes in the other way round.
    """

    NONE = "none"
    LONG_SIDE = "long_side"
    SHORT_SIDE = "short_side"


# Subtype -> how its boards are halved. Confirmed twice: with the shop, and
# against the vendor's own "(MEDIO)" SKUs, which exist for MDP, PLYWOOD, MDF,
# RANURADO and HDF only — never for OSB, pino, madera natural or enchapado.
_HALF_BOARD_SPLIT = {
    BoardSubtype.MDP: HalfBoardSplit.LONG_SIDE,
    BoardSubtype.MDF: HalfBoardSplit.LONG_SIDE,
    BoardSubtype.HDF: HalfBoardSplit.LONG_SIDE,
    BoardSubtype.PLYWOOD: HalfBoardSplit.SHORT_SIDE,
    BoardSubtype.GROOVED: HalfBoardSplit.SHORT_SIDE,
    BoardSubtype.OSB: HalfBoardSplit.NONE,
    BoardSubtype.PINE: HalfBoardSplit.NONE,
    BoardSubtype.NATURAL_WOOD: HalfBoardSplit.NONE,
    BoardSubtype.HIGH_GLOSS: HalfBoardSplit.NONE,
    BoardSubtype.MATH_SOFT: HalfBoardSplit.NONE,
    BoardSubtype.VENEER: HalfBoardSplit.NONE,
}

# The thick MDP the shop never halves. Written as a floor rather than ``== 36``
# so it can't be defeated by a float comparison; nothing above 36 exists in the
# catalog today, so the two readings select the same boards.
_MDP_UNSPLITTABLE_THICKNESS_MM = 36.0


def half_board_split(subtype, thickness: float) -> HalfBoardSplit:
    """The half-board policy for a board, from its subtype and thickness.

    ``subtype`` accepts the enum, its raw string or ``None`` (a board registered
    by hand, or a vendor ``TIPO`` this build does not know yet). An unknown
    subtype falls back to ``LONG_SIDE`` — the behavior every board had before
    this rule existed, so nothing a seller already quotes stops being offered
    because the catalog grew a value.
    """
    if not isinstance(subtype, BoardSubtype):
        if subtype is None:
            return HalfBoardSplit.LONG_SIDE
        try:
            subtype = BoardSubtype(subtype)
        except ValueError:
            return HalfBoardSplit.LONG_SIDE
    if (
        subtype is BoardSubtype.MDP
        and float(thickness or 0) >= _MDP_UNSPLITTABLE_THICKNESS_MM
    ):
        return HalfBoardSplit.NONE
    return _HALF_BOARD_SPLIT.get(subtype, HalfBoardSplit.LONG_SIDE)
