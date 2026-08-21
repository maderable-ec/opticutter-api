from enum import Enum
from typing import Optional

from pydantic import Field, PositiveInt

from src.shared.schemas import CamelModel


class BoardSubtype(str, Enum):
    """Board material subtype (closed set, case-insensitive input).

    Mirrors the values the external inventory system uses in its own ``TIPO``
    column, so the catalog sync (``products/catalog_sync.py``) can map straight
    into this enum without a translation table.
    """

    MDP = "MDP"
    MDF = "MDF"
    HDF = "HDF"
    PLYWOOD = "Plywood"
    PINO = "Pino"
    MADERA_NATURAL = "Madera Natural"
    HIGH_GLOSS = "High Gloss"
    MATH_SOFT = "Math Soft"
    OSB = "OSB"
    ENCHAPADO = "Enchapado"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            norm = value.strip().lower()
            for member in cls:
                if member.value.lower() == norm:
                    return member
        return None


class BoardAttributes(CamelModel):
    """Board-specific attributes (input to the cutting optimizer)."""

    height: PositiveInt = Field(
        ..., description="Height (length, first dimension) in mm"
    )
    width: PositiveInt = Field(..., description="Width (second dimension) in mm")
    thickness: PositiveInt = Field(..., description="Thickness in mm")
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
