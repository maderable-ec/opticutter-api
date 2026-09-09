import io
from typing import List, Optional, Set, Tuple

from PIL import Image, ImageColor, ImageDraw, ImageFont

from src.modules.optimizations.patterns import base_label

# Font paths to try in order (macOS first, then Linux/Docker).
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
]

# Diagram palette. The cut diagram only ever prints inside the order packet, on
# the shop's own printer, so it is monochrome throughout: outlines, dimensions and
# labels in black, pieces white, offcuts a neutral grey. The branded (coral) twin
# died with the documents that carried it, and colour was never what carried the
# distinction that matters -- a banded edge is told apart by its FILL (solid for
# soft, hatched for hard), which is exactly what survives a black-and-white print.
COLOR_INK = "black"  # outlines, dimensions, labels, efficiency, banded edges
COLOR_PIECE_FILL = "white"
COLOR_WASTE_FILL = "#ECECEC"  # neutral grey: an offcut is not a piece
COLOR_WASTE_OUTLINE = "#9E9E9E"
# Wood grain. The board is melamine and it has a direction: the operator has to
# lay the sheet the right way round before the first cut, and nothing in the
# drawing used to say which way that was.
COLOR_GRAIN = "#6E6E6E"
# Alpha of the grain overlay. It is painted OVER everything (see ``_draw_grain``),
# so this is what keeps the dimensions and labels underneath readable. It is the
# knob to turn if the grain comes out heavy on paper.
GRAIN_ALPHA = 42
# Grain is a TEXTURE, not a ruling: fine closely-spaced lines, the way the wood
# actually looks, rather than a handful of widely-spaced rules that read as a
# grid drawn over the plan. Both are physical millimetres and therefore FIXED --
# grain does not get coarser because the sheet is bigger -- which also means a
# client's small offcut still comes back with several lines rather than one.
GRAIN_STEP_MM = 26.0
GRAIN_STROKE_MM = 3.0
# ...and real grain is neither evenly spaced, nor all the same weight, nor
# continuous. Perfectly regular lines are what made the first attempt read as
# ruled paper: the irregularity IS the difference between wood and a grid. Each
# line takes a dash pattern (a long streak, a gap, a fleck, a gap), a weight and
# a spacing nudge from these tables by index.
#
# Tables and not an RNG, deliberately: the diagram is rendered from a cached
# payload and mirrored by the web repo, and a texture that reshuffled itself on
# every render would flicker on screen and never match the PDF beside it. The
# three lengths are coprime, so the whole thing only repeats every 385 lines --
# more than any sheet holds.
GRAIN_DASHES_MM = (
    (210, 16, 5, 16),
    (150, 20, 4, 22),
    (260, 14, 6, 14),
    (120, 18, 5, 26),
    (300, 22, 4, 18),
    (180, 15, 7, 20),
    (240, 24, 4, 15),
)
GRAIN_WEIGHTS = (1.0, 0.7, 1.35, 0.85, 1.15)
GRAIN_JITTER = (0.0, 0.22, -0.15, 0.3, -0.28, 0.12, -0.05, 0.18, -0.22, 0.08, -0.12)

# Piece outline thickness. Banded sides are highlighted with a thicker strip
# along the edge, inside the piece.
PIECE_OUTLINE_WIDTH = 2
EDGE_BANDING_WIDTH = PIECE_OUTLINE_WIDTH + 5
# Step (px) of the diagonal hatching that distinguishes hard edge banding.
HATCH_STEP = 6

# White space around the board inside the canvas. A module constant because the
# canvas is the only thing that reports the board's pixel box: with the header
# band measured rather than reserved, its height is ``canvas_height -
# scaled_board_height - 2 * MARGIN`` and nothing else can derive it.
MARGIN = 60

