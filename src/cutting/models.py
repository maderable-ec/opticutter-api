from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Rectangle:
    """Represents a rectangle with position and dimensions"""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        """Validates the rectangle's dimensions and caches its area.

        ``area`` is a plain attribute rather than a ``@property`` because the
        packer's inner loop reads it tens of millions of times per request (gap
        ranking and the remainder sort); dimensions never change after
        construction, so there is nothing to invalidate.
        """
        if self.width < 0 or self.height < 0:
            raise ValueError(
                f"Dimensions cannot be negative: width={self.width}, height={self.height}"
            )
        self.area = self.width * self.height

    def contains(self, width: float, height: float) -> bool:
        """Checks whether this rectangle can contain the given dimensions"""
        return self.width >= width and self.height >= height

    def __repr__(self) -> str:
        return f"Rect(x={self.x}, y={self.y}, w={self.width}, h={self.height})"


@dataclass
class Cut:
    """Represents a guillotine cut (a saw travel segment).

    ``length`` is the travel length (cut axis); ``kerf`` is the blade width
    (perpendicular) and does not affect this length. Start coordinates are kept
    for eventual drawing, though the immediate use is summing ``length``.
    """

    x: float
    y: float
    length: float
    is_horizontal: bool


@dataclass
class Piece:
    """Represents a piece to be cut"""

    id: str
    width: float
    height: float
    quantity: int = 1
    can_rotate: bool = True
    priority: int = 0

    def __post_init__(self):
        """Validates the piece's dimensions and caches its area (see ``Rectangle``)."""
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"Dimensions must be positive: width={self.width}, height={self.height}"
            )
        if self.quantity < 1:
            raise ValueError(f"Quantity must be at least 1: quantity={self.quantity}")
        self.area = self.width * self.height

    def __repr__(self) -> str:
        return f"Piece(id={self.id}, w={self.width}, h={self.height})"


@dataclass
class PlacedPiece:
    """Represents a piece already placed on the material"""

    piece: Piece
    x: float
    y: float
    width: float
    height: float
    rotated: bool = False

    def to_dict(self) -> Dict:
        """Converts to a dictionary for serialization"""
        return {
            "piece_id": self.piece.id,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "rotated": self.rotated,
            "original_width": self.piece.width,
            "original_height": self.piece.height,
        }


@dataclass
class Material:
    """Represents a material/board on which pieces will be cut"""

    id: str
    width: float
    height: float
    thickness: float
    cost_per_unit: float = 0.0
    # Descriptive metadata: ``True`` if this sheet is a half board (width/2,
    # cost = price/2 plus a configurable markup, e.g. cost/2 * 1.15). The
    # algorithm ignores it; upper layers use it to group billing and label
    # the document/cutting plan.
    half_board: bool = False

    def __post_init__(self):
        """Validates the material's dimensions"""
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"Dimensions must be positive: width={self.width}, height={self.height}"
            )
        if self.thickness < 0:
            raise ValueError(
                f"Thickness cannot be negative: thickness={self.thickness}"
            )

    @property
    def area(self) -> float:
        """Computes the material's area"""
        return self.width * self.height

    def __repr__(self) -> str:
        return f"Material(id={self.id}, w={self.width}, h={self.height}, t={self.thickness})"


@dataclass(frozen=True)
class BinSpec:
    """A sheet type the search may open: geometry + cost + supply.

    ``count=None`` means infinite supply (catalog boards); a finite ``count``
    models offcuts. A half board shares the parent's ``key`` (the material
    identity) and differs only in ``width``/``cost_per_unit``/``half_board`` —
    the search treats it as a cheaper alternative bin for the same material and
    the cost objective decides when it pays off.
    """

    key: str
    width: float
    height: float
    thickness: float
    cost_per_unit: float = 0.0
    count: Optional[int] = None
    half_board: bool = False

    @property
    def area(self) -> float:
        return self.width * self.height

    def to_material(self) -> Material:
        return Material(
            id=self.key,
            width=self.width,
            height=self.height,
            thickness=self.thickness,
            cost_per_unit=self.cost_per_unit,
            half_board=self.half_board,
        )


@dataclass
class CuttingLayout:
    """Represents the cutting layout of a material"""

    material: Material
    placed_pieces: List[PlacedPiece] = field(default_factory=list)
    remainders: List[Rectangle] = field(default_factory=list)
    sheet_number: int = 1
    cuts: List[Cut] = field(default_factory=list)

    @property
    def used_area(self) -> float:
        """Computes the used area (kerf not included)"""
        return sum(p.width * p.height for p in self.placed_pieces)

    @property
    def cut_length(self) -> float:
        """Total cut length (mm): sum of the saw's travel across the sheet."""
        return sum(c.length for c in self.cuts)

    @property
    def efficiency(self) -> float:
        """Computes the material usage efficiency (0-1)"""
        if self.material.area == 0:
            return 0.0
        return self.used_area / self.material.area

    @property
    def waste_area(self) -> float:
        """Computes the wasted area"""
        return self.material.area - self.used_area

    def to_dict(self) -> Dict:
        """Converts to a dictionary for serialization"""
        return {
            "material": {
                "material_key": self.material.id,
                "sheet_number": self.sheet_number,
                "width": self.material.width,
                "height": self.material.height,
                "thickness": self.material.thickness,
                "area": self.material.area,
                "cost_per_unit": self.material.cost_per_unit,
                "half_board": self.material.half_board,
            },
            "placed_pieces": [p.to_dict() for p in self.placed_pieces],
            "statistics": {
                "used_area": self.used_area,
                "waste_area": self.waste_area,
                "efficiency": round(self.efficiency * 100, 2),
                "pieces_count": len(self.placed_pieces),
            },
            "remainders": [
                {"x": r.x, "y": r.y, "width": r.width, "height": r.height}
                for r in self.remainders
            ],
            "cuts": [
                {
                    "x": c.x,
                    "y": c.y,
                    "length": c.length,
                    "is_horizontal": c.is_horizontal,
                }
                for c in self.cuts
            ],
        }
