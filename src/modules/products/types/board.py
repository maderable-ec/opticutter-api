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