# The header strip above the board: legend, then the board's name. Its height is
# DERIVED from the text it holds (see ``render_layout``) rather than reserved as a
# fixed 150px — that number was sized for a 36px header face, so every later trim
# to the fonts left the gaps behind, floating the name in the middle of an empty
# band.
LEGEND_BOX = 32  # swatch side
LEGEND_TEXT_GAP = 12  # swatch → its text
LEGEND_ITEM_GAP = 50  # one entry → the next
LEGEND_ROW_GAP = 16  # a wrapped legend row → the next
LEGEND_TOP = 24  # canvas top → legend
HEADER_GAP_ABOVE = 16  # legend → board name
HEADER_GAP_BELOW = 14  # board name → the board itself


def _draw_edge_strip(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    rect: Tuple[int, int, int, int],
    color: str,
    hatched: bool,
) -> None:
    """Paints an edge-banding strip inside ``rect``. Solid (soft edge) or with
    diagonal hatching (hard edge). The hatching is drawn on a temporary image
    the size of the strip and pasted, so it ends up clipped to the strip."""
    x0, y0, x1, y1 = rect
    if not hatched:
        draw.rectangle(rect, fill=color)
        return
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    draw.rectangle(rect, outline=color, width=1)
    strip = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(strip)
    offset = -h
    while offset < w:
        sdraw.line([(offset, h), (offset + h, 0)], fill=color, width=1)
        offset += HATCH_STEP
    img.paste(strip, (x0, y0), strip)


