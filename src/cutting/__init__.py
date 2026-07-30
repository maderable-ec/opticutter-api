"""Pure domain of the 2D (guillotine) cutting algorithm.

No framework dependencies: just dataclasses and optimization logic.
"""

from src.cutting.enums import (
    PACKING_STRATEGY_SPLIT_RULE,
    PackingStrategy,
    SplitRule,
)
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
    "ENGINE_VERSION",
    "PACKING_STRATEGY_SPLIT_RULE",
    "BinSpec",
    "Cut",
    "CuttingLayout",
    "CuttingParameters",
    "ExactConfig",
    "GuillotineOptimizer",
    "Material",
    "MultiSheetGuillotineOptimizer",
    "PackingStrategy",
    "Piece",
    "PlacedPiece",
    "Rectangle",
    "SearchBudget",
    "SplitRule",
    "exact_available",
    "optimize_bins",
]
