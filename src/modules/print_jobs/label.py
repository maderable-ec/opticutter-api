"""Server-side rendering of the piece label for the thermal printer (3nStar LTT334).

The whole label — order code, client, dimensions, the piece diagram and the
edge-banding notation — is drawn as a monochrome raster with Pillow and emitted
as a single TSPL ``BITMAP``. Rendering the label as an image (instead of TSPL text
commands) makes it independent of the printer's resident fonts, so it prints
identically on any TSPL unit. The renderer sits behind ``render_label`` so a
different label language (EPL) can be swapped in without touching the service.

Geometry (roll size, dpi, gap, invert) comes from ``config.PRINT_LABEL_*``.
"""

from dataclasses import dataclass, field
from typing import List, Set, Tuple

from PIL import Image, ImageDraw, ImageFont

from src.modules.optimizations.labels import edge_banding_notation
from src.shared.config import config

# Bold first (headers), then a regular fallback; macOS paths first, then Linux/Docker.
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
]

_MM_PER_INCH = 25.4


@dataclass
class LabelData:
    """Everything the label shows, extracted from the order + placed piece."""

    order_code: str
    client_name: str
    piece_label: str
    width_mm: int
    height_mm: int
    notation: str = ""
    # Geometric banded sides (``top``/``bottom``/``left``/``right``), highlighted
    # on the diagram as thick bars.
    sides: Set[str] = field(default_factory=set)


def _px(mm: float) -> int:
    """Millimeters to dots at the configured print resolution."""
    return round(mm / _MM_PER_INCH * config.PRINT_LABEL_DPI)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Loads a scalable TrueType font; falls back to the default bitmap font."""
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    """Truncates ``text`` with an ellipsis so it fits within ``max_w`` pixels."""
    if _text_size(draw, text, font)[0] <= max_w:
        return text
    truncated = text
    while truncated and _text_size(draw, truncated + "…", font)[0] > max_w:
        truncated = truncated[:-1]
    return (truncated + "…") if truncated else ""


def _draw_piece_diagram(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    data: LabelData,
) -> None:
    """Draws a scaled outline of the piece inside ``box`` (x0, y0, x1, y1), with
    the banded sides as thick black bars — the workshop's at-a-glance view."""
    x0, y0, x1, y1 = box
    avail_w, avail_h = x1 - x0, y1 - y0
    if avail_w <= 4 or avail_h <= 4 or data.width_mm <= 0 or data.height_mm <= 0:
        return
    # Fit the piece's aspect ratio (width across, height down) inside the box.
    scale = min(avail_w / data.width_mm, avail_h / data.height_mm)
    w = max(2, int(data.width_mm * scale))
    h = max(2, int(data.height_mm * scale))
    px = x0 + (avail_w - w) // 2
    py = y0 + (avail_h - h) // 2
    draw.rectangle([px, py, px + w, py + h], outline=0, width=max(1, _px(0.3)))
    bar = max(2, _px(1.2))
    if "top" in data.sides:
        draw.rectangle([px, py, px + w, py + bar], fill=0)
    if "bottom" in data.sides:
        draw.rectangle([px, py + h - bar, px + w, py + h], fill=0)
    if "left" in data.sides:
        draw.rectangle([px, py, px + bar, py + h], fill=0)
    if "right" in data.sides:
        draw.rectangle([px + w - bar, py, px + w, py + h], fill=0)