def _draw_grain(
    img: Image.Image,
    board_x: int,
    board_y: int,
    scaled_width: int,
    scaled_height: int,
    board_width: float,
    scale: float,
    color: str,
) -> None:
    """Paints the wood grain across the whole board.

    The grain runs along the board's LARGO -- its ``height``, the first dimension
    everything in this system enters first -- and ``_rotated_rect`` maps that axis
    to the canvas's horizontal one, so these come out as horizontal lines spanning
    the full drawn width, spaced along the ancho.

    Painted last, over pieces and offcuts alike, because the grain belongs to the
    sheet and not to what is cut out of it: a piece the optimizer rotated does not
    get its own direction, it gets the board's. It goes on one transparent overlay
    pasted in a single call (the same temp-image trick ``_draw_edge_strip`` uses to
    clip its hatching) so ``GRAIN_ALPHA`` applies uniformly and the dimensions
    underneath keep reading.
    """
    if scaled_width <= 0 or scaled_height <= 0:
        return
    overlay = Image.new("RGBA", (scaled_width, scaled_height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    fill = ImageColor.getrgb(color) + (GRAIN_ALPHA,)
    index = 0
    offset = GRAIN_STEP_MM
    while offset < board_width:
        jitter = GRAIN_JITTER[index % len(GRAIN_JITTER)] * GRAIN_STEP_MM
        y = int((offset + jitter) * scale)
        if 0 < y < scaled_height:
            # The stroke is in millimetres too, so the ink-to-paper ratio of the
            # texture is the same on a thumbnail and on a full landscape sheet.
            weight = GRAIN_WEIGHTS[index % len(GRAIN_WEIGHTS)]
            _draw_grain_line(
                odraw,
                y,
                scaled_width,
                scale,
                GRAIN_DASHES_MM[index % len(GRAIN_DASHES_MM)],
                index,
                fill,
                max(1, round(GRAIN_STROKE_MM * weight * scale)),
            )
        index += 1
        offset += GRAIN_STEP_MM
    img.paste(overlay, (board_x, board_y), overlay)


def _draw_grain_line(
    odraw: ImageDraw.ImageDraw,
    y: int,
    scaled_width: int,
    scale: float,
    dashes: Tuple[int, ...],
    index: int,
    fill: Tuple[int, ...],
    width: int,
) -> None:
    """One grain line: its dash/dot pattern walked along the full drawn width.

    The pattern is started at a per-line phase, or the breaks of neighbouring
    lines queue up into a vertical seam -- which reads as a cut, on a drawing
    whose whole subject is where the cuts are.
    """
    pattern = [max(1.0, d * scale) for d in dashes]
    x = -(index * 173) % sum(pattern)
    x -= sum(pattern)
    step = 0
    while x < scaled_width:
        seg = pattern[step % len(pattern)]
        if step % 2 == 0:  # even entries are ink, odd ones are gaps
            x0, x1 = max(0.0, x), min(float(scaled_width - 1), x + seg)
            if x1 > x0:
                odraw.line([(x0, y), (x1, y)], fill=fill, width=width)
        x += seg
        step += 1


def _rotated_rect(
    board_x: int,
    board_y: int,
    board_height: float,
    scale: float,
    x: float,
    y: float,
    w: float,
    h: float,
) -> Tuple[int, int, int, int]:
    """Maps a board rect (mm) to its pixel rect after rotating the board 90°
    clockwise. The board point ``(bx, by)`` maps to ``(H - by, bx)``, so the
    width/height in mm are swapped: height becomes the horizontal extent and
    width the vertical one. Returns ``(px, py, pw, ph)``."""
    px = board_x + int((board_height - y - h) * scale)
    py = board_y + int(x * scale)
    pw = int(h * scale)
    ph = int(w * scale)
    return px, py, pw, ph


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Loads a scalable TrueType font; falls back to the default bitmap font."""
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


# Measuring a string needs a drawing context but no canvas, and a board asks for
# hundreds of measurements (every label truncation walks one character at a time).
# One throwaway 1x1 for the process, instead of one per question.
_MEASURE = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


def _text_bounds(text: str, font: ImageFont.ImageFont) -> Tuple[int, int, int]:
    """``(width, top, bottom)`` of the text RELATIVE to the anchor point.

    ``draw.text((x, y), …)`` anchors on the font's ascent, not on the glyphs, so
    a line drawn at ``y`` actually inks from ``y + top`` to ``y + bottom`` — with
    ``top`` often a third of the size. Measuring the glyph box alone (what
    ``_text_size`` returns) and treating it as the line's height is what made the
    header's gaps come out smaller than the numbers that set them.
    """
    bbox = _MEASURE.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[1], bbox[3]


def _text_size(text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    """Width and height of the text's glyph box — what it occupies, not where it
    inks. Use ``_text_bounds`` whenever the answer positions a line."""
    width, top, bottom = _text_bounds(text, font)
    return width, bottom - top


def _text_image(
    text: str, font: ImageFont.ImageFont, fill: str, pad: int = 2
) -> Image.Image:
    """Renders the text onto a transparent RGBA image (for pasting/rotating)."""
    x0, y0, x1, y1 = _MEASURE.textbbox((0, 0), text, font=font)
    img = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - x0, pad - y0), text, font=font, fill=fill)
    return img


def _fit_label(
    text: str, font: ImageFont.ImageFont, max_width: int, max_height: int
) -> Optional[str]:
    """Returns the label (truncated with … if needed), or None if it doesn't fit."""
    tw, th = _text_size(text, font)
    if th > max_height or max_width < 24:
        return None
    if tw <= max_width:
        return text
    truncated = text
    while truncated and _text_size(truncated + "…", font)[0] > max_width:
        truncated = truncated[:-1]
    return (truncated + "…") if truncated else None


def _legend_entries(band_types: Set[str]) -> List[Tuple[str, str, int, str, str]]:
    """The legend's entries: ``(fill, outline, width, text, swatch)``.

    ``swatch`` is ``""`` for a plain box, ``"hatch"`` for the hard-edge hatching
    or ``"grain"`` for the veta. The two banding types get one entry each and only
    the ones present in the pattern: with no colour, the hatching is the only
    thing that tells them apart, so it has to be spelled out.
    """
    entries = [
        (COLOR_PIECE_FILL, COLOR_INK, PIECE_OUTLINE_WIDTH, "Pieza", ""),
        (
            COLOR_WASTE_FILL,
            COLOR_WASTE_OUTLINE,
            PIECE_OUTLINE_WIDTH,
            "Retazo / Desperdicio",
            "",
        ),
        ("white", COLOR_INK, 1, "Veta", "grain"),
    ]
    if "Soft" in band_types:
        entries.append((COLOR_INK, COLOR_INK, 1, "Canto suave", ""))
    if "Hard" in band_types:
        entries.append(("white", COLOR_INK, 1, "Canto duro", "hatch"))
    return entries


def _legend_layout(
    entries: List[Tuple[str, str, int, str, str]],
    font: ImageFont.ImageFont,
    x0: int,
    y0: int,
    max_x: Optional[int] = None,
) -> Tuple[List[Tuple[int, int]], int]:
    """Top-left corner of each entry's swatch, plus the legend's bottom edge.

    Split out from the drawing so the canvas can be sized BEFORE it is created:
    the header's height follows the legend's, and the legend wraps to a second
    row on a narrow board. One implementation, so the measure and the drawing
    cannot disagree about where an entry lands.
    """
    positions: List[Tuple[int, int]] = []
    x, y = x0, y0
    for _, _, _, text, _ in entries:
        tw, _ = _text_size(text, font)
        if (
            max_x is not None
            and x > x0
            and x + LEGEND_BOX + LEGEND_TEXT_GAP + tw > max_x
        ):
            x, y = x0, y + LEGEND_BOX + LEGEND_ROW_GAP
        positions.append((x, y))
        x += LEGEND_BOX + LEGEND_TEXT_GAP + tw + LEGEND_ITEM_GAP
    return positions, y + LEGEND_BOX


def _draw_remainder_labels(
    img: Image.Image,
    rect: Tuple[int, int, int, int],
    remainder: dict,
    dim_font: ImageFont.ImageFont,
) -> None:
    """An offcut's size, on its edges, exactly like a piece's: the height (its
    horizontal extent after the rotation) along the bottom, the width (its
    vertical extent) along the left, rotated. Each is dropped on its own if it
    does not fit, which is what keeps the long dimension of a thin strip when the
    short one has no room.

    Takes the pixel rect the shape pass already computed. Drawn AFTER the grain,
    so the texture never runs through a number.
    """
    rx, ry, rw, rh = rect
    pad = 4
    alto = _text_image(str(int(remainder["height"])), dim_font, COLOR_INK)
    if alto.width <= rw - 2 * pad and alto.height <= rh - 2 * pad:
        img.paste(
            alto, (rx + (rw - alto.width) // 2, ry + rh - alto.height - pad), alto
        )
    ancho = _text_image(str(int(remainder["width"])), dim_font, COLOR_INK).rotate(
        90, expand=True
    )
    if ancho.height <= rh - 2 * pad and ancho.width <= rw - 2 * pad:
        img.paste(ancho, (rx + pad, ry + (rh - ancho.height) // 2), ancho)


class VisualizationService:
    @staticmethod
    def render_layout(
        group: dict,
        target_long: int = 2000,
        board_name: Optional[str] = None,
    ) -> Image.Image:
        """Draws a single cutting pattern filling the whole canvas.

        The canvas adopts the board's aspect ratio so that, embedded full-page,
        it fills it to the maximum. Each piece's height is dimensioned along the
        left edge (vertical text) and its width along the bottom edge; the label
        is centered. Offcuts are dimensioned the same way (in the muted waste
        color) when they are large enough to hold the text. The wood grain is laid
        over the finished board.

        Returns the PIL image, not an encoded buffer: its one consumer hands it
        straight to reportlab, which wants the pixels. Encoding a PNG only for
        reportlab to decode it again was 60% of the packet's render time — see
        ``generate_layout_image`` for the buffer, which nothing in the PDF path
        needs any more.

        ``board_name`` is the material's printed name, resolved by the caller off
        the payload's ``materials_summary`` (the render layer never touches the
        DB). The header said "Tablero 1", a pattern index that means nothing at
        the saw: on a multi-material job the operator has to know WHICH board this
        pattern is cut on. Falls back to the old label when the name is unknown —
        a snapshot from before the summary carried it, say.
        """
        layout = group.get("layout", group)
        count = group.get("count", 1)
        material = layout.get("material", {})
        board_width = material.get("width", 1220)
        board_height = material.get("height", 2440)

        # The board is drawn rotated 90° clockwise (landscape): the board's height
        # becomes the canvas's horizontal extent and its width the vertical one.
        scale = target_long / max(board_width, board_height)
        scaled_board_width = int(board_height * scale)
        scaled_board_height = int(board_width * scale)

        canvas_width = scaled_board_width + 2 * MARGIN

        # The header now carries the board's full name instead of "Tablero 1",
        # so it is set smaller than the index was: a long melamine name has to
        # fit on the line next to the dimensions and the efficiency.
        header_font = _load_font(18)
        # Dimensions and labels are deliberately smaller than the canvas would
        # suggest: the diagram prints on a landscape sheet at ~1.47x the old
        # scale, so these still come out larger on paper than before (~7.6pt and
        # ~8.7pt vs 6.4pt and 7.4pt), and the smaller face lets ``_fit_label``
        # keep labels that used to be dropped for not fitting.
        dim_font = _load_font(21)
        label_font = _load_font(24)
        legend_font = _load_font(16)

        # Edge-banding types present in the pattern (for the legend). A banded
        # piece with no known type (older snapshots) is treated as solid → "Soft".
        band_types: Set[str] = set()
        for piece in layout.get("placed_pieces", []):
            edges = piece.get("edges") or {}
            if edges.get("sides"):
                bt = edges.get("band_type")
                band_types.add(bt if bt in ("Soft", "Hard") else "Soft")

        badge = f"  ·  ×{count}" if count > 1 else ""
        name = board_name or f"Tablero {group.get('pattern_id', 1)}"
        board_label = f"{name}{badge}  ·  {int(board_height)}×{int(board_width)} mm"
        label_w, label_top, label_bottom = _text_bounds(board_label, header_font)

        # The header band is measured, not reserved: legend, gap, name, gap. Both
        # gaps used to be whatever was left over inside a fixed 150px band, so
        # trimming the fonts only made the empty space bigger. ``label_top`` is
        # subtracted back out so the two constants are the INKED gaps, the ones
        # the eye sees, not distances to an invisible anchor line.
        #
        # Laid out once and handed to the drawing below: the legend's own height
        # is what sets the band, so measuring it here and letting ``_draw_legend``
        # measure it again is both wasted work and two places to disagree.
        legend = _legend_entries(band_types)
        legend_positions, legend_bottom = _legend_layout(
            legend, legend_font, MARGIN, LEGEND_TOP, canvas_width - MARGIN
        )
        label_y = legend_bottom + HEADER_GAP_ABOVE - label_top
        info_height = label_y + label_bottom + HEADER_GAP_BELOW
        canvas_height = info_height + scaled_board_height + 2 * MARGIN

        img = Image.new("RGB", (canvas_width, canvas_height), color="white")
        draw = ImageDraw.Draw(img)

        VisualizationService._draw_legend(
            img, draw, legend, legend_positions, legend_font
        )

        board_x = MARGIN
        board_y = info_height

        draw.text((board_x, label_y), board_label, fill=COLOR_INK, font=header_font)
        efficiency = layout.get("statistics", {}).get("efficiency", 0)
        draw.text(
            (board_x + label_w + 30, label_y),
            f"Eficiencia: {efficiency:.1f}%",
            fill=COLOR_INK,
            font=header_font,
        )

        draw.rectangle(
            [
                board_x,
                board_y,
                board_x + scaled_board_width,
                board_y + scaled_board_height,
            ],
            outline=COLOR_INK,
            width=3,
        )

        # Shapes first, then the grain, then every label: the grain belongs to the
        # sheet and has to run over the pieces and the offcuts, but a texture
        # drawn through a dimension is just a dirty number. Both passes read the
        # same pixel rects, so they are mapped once here rather than derived twice
        # from the board frame.
        def _rect(item: dict) -> Tuple[int, int, int, int]:
            return _rotated_rect(
                board_x,
                board_y,
                board_height,
                scale,
                item["x"],
                item["y"],
                item["width"],
                item["height"],
            )

        pieces = [(p, _rect(p)) for p in layout.get("placed_pieces", [])]
        # An offcut too small to hold ink is not drawn at all, shape or label.
        remainders = [
            (r, rect)
            for r, rect in ((r, _rect(r)) for r in layout.get("remainders", []))
            if rect[2] > 5 and rect[3] > 5
        ]

        for piece, rect in pieces:
            VisualizationService._draw_piece(img, draw, rect, piece)

        for _, (rx, ry, rw, rh) in remainders:
            draw.rectangle(
                [rx, ry, rx + rw, ry + rh],
                fill=COLOR_WASTE_FILL,
                outline=COLOR_WASTE_OUTLINE,
                width=1,
            )

        # Last, so it runs over pieces, offcuts and bare sheet alike.
        _draw_grain(
            img,
            board_x,
            board_y,
            scaled_board_width,
            scaled_board_height,
            board_width,
            scale,
            COLOR_GRAIN,
        )

        for piece, rect in pieces:
            VisualizationService._draw_piece_labels(
                img, rect, piece, dim_font, label_font
            )

        for remainder, rect in remainders:
            _draw_remainder_labels(img, rect, remainder, dim_font)

        return img

    @staticmethod
    def generate_layout_image(
        group: dict,
        target_long: int = 2000,
        board_name: Optional[str] = None,
    ) -> Tuple[io.BytesIO, Tuple[int, int]]:
        """``render_layout`` encoded as a PNG, with the image's size in px.

        The PDF path does NOT go through here — reportlab takes the PIL image
        directly. This is for anything that needs the diagram as a file: an
        export, a debugging script, a future endpoint.
        """
        img = VisualizationService.render_layout(
            group, target_long=target_long, board_name=board_name
        )
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer, img.size

    @staticmethod
    def _draw_legend(
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        legend: List[Tuple[str, str, int, str, str]],
        positions: List[Tuple[int, int]],
        legend_font: ImageFont.ImageFont,
    ) -> None:
        """Draws the legend (piece, offcut, grain and, when present, the two
        banding types) at the positions ``_legend_layout`` already resolved."""
        box = LEGEND_BOX
        for (fill, outline, width, text, swatch), (x, y) in zip(legend, positions):
            _, th = _text_size(text, legend_font)
            if swatch == "hatch":
                _draw_edge_strip(img, draw, (x, y, x + box, y + box), outline, True)
            else:
                draw.rectangle(
                    [x, y, x + box, y + box], fill=fill, outline=outline, width=width
                )
                if swatch == "grain":
                    # Solid and closely spaced, unlike the diagram: at GRAIN_ALPHA
                    # a 32px swatch would read as an empty box, and the point of
                    # the entry is that the mark is a broken texture.
                    for k, gy in enumerate(range(y + 4, y + box - 2, 5)):
                        cut = x + 8 + (k * 5) % 12
                        draw.line([(x + 3, gy), (cut, gy)], fill=COLOR_GRAIN, width=1)
                        draw.line(
                            [(cut + 3, gy), (x + box - 3, gy)],
                            fill=COLOR_GRAIN,
                            width=1,
                        )
            draw.text(
                (x + box + LEGEND_TEXT_GAP, y + (box - th) // 2),
                text,
                fill=COLOR_INK,
                font=legend_font,
            )

    @staticmethod
    def _draw_piece(
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        rect: Tuple[int, int, int, int],
        piece: dict,
    ) -> None:
        """Draws a piece's SHAPE (board rotated 90 degrees clockwise): its
        rectangle and the strips along its banded sides.

        Its text is a separate pass (``_draw_piece_labels``) because the grain
        goes between the two -- over the shapes, under the numbers.
        """
        px, py, pw, ph = rect

        draw.rectangle(
            [px, py, px + pw, py + ph],
            fill=COLOR_PIECE_FILL,
            outline=COLOR_INK,
            width=PIECE_OUTLINE_WIDTH,
        )

        # Banded sides: thick strip along the edge, inside the piece. Hard edges
        # are hatched diagonally and soft (or unknown) ones are solid. After
        # rotating the board 90 degrees clockwise the sides rotate:
        # left->top, top->right, right->bottom, bottom->left.
        edges = piece.get("edges") or {}
        sides = set(edges.get("sides") or [])
        if sides:
            hatched = edges.get("band_type") == "Hard"
            w = EDGE_BANDING_WIDTH
            color = COLOR_INK
            if "left" in sides:
                _draw_edge_strip(img, draw, (px, py, px + pw, py + w), color, hatched)
            if "right" in sides:
                _draw_edge_strip(
                    img, draw, (px, py + ph - w, px + pw, py + ph), color, hatched
                )
            if "bottom" in sides:
                _draw_edge_strip(img, draw, (px, py, px + w, py + ph), color, hatched)
            if "top" in sides:
                _draw_edge_strip(
                    img, draw, (px + pw - w, py, px + pw, py + ph), color, hatched
                )

    @staticmethod
    def _draw_piece_labels(
        img: Image.Image,
        rect: Tuple[int, int, int, int],
        piece: dict,
        dim_font: ImageFont.ImageFont,
        label_font: ImageFont.ImageFont,
    ) -> None:
        """A piece's text: one dimension on the left, another on the bottom, the
        label centered. After the rotation the height in mm is the rect's
        horizontal extent and the width the vertical one.

        Drawn AFTER the grain, so the texture never runs through a number.
        """
        px, py, pw, ph = rect
        pad = 4

        # After rotation, the height (first dimension) is the horizontal extent:
        # it goes along the bottom edge with horizontal text.
        alto = _text_image(str(int(piece["height"])), dim_font, COLOR_INK)
        if alto.width <= pw - 2 * pad and alto.height <= ph - 2 * pad:
            img.paste(
                alto,
                (px + (pw - alto.width) // 2, py + ph - alto.height - pad),
                alto,
            )

        # The width (second dimension) is the vertical extent: it goes along the
        # left edge with vertical text.
        ancho = _text_image(str(int(piece["width"])), dim_font, COLOR_INK).rotate(
            90, expand=True
        )
        if ancho.height <= ph - 2 * pad and ancho.width <= pw - 2 * pad:
            img.paste(ancho, (px + pad, py + (ph - ancho.height) // 2), ancho)

        # Centered text: the piece label (base label, without the instance suffix
        # and omitting the auto-generated piece_N) and, below it, the edge-banding
        # notation (e.g. "2L1C CS"). They're stacked and centered as a block; each
        # line is omitted if it doesn't fit, covering label+notation, label only,
        # or notation only.
        stack = []
        piece_id = base_label(str(piece.get("piece_id", "")))
        if piece_id and not piece_id.startswith("piece_"):
            label = _fit_label(piece_id, label_font, pw - 2 * pad, ph - 2 * pad)
            if label:
                stack.append(_text_image(label, label_font, COLOR_INK))

        notation = (piece.get("edges") or {}).get("notation")
        if notation:
            fitted = _fit_label(notation, dim_font, pw - 2 * pad, ph - 2 * pad)
            if fitted:
                stack.append(_text_image(fitted, dim_font, COLOR_INK))

        if stack:
            gap = 2
            total_h = sum(im.height for im in stack) + gap * (len(stack) - 1)
            if total_h <= ph - 2 * pad:
                y = py + (ph - total_h) // 2
                for im in stack:
                    img.paste(im, (px + (pw - im.width) // 2, y), im)
                    y += im.height + gap
