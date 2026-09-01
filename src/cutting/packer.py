"""Single-bin guillotine packer: the placement primitive every constructor reuses.

``GuillotineOptimizer`` packs one sheet. The multi-sheet orchestration (board
count as an objective, half boards, finite bins) lives in ``search.py``; the
candidate-fill generators live in ``constructors.py``. Both compose this class,
so geometry, kerf, trims and the cut list have a single implementation.
"""

from operator import attrgetter
from typing import List, Tuple

from src.cutting.enums import (
    PACKING_STRATEGY_SPLIT_RULE,
    PackingStrategy,
    SplitRule,
)
from src.cutting.models import (
    Cut,
    Material,
    Piece,
    PlacedPiece,
    Rectangle,
)
from src.cutting.parameters import CuttingParameters

# Sort key for the free-rectangle list; see ``_split_remainder``.
_AREA = attrgetter("area")


def _sort_pieces(pieces: List[Piece], strategy: PackingStrategy) -> List[Piece]:
    """Placement order based on the packing strategy.

    ``MAX_EFFICIENCY`` sorts by decreasing area (Decreasing Area).
    ``LONG_OFFCUTS`` sorts by decreasing height then width, so the tallest
    pieces anchor full columns along the long axis.
    """
    if strategy == PackingStrategy.LONG_OFFCUTS:
        return sorted(pieces, key=lambda p: (-p.priority, -p.height, -p.width))
    return sorted(pieces, key=lambda p: (-p.priority, -p.area))


def expand_pieces(pieces: List[Piece]) -> List[Piece]:
    """Expands ``quantity > 1`` pieces into single instances with unique ids.

    ``#`` is a reserved instance separator: it doesn't collide with labels
    ending in ``_<n>`` (see ``base_label``).

    Already-expanded pools take a fast path. ``optimize()`` calls this on every
    candidate fill, and the search feeds it pools that were expanded once up
    front (``_build_pieces`` emits one ``quantity=1`` instance per physical
    piece), so the general path spent most of its time rebuilding millions of
    ``Piece`` objects identical to its own input. Returning a shallow copy is
    equivalent: the expansion of a list of singletons is that same list, and no
    caller mutates the pieces.
    """
    for piece in pieces:
        if piece.quantity != 1:
            break
    else:
        return list(pieces)

    expanded: List[Piece] = []
    for piece in pieces:
        for i in range(piece.quantity):
            expanded.append(
                Piece(
                    id=f"{piece.id}#{i + 1}" if piece.quantity > 1 else piece.id,
                    width=piece.width,
                    height=piece.height,
                    quantity=1,
                    can_rotate=piece.can_rotate,
                    priority=piece.priority,
                )
            )
    return expanded