def _render_raster(data: LabelData) -> Image.Image:
    """Draws the full label as a 1-bit image (white background, black ink)."""
    width_px = _px(config.PRINT_LABEL_WIDTH_MM)
    height_px = _px(config.PRINT_LABEL_HEIGHT_MM)
    # TSPL BITMAP rows are byte-aligned: pad the width up to a multiple of 8.
    width_px = ((width_px + 7) // 8) * 8

    img = Image.new("1", (width_px, height_px), 1)  # 1 = white
    draw = ImageDraw.Draw(img)

    margin = _px(2)
    # Font sizes scale with the label height so they stay legible on any roll.
    code_font = _load_font(max(18, height_px // 6))
    text_font = _load_font(max(14, height_px // 11))
    dim_font = _load_font(max(16, height_px // 8))

    # Right third holds the piece diagram; text flows down the left column.
    diagram_w = min(height_px, width_px // 3)
    text_w = width_px - diagram_w - 3 * margin
    _draw_piece_diagram(
        draw,
        (width_px - diagram_w - margin, margin, width_px - margin, height_px - margin),
        data,
    )

    lines: List[Tuple[str, ImageFont.FreeTypeFont]] = [
        (data.order_code, code_font),
        (data.client_name, text_font),
        (f"{data.width_mm} x {data.height_mm} mm", dim_font),
    ]
    # The first tape rides next to the piece's label, as the whole notation
    # always did; with cantos especiales every other tape gets a line of its own
    # (``1C CS BNL``) -- on the label's line it was the part the truncation cut.
    tapes = [t for t in (data.notation or "").split(" · ") if t]
    label_line = data.piece_label or ""
    if tapes:
        label_line = f"{label_line}  {tapes[0]}".strip()
    if label_line:
        lines.append((label_line, text_font))
    lines += [(tape, text_font) for tape in tapes[1:]]

    y = margin
    for text, font in lines:
        line_h = _text_size(draw, "Ag", font)[1]
        if y + line_h > height_px - margin:
            break  # a label is a fixed size: the diagram on the right still shows every side
        fitted = _truncate(draw, text, font, text_w)
        draw.text((margin, y), fitted, font=font, fill=0)
        y += line_h + _px(1.5)

    return img


def _to_tspl(img: Image.Image) -> bytes:
    """Wraps a 1-bit raster in a TSPL job (SIZE/GAP/CLS/BITMAP/PRINT)."""
    width_px, height_px = img.size
    width_bytes = width_px // 8  # width_px is a multiple of 8 (see _render_raster)
    # PIL mode "1" packs 8 px/byte, MSB first: white(255)->bit 1, black(0)->bit 0,
    # which is exactly TSPL's BITMAP convention (bit 0 = printed black dot).
    raster = img.tobytes()
    if config.PRINT_LABEL_INVERT:
        raster = bytes(b ^ 0xFF for b in raster)

    header = (
        f"SIZE {config.PRINT_LABEL_WIDTH_MM:g} mm,{config.PRINT_LABEL_HEIGHT_MM:g} mm\r\n"
        f"GAP {config.PRINT_LABEL_GAP_MM:g} mm,0 mm\r\n"
        "DIRECTION 1\r\n"
        "CLS\r\n"
    ).encode("ascii")
    bitmap_cmd = f"BITMAP 0,0,{width_bytes},{height_px},0,".encode("ascii")
    return header + bitmap_cmd + raster + b"\r\nPRINT 1,1\r\n"


def render_label(data: LabelData) -> bytes:
    """Renders ``data`` to TSPL bytes ready to send RAW to the thermal printer."""
    return _to_tspl(_render_raster(data))


def build_label_data(order, piece) -> LabelData:
    """Extracts the label fields from an order and one of its placed pieces."""
    client = order.client
    client_name = (
        f"{getattr(client, 'first_name', '') or ''} "
        f"{getattr(client, 'last_name', '') or ''}"
    ).strip()
    edges = piece.edges or {}
    sides = set(edges.get("sides") or [])
    notation = edges.get("notation") or edge_banding_notation(
        sides, edges.get("band_type")
    )
    return LabelData(
        order_code=order.code or f"ORD-{order.id:06d}",
        client_name=client_name,
        piece_label=piece.label,
        width_mm=round(piece.original_width),
        height_mm=round(piece.original_height),
        notation=notation,
        sides=sides,
    )
