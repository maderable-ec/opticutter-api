"""Pure domain of the 2D (guillotine) cutting algorithm.

No framework dependencies: just dataclasses and optimization logic.
"""

from src.cutting.consolidate import (
    DEFAULT_MIN_USABLE_OFFCUT,
    consolidate_layout,
    consolidate_layouts,
)
from src.cutting.enums import Selection, SplitRule
from src.cutting.exact import is_available as exact_available
from src.cutting.models import (
    BinSpec,
    Cut,
    CuttingLayout,
    Material,
    Piece,
    PlacedPiece,
    Rectangle,
)
from src.cutting.packer import GuillotineOptimizer
from src.cutting.parameters import CuttingParameters
from src.cutting.search import (
    ENGINE_VERSION,
    ExactConfig,
    MultiSheetGuillotineOptimizer,
    SearchBudget,
    optimize_bins,
)

__all__ = [
    "DEFAULT_MIN_USABLE_OFFCUT",
    "ENGINE_VERSION",
    "BinSpec",
    "Cut",
    "CuttingLayout",
    "CuttingParameters",
    "ExactConfig",
    "GuillotineOptimizer",
    "Material",
    "MultiSheetGuillotineOptimizer",
    "Piece",
    "PlacedPiece",
    "Rectangle",
    "SearchBudget",
    "Selection",
    "SplitRule",
    "consolidate_layout",
    "consolidate_layouts",
    "exact_available",
    "optimize_bins",
]
