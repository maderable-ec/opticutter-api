# Cutting diagram rendering

`src/modules/optimizations/visualization.py` (`VisualizationService`) draws
the per-board cutting diagram: boards, placed pieces, remainders (waste) and
edge-banded sides, with dimensions and an efficiency percentage. It is built
on Pillow (PIL) and used as an **internal building block**, not a
standalone endpoint — there is no `/optimize/visualize/{hash}` route. The
diagram is embedded into the PDF that `documents.py` renders.

An order emits exactly **one** pdf — `GET /orders/{id}/document`, built by
`build_order_packet` and spooled unchanged by the print agent
(`POST /print/consolidated`). It merges the ORDEN DE PEDIDO (portrait, no
diagram of its own) with the diagram-only document (`generate_diagram_pdf`),
which has no header at all: it only ever travels inside the packet, where the
ORDEN DE PEDIDO already identifies the job. The production and dispatch sheets
no longer exist.

If you need a diagram outside of those documents (e.g. for a new export or a
debugging script), call `VisualizationService` directly rather than adding a
new public image endpoint — see `documents.py` for the call pattern.

## Themes

Two color themes share the same drawing code:

| Theme | Used in | Notes |
|-------|---------|-------|
| `brand` | Available to any caller of `generate_layout_image` | Branded palette (coral pieces, dark outlines), matches the MADERABLE letterhead. |
| `mono` | The diagram pages of the order packet | Black & white, optimized for workshop printing. |

Font sizes live at the top of `generate_layout_image` (`header_font`,
`legend_font`, `dim_font`, `label_font`); the header and the legend were cut
back when the header started carrying a full melamine name instead of an index.

The header band above the board — legend, gap, board name, gap — is **measured,
not reserved**: `_legend_layout` reports where the legend ends (it wraps to a
second row on a narrow board) and `_text_bounds` reports where the name's glyphs
actually ink, so `HEADER_GAP_ABOVE`/`HEADER_GAP_BELOW` are the gaps the eye sees.
It used to be a flat `info_height = 150` sized for a 36px face, so every later
trim to the fonts left the name floating in an empty band. Note `_text_bounds`
and not `_text_size`: `draw.text` anchors on the font's ascent, not on the
glyphs, so the glyph box alone under-measures the line by roughly a third.

In both themes, a banded edge is drawn as a colored strip along that side of
the piece: solid fill for soft (`Suave`) banding, diagonal hatching for hard
(`Duro`) banding — so the distinction survives in the monochrome pages, where
color alone can't carry it.

## Visual elements

- **Boards** — rectangles with a dark outline.
- **Pieces** — filled rectangles with a colored outline; a thicker band along
  any edge-banded side highlights the canto (see Themes above for soft vs.
  hard rendering).
- **Remainders (waste)** — neutral gray rectangles.
- **Annotations** — per-board title, dimensions, and a yield/efficiency
  percentage.

## Layout

- **One cutting pattern per page, on a landscape sheet.** The board is drawn
  rotated 90° (`_rotated_rect`), so the PNG is always wider than it is tall; on
  a portrait A4 that left ~60% of the paper blank. `documents._CutterDoc`
  registers a portrait and a landscape `PageTemplate`; the ORDEN DE PEDIDO uses
  the first from end to end and `generate_diagram_pdf` starts on the second
  (`start_landscape=True`). `merge_pdfs` copies each page's own mediabox, so the
  packet is simply mixed-orientation: portrait lists, then landscape diagrams.
- **The diagram pages carry no footer of their own.** The packet is printed as
  one body, so `stamp_packet_footer` draws the running footer (`Generado el … ·
  N° <orden>` / `Página i de N`) over every page *after* the merge, annexes
  included — the only layer that knows the real page count. Each document used
  to number its own, so a printout read "Página 1" for the order and "Página 1"
  again for the first diagram.
- **The header names the board**, not the pattern: `_board_names` resolves
  `(material_key, half_board)` → the summary's `product_name` (with
  "(medio tablero)" and "· sin refilar" already in it) and passes it as
  `board_name`. "Tablero 1" was a pattern index, which tells the operator
  nothing about *which* board to pull off the rack. The pair is the key because
  the same material cut whole and halved is two summary lines and two headers.
- **Nothing shares a diagram sheet** — no document header, no section title, not
  even `DISPOSICIÓN DE CORTES`. Anything above the image would shrink that one
  pattern and make it inconsistent with the rest, so every diagram is drawn at
  the same maximum size. The diagram-only document therefore starts landscape at
  page 1 (`start_landscape=True`) and is images plus a footer, nothing else.
- Identical patterns are deduplicated by `patterns.group_layouts` and printed
  once with a `×N` badge, so pages count patterns, not physical boards.
- `_build_layout_pages` takes the frame it draws into (`frame_width` /
  `max_height`). A landscape frame is **wider but shorter** than a portrait
  one, so both bounds must travel together: a proportionally tall board is
  scaled down to fit rather than overflowing, which reportlab would reject.
- Scale and minimum dimensions are computed automatically to keep small
  pieces legible.
- Dimension and label text is sized against the *printed* result, not the PNG:
  the landscape sheet draws the canvas at ~1.47x the old portrait scale, so the
  faces were reduced (dimensions 26px, labels 30px) to 21px/24px — still larger
  on paper than before (~7.6pt/~8.7pt vs 6.4pt/7.4pt), and small enough that
  `_fit_label` keeps labels it used to drop. Change these together with the page
  geometry, never on their own.
- Font lookup tries a list of system paths in order (macOS first, then
  Linux/Docker) — see `_FONT_CANDIDATES` — and falls back to PIL's default
  bitmap font if none are found.

## Possible improvements

- Cache rendered diagrams (currently regenerated on every document request).
- Support additional output formats (SVG) for non-PDF consumers.
- Surface cost/kerf annotations directly on the diagram.
