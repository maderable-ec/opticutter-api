# Cutting diagram rendering

`src/modules/optimizations/visualization.py` (`VisualizationService`) draws
the per-board cutting diagram: boards, placed pieces, remainders (waste) and
edge-banded sides, with dimensions and an efficiency percentage. It is built
on Pillow (PIL) and used as an **internal building block**, not a
standalone endpoint — there is no `/optimize/visualize/{hash}` route. The
diagram is embedded directly into the PDF documents rendered by
`proforma.py`:

- the order document (`GET /orders/{id}/document`),
- the order's production sheet (`GET /orders/{id}/production-sheet`),
- the diagram-only document (`generate_diagram_pdf`), which is what the
  consolidated packet (`GET /orders/{id}/consolidated`) and the print agent
  (`POST /print/consolidated`) carry. It has no header of its own — it only ever
  travels inside the packet, where the ORDEN DE PEDIDO identifies the job.

The pre-order proforma (`GET /preorders/{id}/proforma`) and the dispatch sheet
(`GET /orders/{id}/dispatch-sheet`) render **no** diagram.

If you need a diagram outside of those documents (e.g. for a new export or a
debugging script), call `VisualizationService` directly rather than adding a
new public image endpoint — see `proforma.py` for the call pattern.

## Themes

Two color themes share the same drawing code:

| Theme | Used in | Notes |
|-------|---------|-------|
| `brand` | Proforma / order document | Branded palette (coral pieces, dark outlines), matches the MADERABLE letterhead. |
| `mono` | Production sheet, diagram-only document | Black & white, optimized for workshop printing. |

In both themes, a banded edge is drawn as a colored strip along that side of
the piece: solid fill for soft (`Suave`) banding, diagonal hatching for hard
(`Duro`) banding — so the distinction survives in the monochrome sheet, where
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
  a portrait A4 that left ~60% of the paper blank. `proforma._CutterDoc`
  registers a portrait and a landscape `PageTemplate`, and the story switches
  with `NextPageTemplate("landscape")` before the `DISPOSICIÓN DE CORTES`
  section — the piece/board lists stay portrait, the diagrams go landscape, page
  numbering stays continuous, and it is all one PDF. `merge_pdfs` copies each
  page's own mediabox, so the consolidated packet is simply mixed-orientation.
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