class GuillotineOptimizer:
    """Cutting optimizer using the Guillotine algorithm for a single board"""

    def __init__(
        self,
        material: Material,
        cutting_params: CuttingParameters = None,
        split_rule: SplitRule = None,
        min_rect_size: float = 0.1,
        strategy: PackingStrategy = PackingStrategy.MAX_EFFICIENCY,
    ):
        self.material = material
        self.strategy = strategy
        # Explicit ``split_rule`` wins; otherwise it's derived from the strategy.
        self.split_rule = (
            split_rule
            if split_rule is not None
            else PACKING_STRATEGY_SPLIT_RULE[strategy]
        )
        self.cutting_params = cutting_params or CuttingParameters()
        self.kerf = max(0, self.cutting_params.kerf)
        self.top_trim = max(0, self.cutting_params.top_trim)
        self.bottom_trim = max(0, self.cutting_params.bottom_trim)
        self.left_trim = max(0, self.cutting_params.left_trim)
        self.right_trim = max(0, self.cutting_params.right_trim)
        self.min_rect_size = max(0.01, min_rect_size)

        total_horizontal_trim = self.left_trim + self.right_trim
        total_vertical_trim = self.top_trim + self.bottom_trim

        if total_horizontal_trim >= material.width:
            raise ValueError(
                f"Horizontal trims ({total_horizontal_trim}) exceed "
                f"the material width ({material.width})"
            )
        if total_vertical_trim >= material.height:
            raise ValueError(
                f"Vertical trims ({total_vertical_trim}) exceed "
                f"the material height ({material.height})"
            )

        usable_width = material.width - self.left_trim - self.right_trim
        usable_height = material.height - self.top_trim - self.bottom_trim

        self.remainders: List[Rectangle] = [
            Rectangle(self.left_trim, self.bottom_trim, usable_width, usable_height)
        ]
        self.placed_pieces: List[PlacedPiece] = []
        self.cuts: List[Cut] = []

    def optimize(
        self, pieces: List[Piece], presorted: bool = False
    ) -> Tuple[List[PlacedPiece], List[Piece]]:
        """Places the pieces; ``presorted`` keeps the caller's order verbatim.

        The search constructors decide the placement order themselves (it's one
        of the portfolio dimensions), so they pass ``presorted=True``; direct
        callers keep the strategy-derived sort.
        """
        if not pieces:
            return [], []

        expanded_pieces = expand_pieces(pieces)

        sorted_pieces = (
            expanded_pieces
            if presorted
            else _sort_pieces(expanded_pieces, self.strategy)
        )

        unplaced_pieces = []

        for piece in sorted_pieces:
            placed = self._place_piece(piece)
            if not placed:
                unplaced_pieces.append(piece)

        return self.placed_pieces, unplaced_pieces

    def _place_piece(self, piece: Piece) -> bool:
        best_rect_index = -1
        best_rotated = False
        best_score = None

        # Hoisted out of the loop: this runs tens of millions of times per
        # request, so every attribute lookup and method call inside it is paid
        # per (piece, gap) pair. ``Rectangle.contains`` and the fit score are
        # inlined here for the same reason.
        #
        # The fit score ranks gaps, lower first. ``MAX_EFFICIENCY`` is
        # Best-Area-Fit: the leftover area after placing the piece.
        # ``LONG_OFFCUTS`` is Bottom-Left — the gap furthest left and down
        # wins, ties broken by area fit — which pushes pieces into a corner and
        # leaves the dominant leftover as one continuous strip on the opposite
        # side.
        piece_w = piece.width
        piece_h = piece.height
        piece_area = piece.area
        can_rotate = piece.can_rotate
        long_offcuts = self.strategy == PackingStrategy.LONG_OFFCUTS

        for i, rect in enumerate(self.remainders):
            rect_w = rect.width
            rect_h = rect.height
            leftover = rect.area - piece_area
            fit = (rect.x, rect.y, leftover) if long_offcuts else leftover

            if rect_w >= piece_w and rect_h >= piece_h:
                # Secondary key: leftover width after placing. On equal-area
                # ties (same gap, either orientation) it prefers the
                # orientation that completes a full-width band — the
                # structure strip layouts need.
                score = (fit, rect_w - piece_w)
                if best_score is None or score < best_score:
                    best_score = score
                    best_rect_index = i
                    best_rotated = False

            if can_rotate and rect_w >= piece_h and rect_h >= piece_w:
                score = (fit, rect_w - piece_h)
                if best_score is None or score < best_score:
                    best_score = score
                    best_rect_index = i
                    best_rotated = True

        if best_rect_index == -1:
            return False

        rect = self.remainders[best_rect_index]

        if best_rotated:
            placed_width = piece.height
            placed_height = piece.width
        else:
            placed_width = piece.width
            placed_height = piece.height

        placed_piece = PlacedPiece(
            piece=piece,
            x=rect.x,
            y=rect.y,
            width=placed_width,
            height=placed_height,
            rotated=best_rotated,
        )
        self.placed_pieces.append(placed_piece)

        self._split_remainder(best_rect_index, placed_piece)

        return True

    def _split_remainder(self, rect_index: int, placed: PlacedPiece):
        rect = self.remainders[rect_index]

        self.remainders.pop(rect_index)

        width_leftover = rect.width - placed.width
        height_leftover = rect.height - placed.height

        effective_width = placed.width + (self.kerf if width_leftover > 0 else 0)
        effective_height = placed.height + (self.kerf if height_leftover > 0 else 0)

        new_rects, orientation = self._create_split_rectangles(
            rect,
            placed,
            effective_width,
            effective_height,
            width_leftover,
            height_leftover,
        )

        self.cuts.extend(
            self._cuts_for(rect, placed, width_leftover, height_leftover, orientation)
        )

        new_rects = [
            r
            for r in new_rects
            if r.width >= self.min_rect_size and r.height >= self.min_rect_size
        ]

        self.remainders.extend(new_rects)

        # ``attrgetter`` rather than ``lambda r: r.area``: this sort runs after
        # every single placement, so the key is evaluated tens of millions of
        # times per request and a C-level getter removes that many interpreted
        # calls. Same key, same (stable) order.
        self.remainders.sort(key=_AREA)

    def _cuts_for(
        self,
        rect: Rectangle,
        placed: PlacedPiece,
        width_leftover: float,
        height_leftover: float,
        orientation: str,
    ) -> List[Cut]:
        """Cut segments that separate the piece from the rectangle, by orientation.

        ``vertical_first`` leaves the top leftover at full width (horizontal
        cut of ``rect.width``) and the right leftover at the piece's height
        (vertical cut of ``placed.height``). ``horizontal_first`` leaves the
        right leftover at full height (vertical cut of ``rect.height``) and the
        top leftover at the piece's width (horizontal cut of ``placed.width``).
        ``kerf`` is the blade width (perpendicular) and doesn't change the
        length. No leftover on an axis means no cut on that axis (it was
        already separated by a previous cut or the board's edge).
        """
        cuts: List[Cut] = []
        if orientation == "vertical_first":
            if height_leftover > 0:
                cuts.append(
                    Cut(rect.x, rect.y + placed.height, rect.width, is_horizontal=True)
                )
            if width_leftover > 0:
                cuts.append(
                    Cut(
                        rect.x + placed.width,
                        rect.y,
                        placed.height,
                        is_horizontal=False,
                    )
                )
        else:  # horizontal_first
            if width_leftover > 0:
                cuts.append(
                    Cut(
                        rect.x + placed.width,
                        rect.y,
                        rect.height,
                        is_horizontal=False,
                    )
                )
            if height_leftover > 0:
                cuts.append(
                    Cut(
                        rect.x,
                        rect.y + placed.height,
                        placed.width,
                        is_horizontal=True,
                    )
                )
        return cuts

    def _create_split_rectangles(
        self,
        rect: Rectangle,
        placed: PlacedPiece,
        effective_width: float,
        effective_height: float,
        width_leftover: float,
        height_leftover: float,
    ) -> Tuple[List[Rectangle], str]:
        """Creates the leftover rectangles and returns the chosen split orientation.

        The orientation (``"vertical_first"`` | ``"horizontal_first"``) is used
        by ``_cuts_for`` to derive cut lengths from the actual topology.
        """
        new_rects = []
        orientation = "vertical_first"

        if self.split_rule == SplitRule.SHORTER_LEFTOVER_AXIS:
            orientation = (
                "vertical_first"
                if width_leftover <= height_leftover
                else "horizontal_first"
            )

        elif self.split_rule == SplitRule.LONGER_LEFTOVER_AXIS:
            orientation = (
                "vertical_first"
                if width_leftover >= height_leftover
                else "horizontal_first"
            )

        elif self.split_rule == SplitRule.SHORTER_AXIS:
            orientation = (
                "vertical_first" if rect.width <= rect.height else "horizontal_first"
            )

        elif self.split_rule == SplitRule.LONGER_AXIS:
            orientation = (
                "vertical_first" if rect.width >= rect.height else "horizontal_first"
            )

        elif self.split_rule in (SplitRule.MINIMIZE_AREA, SplitRule.MAXIMIZE_AREA):
            vertical_rects = self._vertical_split_first(
                rect, effective_width, effective_height, width_leftover, height_leftover
            )
            horizontal_rects = self._horizontal_split_first(
                rect,
                placed,
                effective_width,
                effective_height,
                width_leftover,
                height_leftover,
            )

            max_vertical = max((r.area for r in vertical_rects), default=0)
            max_horizontal = max((r.area for r in horizontal_rects), default=0)

            if self.split_rule == SplitRule.MINIMIZE_AREA:
                use_vertical = max_vertical <= max_horizontal
            else:
                use_vertical = max_vertical >= max_horizontal

            if use_vertical:
                return vertical_rects, "vertical_first"
            return horizontal_rects, "horizontal_first"

        else:
            orientation = (
                "vertical_first"
                if width_leftover <= height_leftover
                else "horizontal_first"
            )

        if orientation == "vertical_first":
            new_rects = self._vertical_split_first(
                rect, effective_width, effective_height, width_leftover, height_leftover
            )
        else:
            new_rects = self._horizontal_split_first(
                rect,
                placed,
                effective_width,
                effective_height,
                width_leftover,
                height_leftover,
            )

        return new_rects, orientation

    def _vertical_split_first(
        self,
        rect: Rectangle,
        effective_width: float,
        effective_height: float,
        width_leftover: float,
        height_leftover: float,
    ) -> List[Rectangle]:
        new_rects = []

        if width_leftover > 0:
            remaining_width = rect.width - effective_width
            if remaining_width > 0:
                new_rects.append(
                    Rectangle(
                        rect.x + effective_width,
                        rect.y,
                        remaining_width,
                        effective_height - self.kerf
                        if height_leftover > 0
                        else effective_height,
                    )
                )

        if height_leftover > 0:
            remaining_height = rect.height - effective_height
            if remaining_height > 0:
                new_rects.append(
                    Rectangle(
                        rect.x,
                        rect.y + effective_height,
                        rect.width,
                        remaining_height,
                    )
                )

        return new_rects

    def _horizontal_split_first(
        self,
        rect: Rectangle,
        placed: PlacedPiece,
        effective_width: float,
        effective_height: float,
        width_leftover: float,
        height_leftover: float,
    ) -> List[Rectangle]:
        new_rects = []

        if height_leftover > 0:
            remaining_height = rect.height - effective_height
            if remaining_height > 0:
                new_rects.append(
                    Rectangle(
                        rect.x,
                        rect.y + effective_height,
                        effective_width - self.kerf
                        if width_leftover > 0
                        else effective_width,
                        remaining_height,
                    )
                )

        if width_leftover > 0:
            remaining_width = rect.width - effective_width
            if remaining_width > 0:
                new_rects.append(
                    Rectangle(
                        rect.x + effective_width,
                        rect.y,
                        remaining_width,
                        rect.height,
                    )
                )

        return new_rects
