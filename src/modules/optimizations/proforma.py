import base64
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union
from xml.sax.saxutils import escape

from fastapi.responses import StreamingResponse
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from src.modules.optimizations.carrier import ProformaCarrier
from src.modules.optimizations.labels import BAND_TYPE_LABEL, edge_banding_notation
from src.modules.optimizations.patterns import group_layouts
from src.modules.optimizations.schemas import MaterialSource
from src.modules.optimizations.visualization import VisualizationService

# MADERABLE brand palette (sampled from the official letterhead).
BRAND_CORAL = colors.HexColor("#E8564B")  # main accent / table headers
BRAND_ORANGE = colors.HexColor("#EC7829")  # footer band
BRAND_BLACK = colors.HexColor("#1D1D1B")  # logo / text / rules
LIGHT_CORAL = colors.HexColor("#FCE9E6")  # totals box background
ZEBRA_GREY = colors.HexColor("#F5F5F5")
TEXT_GREY = colors.HexColor("#424242")


@dataclass(frozen=True)
class Palette:
    """Themed colors for a PDF. Lets the proforma go branded and the production
    sheet stay black and white while reusing the same builders."""

    accent: colors.Color  # table header, section rule, totals border
    accent_fill: colors.Color  # totals box / TOTAL row background
    text: colors.Color  # titles and dark rules
    text_grey: colors.Color  # secondary text in cells
    zebra: colors.Color  # alternating rows
    header_text: colors.Color  # text over the table header


# Commercial proforma: brand palette. Production sheet: monochrome for the
# workshop (black header with white text, legible when printed/photocopied in B/W).
BRAND_PALETTE = Palette(
    accent=BRAND_CORAL,
    accent_fill=LIGHT_CORAL,
    text=BRAND_BLACK,
    text_grey=TEXT_GREY,
    zebra=ZEBRA_GREY,
    header_text=colors.whitesmoke,
)
MONO_PALETTE = Palette(
    accent=BRAND_BLACK,
    accent_fill=colors.HexColor("#EAEAEA"),
    text=BRAND_BLACK,
    text_grey=colors.HexColor("#333333"),
    zebra=colors.HexColor("#F2F2F2"),
    header_text=colors.white,
)

PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT_MARGIN = RIGHT_MARGIN = 0.5 * inch
CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

TOP_MARGIN = 0.4 * inch
BOTTOM_MARGIN = 0.45 * inch

# Cut diagrams print on landscape sheets: the PNG is already drawn with the board
# rotated 90 degrees (see ``visualization.generate_layout_image``), so a portrait
# page leaves ~60% of the paper blank. Landscape gives the same diagram ~2.2x the
# area without spending an extra sheet.
LANDSCAPE_SIZE = landscape(A4)
LAND_WIDTH, LAND_HEIGHT = LANDSCAPE_SIZE
LAND_CONTENT_WIDTH = LAND_WIDTH - LEFT_MARGIN - RIGHT_MARGIN
# ``Frame`` insets its content by 6pt on every side (reportlab's default, which
# ``SimpleDocTemplate`` also used), so the height a flowable can actually claim is
# 12pt short of the margin box.
FRAME_PADDING = 6
LAND_FRAME_HEIGHT = LAND_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN - 2 * FRAME_PADDING

ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_PATH = ASSETS_DIR / "header.jpg"
WATERMARK_PATH = ASSETS_DIR / "watermark.jpg"
ICON_WHATSAPP = ASSETS_DIR / "whatsapp.jpg"
ICON_EMAIL = ASSETS_DIR / "email.jpg"
ICON_ADDRESS = ASSETS_DIR / "address.jpg"

# Dispatch sheet disclaimer: by signing it the client accepts the goods as
# delivered in good order and releases the company from later claims.
DISPATCH_DISCLAIMER = (
    "Con la firma de este documento, el cliente declara haber recibido y revisado a "
    "entera conformidad las piezas detalladas, verificando cantidades, medidas, color "
    "y el estado de la superficie y los cantos. La empresa queda liberada de toda "
    "responsabilidad por reclamos posteriores a la entrega (faltantes, diferencias de "
    "medida, rayones, despostillados o daños). Los cortes se ejecutan según las medidas "
    "proporcionadas por el cliente; la empresa no se responsabiliza por errores en "
    "dichas medidas. La mercadería viaja por cuenta y riesgo del cliente una vez "
    "retirada de nuestras instalaciones."
)


def _scaled_image(path: Path, width: float) -> Image:
    """Image scaled to ``width`` while preserving its aspect ratio."""
    img_width, img_height = ImageReader(str(path)).getSize()
    height = width * img_height / img_width
    return Image(str(path), width=width, height=height)


def _draw_watermark(canvas, page_width: float, page_height: float) -> None:
    """Faint centered watermark (drawn underneath the content)."""
    reader = ImageReader(str(WATERMARK_PATH))
    img_width, img_height = reader.getSize()
    wm_width = 3.8 * inch
    wm_height = wm_width * img_height / img_width
    canvas.drawImage(
        reader,
        (page_width - wm_width) / 2,
        (page_height - wm_height) / 2,
        width=wm_width,
        height=wm_height,
        mask="auto",
    )


def _draw_footer_accent(canvas, page_width: float) -> None:
    """Angular orange band with a black notch on the bottom edge (letterhead style)."""
    band_h = 12
    slant = 20
    x0 = page_width * 0.52

    canvas.setFillColor(BRAND_BLACK)
    notch = canvas.beginPath()
    notch.moveTo(x0 - 44, 0)
    notch.lineTo(x0 + 6, 0)
    notch.lineTo(x0 + 6 + slant, band_h)
    notch.lineTo(x0 - 44 + slant, band_h)
    notch.close()
    canvas.drawPath(notch, fill=1, stroke=0)

    canvas.setFillColor(BRAND_ORANGE)
    band = canvas.beginPath()
    band.moveTo(x0, 0)
    band.lineTo(page_width, 0)
    band.lineTo(page_width, band_h)
    band.lineTo(x0 + slant, band_h)
    band.close()
    canvas.drawPath(band, fill=1, stroke=0)


def _page_size(doc) -> tuple:
    """Size of the page being drawn, not of the document default.

    A document mixes orientations (portrait lists, landscape diagrams), so every
    footer/watermark position has to come from the *active* page template.
    """
    return getattr(doc.pageTemplate, "pagesize", None) or doc.pagesize


def _draw_footer_line(canvas, page_width: float) -> None:
    """Generation date on the left, page number on the right margin."""
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.grey)
    canvas.drawString(
        LEFT_MARGIN,
        0.35 * inch,
        f"Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')}",
    )
    canvas.drawRightString(
        page_width - RIGHT_MARGIN,
        0.35 * inch,
        f"Página {canvas.getPageNumber()}",
    )


def _draw_page_decoration(canvas, doc) -> None:
    """Watermark, footer accent and generation/page line on every sheet."""
    page_width, page_height = _page_size(doc)
    canvas.saveState()
    _draw_watermark(canvas, page_width, page_height)
    _draw_footer_accent(canvas, page_width)
    _draw_footer_line(canvas, page_width)
    canvas.restoreState()


def _draw_page_decoration_plain(canvas, doc) -> None:
    """Minimal footer for the production sheet: only generation date and page.

    No watermark or footer band (the workshop sheet is black and white).
    """
    page_width, _ = _page_size(doc)
    canvas.saveState()
    _draw_footer_line(canvas, page_width)
    canvas.restoreState()


class _CutterDoc(BaseDocTemplate):
    """A4 document with two page templates: ``portrait`` for the lists and
    ``landscape`` for the cut diagrams.

    Compact margins to save paper; the horizontal margin stays fixed so table
    widths don't need recalculating. A story switches orientation by emitting
    ``NextPageTemplate("landscape")`` before a ``PageBreak`` — the switch sticks
    for every following page (``BaseDocTemplate`` only changes template when one
    is explicitly queued), which is why the diagram section never reverts.
    """

    def __init__(
        self,
        buffer: io.BytesIO,
        on_page,
        top: float = TOP_MARGIN,
        bottom: float = BOTTOM_MARGIN,
        start_landscape: bool = False,
    ):
        super().__init__(
            buffer,
            pagesize=LANDSCAPE_SIZE if start_landscape else A4,
            topMargin=top,
            bottomMargin=bottom,
            leftMargin=LEFT_MARGIN,
            rightMargin=RIGHT_MARGIN,
        )
        portrait = PageTemplate(
            id="portrait",
            pagesize=A4,
            onPage=on_page,
            frames=[
                Frame(
                    LEFT_MARGIN,
                    bottom,
                    CONTENT_WIDTH,
                    PAGE_HEIGHT - top - bottom,
                    id="portrait_frame",
                )
            ],
        )
        landscape_tpl = PageTemplate(
            id="landscape",
            pagesize=LANDSCAPE_SIZE,
            onPage=on_page,
            frames=[
                Frame(
                    LEFT_MARGIN,
                    bottom,
                    LAND_CONTENT_WIDTH,
                    LAND_HEIGHT - top - bottom,
                    id="landscape_frame",
                )
            ],
        )
        # The build starts on ``pageTemplates[0]``.
        self.addPageTemplates(
            [landscape_tpl, portrait] if start_landscape else [portrait, landscape_tpl]
        )


class ProformaService:
    @staticmethod
    def generate_proforma_pdf(
        carrier: ProformaCarrier,
        title: str = "PROFORMA",
        include_diagram: bool = True,
    ) -> io.BytesIO:
        """Commercial document: requirements, priced materials and layout.

        The same render serves both the quote (``title="PROFORMA"``, non-binding)
        and the confirmed order (``title="ORDEN DE PEDIDO"``, committed); only the
        header label changes.

        ``include_diagram=False`` drops the cut-layout pages: used by the
        consolidated print packet, where the diagram lives once in the production
        sheet and would otherwise be duplicated here.
        """
        buffer = io.BytesIO()
        doc = _CutterDoc(buffer, _draw_page_decoration)

        styles = getSampleStyleSheet()
        heading_style = _heading_style(styles)
        cell_style = _cell_style(styles)

        story = []
        story.extend(ProformaService._build_header(carrier, styles, title))
        story.append(Spacer(1, 0.15 * inch))

        story.extend(_section("INFORMACIÓN DEL CLIENTE", heading_style))
        story.append(ProformaService._build_client_table(carrier))
        story.append(Spacer(1, 0.15 * inch))

        story.extend(_section("DETALLE DE REQUERIMIENTOS", heading_style))
        story.append(ProformaService._build_requirements_table(carrier, cell_style))
        story.append(Spacer(1, 0.15 * inch))

        # Omitted outright when nothing is billed — a job cut entirely on the
        # client's material would otherwise print a "RESUMEN DE MATERIALES"
        # heading over a single "Sin datos de materiales" row, immediately above
        # the block that does list what was cut.
        if _billable_material_rows(carrier) or carrier.edge_bandings_summary:
            story.extend(_section("RESUMEN DE MATERIALES", heading_style))
            story.append(ProformaService._build_materials_table(carrier, cell_style))
            story.append(Spacer(1, 0.15 * inch))

        # The client's own retazos: listed so the document says what was cut, but
        # never priced. Putting them in the table above would fill a Subtotal
        # column with $0.00 rows that read as an error — what the shop actually
        # charges for this job is the cutting and the banding, further down.
        client_material = _client_material_rows(carrier)
        if client_material:
            story.extend(_section("MATERIAL DEL CLIENTE", heading_style))
            story.append(
                ProformaService._build_client_material_table(
                    client_material, cell_style
                )
            )
            story.append(Spacer(1, 0.15 * inch))

        if carrier.additional_services:
            story.extend(_section("SERVICIOS ADICIONALES", heading_style))
            story.append(ProformaService._build_services_table(carrier, cell_style))
            story.append(Spacer(1, 0.15 * inch))

        story.append(ProformaService._build_totals_table(carrier))

        payment_block = ProformaService._payment_section(carrier, heading_style)
        if payment_block:
            story.append(Spacer(1, 0.15 * inch))
            story.extend(payment_block)

        story.append(Spacer(1, 0.18 * inch))
        # Validity only applies to quotes (pre-order / live optimization); an
        # already-confirmed order doesn't carry it (``carrier.validity_days`` is ``None``).
        validity_note = (
            f"Esta proforma es válida por {carrier.validity_days} días. "
            if carrier.validity_days
            else ""
        )
        story.append(
            Paragraph(
                f"{validity_note}Valores en USD. Los precios no incluyen IVA; "
                f"el impuesto se detalla en el total.",
                ParagraphStyle(
                    "Note",
                    parent=styles["Normal"],
                    fontSize=8,
                    textColor=colors.grey,
                    alignment=TA_LEFT,
                ),
            )
        )

        if include_diagram:
            # The diagrams go on landscape sheets and stay there until the end of
            # the document, so the note above has to be emitted before the switch.
            story.append(NextPageTemplate("landscape"))
            story.append(PageBreak())
            story.extend(
                ProformaService._build_layout_pages(
                    carrier,
                    frame_width=LAND_CONTENT_WIDTH,
                    max_height=LAND_FRAME_HEIGHT,
                )
            )

        doc.build(story)
        buffer.seek(0)
        return buffer

    @staticmethod
    def generate_production_sheet_pdf(carrier: ProformaCarrier) -> io.BytesIO:
        """Production sheet for the workshop: black and white, no letterhead, cut
        list and layout WITHOUT prices. Makes the most of the paper (compact
        margins and spacing) and differentiates the edge-banding type (soft/hard)."""
        buffer = io.BytesIO()
        doc = _CutterDoc(buffer, _draw_page_decoration_plain)
        pal = MONO_PALETTE
        pad = 4

        styles = getSampleStyleSheet()
        heading_style = _heading_style(styles)
        cell_style = _cell_style(styles)

        story = []
        story.extend(ProformaService._build_production_header(carrier, styles, pal))
        story.append(Spacer(1, 0.12 * inch))

        story.extend(_section("LISTA DE CORTE", heading_style, pal, space_after=4))
        story.append(
            ProformaService._build_requirements_table(carrier, cell_style, pal, pad)
        )
        story.append(Spacer(1, 0.12 * inch))

        story.extend(_section("TABLEROS A UTILIZAR", heading_style, pal, space_after=4))
        story.append(
            ProformaService._build_materials_plain_table(carrier, cell_style, pal, pad)
        )
        story.append(Spacer(1, 0.1 * inch))
        story.append(ProformaService._build_boards_total_table(carrier, pal))

        if carrier.edge_bandings_summary:
            story.append(Spacer(1, 0.12 * inch))
            story.extend(
                _section("TAPACANTOS A APLICAR", heading_style, pal, space_after=4)
            )
            story.append(
                ProformaService._build_edge_bandings_table(
                    carrier, cell_style, with_prices=False, palette=pal, pad=pad
                )
            )

        story.append(Spacer(1, 0.12 * inch))
        story.extend(
            _section("RESUMEN DE CORTE Y CANTO", heading_style, pal, space_after=4)
        )
        story.append(ProformaService._build_cut_summary_table(carrier, pal, pad))

        story.append(NextPageTemplate("landscape"))
        story.append(PageBreak())
        story.extend(
            ProformaService._build_layout_pages(
                carrier,
                mono=True,
                frame_width=LAND_CONTENT_WIDTH,
                max_height=LAND_FRAME_HEIGHT,
            )
        )

        doc.build(story)
        buffer.seek(0)
        return buffer

    @staticmethod
    def generate_diagram_pdf(carrier: ProformaCarrier) -> io.BytesIO:
        """Cutting diagram only (the *gráfico*), B/W, WITHOUT the piece/board lists.

        Used by the consolidated print packet: the cut list, boards and edge
        banding already appear in the ORDEN DE PEDIDO, so here we print just the
        visual layout (``DISPOSICIÓN DE CORTES``) under a compact identifying header.
        """
        buffer = io.BytesIO()
        # Every page of this document is a diagram, so it is landscape throughout
        # and carries no header at all: it only ever travels inside the
        # consolidated packet, where the ORDEN DE PEDIDO already identifies the
        # job, and a header would shrink the first diagram for nothing.
        doc = _CutterDoc(buffer, _draw_page_decoration_plain, start_landscape=True)

        story = ProformaService._build_layout_pages(
            carrier,
            mono=True,
            frame_width=LAND_CONTENT_WIDTH,
            max_height=LAND_FRAME_HEIGHT,
        )

        doc.build(story)
        buffer.seek(0)
        return buffer

    @staticmethod
    def generate_dispatch_sheet_pdf(carrier: ProformaCarrier) -> io.BytesIO:
        """Dispatch sheet (delivery to the client): brand letterhead, client data,
        piece detail WITHOUT prices, board count, liability disclaimer note and
        signature block (delivered-by / received-in-good-order)."""
        buffer = io.BytesIO()
        doc = _CutterDoc(buffer, _draw_page_decoration)

        styles = getSampleStyleSheet()
        heading_style = _heading_style(styles)
        cell_style = _cell_style(styles)

        story = []
        story.extend(ProformaService._build_header(carrier, styles, "HOJA DE DESPACHO"))
        story.append(Spacer(1, 0.15 * inch))

        story.extend(_section("INFORMACIÓN DEL CLIENTE", heading_style))
        story.append(ProformaService._build_client_table(carrier))
        # Dispatch date and the person responsible for delivery (frozen on the
        # order; fall back to "now" / "—" if not yet dispatched).
        dispatch_date = carrier.dispatch_date or datetime.now()
        story.append(
            Paragraph(
                f"<b>Fecha de despacho:</b> {dispatch_date.strftime('%d/%m/%Y')}"
                f" &nbsp;&nbsp; <b>Despachado por:</b> "
                f"{carrier.dispatched_by_label or '—'}",
                ParagraphStyle(
                    "DispatchMeta",
                    parent=styles["Normal"],
                    fontSize=9,
                    textColor=TEXT_GREY,
                    alignment=TA_LEFT,
                    spaceBefore=6,
                ),
            )
        )
        story.append(Spacer(1, 0.15 * inch))

        story.extend(_section("DETALLE DE PIEZAS", heading_style))
        story.append(ProformaService._build_requirements_table(carrier, cell_style))
        story.append(Spacer(1, 0.12 * inch))
        story.append(ProformaService._build_boards_total_table(carrier))
        story.append(Spacer(1, 0.15 * inch))

        payment_block = ProformaService._payment_section(carrier, heading_style)
        if payment_block:
            story.extend(payment_block)
            story.append(Spacer(1, 0.15 * inch))

        story.extend(_section("DESCARGO DE RESPONSABILIDAD", heading_style))
        story.append(
            Paragraph(
                DISPATCH_DISCLAIMER,
                ParagraphStyle(
                    "Disclaimer",
                    parent=styles["Normal"],
                    fontSize=8,
                    leading=11,
                    textColor=colors.grey,
                    alignment=TA_JUSTIFY,
                ),
            )
        )

        story.append(
            KeepTogether(
                [Spacer(1, 0.25 * inch), ProformaService._build_signature_block(styles)]
            )
        )

        doc.build(story)
        buffer.seek(0)
        return buffer

    @staticmethod
    def _build_signature_block(styles) -> Table:
        """Three delivery signature slots side by side (multiple staff may hand
        over the goods), plus the client's receipt signature below spanning the
        full width. The space to sign comes from the padding above each line;
        the line goes above each label."""
        label_style = ParagraphStyle(
            "SignLabel",
            parent=styles["Normal"],
            fontSize=9,
            textColor=BRAND_BLACK,
            alignment=TA_CENTER,
            fontName="Helvetica-Bold",
        )
        sub_style = ParagraphStyle(
            "SignSub",
            parent=styles["Normal"],
            fontSize=8,
            textColor=TEXT_GREY,
            alignment=TA_CENTER,
            leading=11,
        )

        def _slot(label: str, sub: str) -> List[Paragraph]:
            return [Paragraph(label, label_style), Paragraph(sub, sub_style)]

        cliente = _slot("Recibí conforme — Cliente", "Nombre / C.I. / Firma / Fecha")

        col_width = CONTENT_WIDTH / 3
        table = Table(
            [
                [
                    _slot("Entregado por", "Nombre / Firma"),
                    _slot("Entregado por", "Nombre / Firma"),
                    _slot("Entregado por", "Nombre / Firma"),
                ],
                [cliente, "", ""],
            ],
            colWidths=[col_width, col_width, col_width],
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (0, 1), (-1, 1), "CENTER"),
                    ("SPAN", (0, 1), (-1, 1)),
                    ("LINEABOVE", (0, 0), (0, 0), 0.75, BRAND_BLACK),
                    ("LINEABOVE", (1, 0), (1, 0), 0.75, BRAND_BLACK),
                    ("LINEABOVE", (2, 0), (2, 0), 0.75, BRAND_BLACK),
                    ("LINEABOVE", (0, 1), (-1, 1), 0.75, BRAND_BLACK),
                    ("TOPPADDING", (0, 0), (-1, 0), 6),
                    ("TOPPADDING", (0, 1), (-1, 1), 40),
                ]
            )
        )
        return table

    @staticmethod
    def _build_header(carrier: ProformaCarrier, styles, title: str) -> List:
        """MADERABLE letterhead: logo + contact, black rule and title bar."""
        logo = _scaled_image(LOGO_PATH, 1.9 * inch)
        logo.hAlign = "LEFT"

        header_table = Table(
            [[logo, ProformaService._build_contact_block(carrier, styles)]],
            colWidths=[CONTENT_WIDTH * 0.38, CONTENT_WIDTH * 0.62],
        )
        header_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (0, 0), (0, 0), "LEFT"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )

        rule = HRFlowable(
            width="100%",
            thickness=1.5,
            color=BRAND_BLACK,
            spaceBefore=6,
            spaceAfter=6,
        )

        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Normal"],
            fontSize=16,
            leading=20,
            textColor=BRAND_CORAL,
            fontName="Helvetica-Bold",
            alignment=TA_LEFT,
        )
        meta_style = ParagraphStyle(
            "DocMeta",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            textColor=BRAND_BLACK,
            alignment=TA_RIGHT,
        )
        title_bar = Table(
            [
                [
                    Paragraph(title, title_style),
                    [
                        Paragraph(
                            f"N° {carrier.reference}<br/>"
                            f"Fecha: {datetime.now().strftime('%d/%m/%Y')}",
                            meta_style,
                        ),
                        *_reference_lines(carrier, meta_style),
                    ],
                ]
            ],
            colWidths=[CONTENT_WIDTH * 0.5, CONTENT_WIDTH * 0.5],
        )
        title_bar.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )

        return [header_table, rule, title_bar]

    @staticmethod
    def _build_contact_block(carrier: ProformaCarrier, styles) -> Table:
        """Contact block with icons: WhatsApp, email and branches.

        Reads the company data (letterhead) live from ``carrier.company``.
        """
        company = carrier.company or {}
        text_style = ParagraphStyle(
            "Contact",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=BRAND_BLACK,
            alignment=TA_LEFT,
        )
        icon_w = 0.18 * inch

        rows = [
            [
                _scaled_image(ICON_WHATSAPP, icon_w),
                Paragraph(company.get("phone", ""), text_style),
            ],
            [
                _scaled_image(ICON_EMAIL, icon_w),
                Paragraph(company.get("email", ""), text_style),
            ],
        ]
        for branch in company.get("branches") or []:
            rows.append(
                [
                    _scaled_image(ICON_ADDRESS, icon_w),
                    Paragraph(
                        f"<b>{branch['name']}</b> {branch['address']}", text_style
                    ),
                ]
            )

        table = Table(rows, colWidths=[icon_w + 8, 4.0 * inch])
        table.hAlign = "RIGHT"
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (0, 0), (0, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (0, -1), 6),
                    ("RIGHTPADDING", (1, 0), (1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        return table

    @staticmethod
    def _build_production_header(
        carrier: ProformaCarrier,
        styles,
        palette: Palette = MONO_PALETTE,
        title: str = "HOJA DE PRODUCCIÓN",
        content_width: float = CONTENT_WIDTH,
    ) -> List:
        """Compact workshop header: title + No./date/client, no logo or
        letterhead. The client name goes here (the sheet has no CLIENT section).
        """
        client = carrier.client
        client_name = (
            f"{getattr(client, 'first_name', '') or ''} "
            f"{getattr(client, 'last_name', '') or ''}".strip()
            or "N/A"
        )

        title_style = ParagraphStyle(
            "ProdTitle",
            parent=styles["Normal"],
            fontSize=15,
            leading=18,
            textColor=palette.text,
            fontName="Helvetica-Bold",
            alignment=TA_LEFT,
        )
        meta_style = ParagraphStyle(
            "ProdMeta",
            parent=styles["Normal"],
            fontSize=9,
            leading=12,
            textColor=palette.text,
            alignment=TA_RIGHT,
        )

        header = Table(
            [
                [
                    Paragraph(title, title_style),
                    [
                        Paragraph(
                            f"N° {carrier.reference}<br/>"
                            f"Fecha: {datetime.now().strftime('%d/%m/%Y')}<br/>"
                            f"Cliente: {client_name}",
                            meta_style,
                        ),
                        *_reference_lines(carrier, meta_style),
                    ],
                ]
            ],
            colWidths=[content_width * 0.5, content_width * 0.5],
        )
        header.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        rule = HRFlowable(
            width="100%",
            thickness=1.0,
            color=palette.text,
            spaceBefore=4,
            spaceAfter=4,
        )
        return [header, rule]

    @staticmethod
    def _build_client_table(carrier: ProformaCarrier) -> Table:
        client = carrier.client
        client_name = (
            f"{client.first_name or ''} {client.last_name or ''}".strip() or "N/A"
        )
        client_data = [
            ["Nombre:", client_name],
            ["Celular:", getattr(client, "phone", None) or "N/A"],
        ]
        if getattr(client, "email", None):
            client_data.append(["Email:", client.email])
        client_table = Table(
            client_data, colWidths=[1.5 * inch, CONTENT_WIDTH - 1.5 * inch]
        )
        client_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("TEXTCOLOR", (0, 0), (-1, -1), TEXT_GREY),
                    ("BACKGROUND", (0, 0), (0, -1), ZEBRA_GREY),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return client_table

    @staticmethod
    def _build_requirements_table(
        carrier: ProformaCarrier,
        cell_style,
        palette: Palette = BRAND_PALETTE,
        pad: int = 5,
    ) -> Table:
        requirements = carrier.requirements
        # "Material", not "Tablero": the row can name a retazo, and a quote can be
        # made of nothing else.
        req_data = [["#", "Alto", "Ancho", "Cant.", "Material", "Cantos", "Etiqueta"]]
        if isinstance(requirements, list):
            for idx, req in enumerate(requirements, 1):
                req_data.append(
                    [
                        str(idx),
                        f"{req.get('height', 0)} mm",
                        f"{req.get('width', 0)} mm",
                        str(req.get("quantity", 1)),
                        req.get("product_code") or "N/A",
                        Paragraph(_edge_banding_notation(req), cell_style),
                        Paragraph(req.get("label") or "-", cell_style),
                    ]
                )

        req_table = Table(
            req_data,
            colWidths=[
                0.35 * inch,
                0.8 * inch,
                0.8 * inch,
                0.55 * inch,
                1.25 * inch,
                # "Cantos" now carries the alias too ("2L1C CS CSH" =
                # 58pt at 9pt Helvetica, against 12pt of cell padding), so it
                # takes 0.15" from the flexible "Etiqueta" column for headroom.
                1.25 * inch,
                CONTENT_WIDTH - 5.0 * inch,
            ],
            repeatRows=1,
        )
        req_table.setStyle(
            _data_table_style(header_size=10, body_size=9, palette=palette, pad=pad)
        )
        return req_table

    @staticmethod
    def _build_edge_bandings_table(
        carrier: ProformaCarrier,
        cell_style,
        with_prices: bool = True,
        palette: Palette = BRAND_PALETTE,
        pad: int = 7,
    ) -> Table:
        """Edge-banding summary by type. With prices (proforma) or without
        (production sheet), with a ``Tipo`` (Soft/Hard) column for the workshop."""
        summary = carrier.edge_bandings_summary
        if with_prices:
            header = [
                "Código",
                "Descripción",
                "Tipo",
                "Espesor",
                "Metros",
                "P. Unit.",
                "Subtotal",
            ]
        else:
            header = ["Código", "Descripción", "Tipo", "Espesor", "Metros"]
        eb_data = [header]
        for entry in summary:
            row = [
                entry.get("product_code") or "N/A",
                Paragraph(
                    entry.get("product_name") or entry.get("product_code") or "N/A",
                    cell_style,
                ),
                BAND_TYPE_LABEL.get(entry.get("band_type"), "-"),
                f"{(entry.get('thickness') or 0):.2f} mm",
                f"{entry.get('billed_linear_m', 0):.2f} m",
            ]
            if with_prices:
                row.append(f"${entry.get('price_per_m', 0):.2f}")
                row.append(f"${entry.get('total_cost', 0):.2f}")
            eb_data.append(row)

        if with_prices:
            col_widths = [
                0.9 * inch,
                CONTENT_WIDTH - 5.1 * inch,
                0.8 * inch,  # Tipo
                0.9 * inch,
                0.8 * inch,
                0.8 * inch,
                0.9 * inch,
            ]
        else:
            col_widths = [
                1.4 * inch,  # Código (más ancho: códigos largos tipo TAP-SL-CSH-22)
                CONTENT_WIDTH - 3.8 * inch,  # Descripción (flexible)
                0.8 * inch,  # Tipo (Suave/Duro)
                0.8 * inch,  # Espesor
                0.8 * inch,  # Metros
            ]
        eb_table = Table(eb_data, colWidths=col_widths, repeatRows=1)
        eb_table.setStyle(
            _data_table_style(
                header_size=9 if with_prices else 10,
                body_size=8 if with_prices else 9,
                palette=palette,
                pad=pad,
            )
        )
        return eb_table

    @staticmethod
    def _build_materials_table(carrier: ProformaCarrier, cell_style) -> Table:
        """Single materials summary: boards (quantity in units) and edge banding
        (quantity in meters) in one table with code, description, quantity, unit
        price and subtotal. Spans the full content width.

        Everything here is billed. The client's own retazos are rendered apart by
        ``_build_client_material_table``; they have no price by definition, and a
        priced table whose column adds up to zero invites exactly the question
        the seller does not want to answer."""
        mat_data = [["Código", "Descripción", "Cantidad", "P. Unit.", "Subtotal"]]

        has_rows = False
        for entry in _billable_material_rows(carrier):
            has_rows = True
            mat_data.append(
                [
                    entry.get("product_code") or "N/A",
                    Paragraph(
                        entry.get("product_name") or entry.get("product_code") or "N/A",
                        cell_style,
                    ),
                    f"{entry.get('count', 0)} u",
                    f"${entry.get('cost_per_unit', 0):.2f}",
                    f"${entry.get('total_cost', 0):.2f}",
                ]
            )
        for entry in carrier.edge_bandings_summary or []:
            has_rows = True
            mat_data.append(
                [
                    entry.get("product_code") or "N/A",
                    Paragraph(
                        entry.get("product_name") or entry.get("product_code") or "N/A",
                        cell_style,
                    ),
                    f"{entry.get('billed_linear_m', 0):.2f} m",
                    f"${entry.get('price_per_m', 0):.2f}",
                    f"${entry.get('total_cost', 0):.2f}",
                ]
            )
        if not has_rows:
            mat_data.append(["Sin datos de materiales", "", "", "", ""])

        mat_table = Table(
            mat_data,
            colWidths=[
                1.3 * inch,
                CONTENT_WIDTH - 3.9 * inch,
                0.8 * inch,
                0.8 * inch,
                1.0 * inch,
            ],
            repeatRows=1,
        )
        mat_table.setStyle(_data_table_style(header_size=10, body_size=9))
        return mat_table

    @staticmethod
    def _build_materials_plain_table(
        carrier: ProformaCarrier,
        cell_style,
        palette: Palette = BRAND_PALETTE,
        pad: int = 7,
    ) -> Table:
        """Boards to use WITHOUT prices (production sheet): code, dimensions, qty.

        Spans the full content width to align with the cut list."""
        materials_summary = carrier.materials_summary
        mat_data = [["Código", "Nombre", "Dimensiones", "Espesor", "Cantidad"]]
        if isinstance(materials_summary, list) and materials_summary:
            for entry in materials_summary:
                mat_data.append(
                    [
                        entry.get("product_code") or "N/A",
                        Paragraph(
                            entry.get("product_name")
                            or entry.get("product_code")
                            or "N/A",
                            cell_style,
                        ),
                        f"{entry.get('height', 0):.0f}×{entry.get('width', 0):.0f} mm",
                        # ``g`` so a whole thickness prints "15 mm" while a
                        # fractional one (OSB 11.1, MDF fondo 5.5) keeps its
                        # decimal instead of being rounded to a wrong number.
                        f"{entry.get('thickness', 0):g} mm",
                        str(entry.get("count", 0)),
                    ]
                )
        else:
            mat_data.append(["Sin datos de materiales", "", "", "", ""])

        mat_table = Table(
            mat_data,
            colWidths=[
                1.3 * inch,  # Código (más ancho: códigos largos tipo MDP-SL-CSH-15)
                CONTENT_WIDTH - 4.0 * inch,  # Nombre (flexible)
                1.1 * inch,  # Dimensiones
                0.7 * inch,  # Espesor
                0.9 * inch,  # Cantidad
            ],
            repeatRows=1,
        )
        mat_table.setStyle(
            _data_table_style(header_size=10, body_size=9, palette=palette, pad=pad)
        )
        return mat_table

    @staticmethod
    def _build_services_table(carrier: ProformaCarrier, cell_style) -> Table:
        """Additional services: name, quantity, unit price and subtotal.

        Printed NET, like every other line on the document, even though staff
        registers these prices tax-included: the column has to add up to the
        subtotal the tax line is computed over. The per-line rounding matches
        ``build_pricing`` exactly, so the two can't disagree by a cent.

        Spans the full content width, mirroring the materials table."""
        data = [["Servicio", "Cantidad", "P. Unit.", "Subtotal"]]
        for entry in carrier.additional_services or []:
            qty = entry.get("quantity", 0)
            net_total = carrier.service_net(entry)
            unit = net_total / qty if qty else 0.0
            data.append(
                [
                    Paragraph(entry.get("name") or "N/A", cell_style),
                    f"{qty} u",
                    f"${unit:.2f}",
                    f"${net_total:.2f}",
                ]
            )
        table = Table(
            data,
            colWidths=[
                CONTENT_WIDTH - 2.6 * inch,
                0.8 * inch,
                0.8 * inch,
                1.0 * inch,
            ],
            repeatRows=1,
        )
        table.setStyle(_data_table_style(header_size=10, body_size=9))
        return table

    @staticmethod
    def _build_client_material_table(entries: List[dict], cell_style) -> Table:
        """The client's own material: what was cut on, without a price.

        Same shape as the production sheet's board table (``Descripción``,
        dimensions, thickness, sheets) because it answers the same question —
        which material this work ran on — and deliberately no money column.
        """
        data = [["Descripción", "Dimensiones", "Espesor", "Hojas"]]
        for entry in entries:
            data.append(
                [
                    # No ``product_code`` fallback, unlike the priced table: for an
                    # inline material that field holds the optimization's internal
                    # material key, and this document goes to the client. The
                    # summary already falls back to the dimensions when the seller
                    # typed no label, and the next column repeats them anyway.
                    Paragraph(
                        entry.get("product_name") or "Material del cliente",
                        cell_style,
                    ),
                    f"{entry.get('height', 0):.0f}×{entry.get('width', 0):.0f} mm",
                    f"{entry.get('thickness', 0):g} mm",
                    str(entry.get("count", 0)),
                ]
            )
        table = Table(
            data,
            colWidths=[
                CONTENT_WIDTH - 2.9 * inch,
                1.1 * inch,
                0.7 * inch,
                1.1 * inch,
            ],
            repeatRows=1,
        )
        table.setStyle(_data_table_style(header_size=10, body_size=9))
        return table

    @staticmethod
    def _build_totals_table(carrier: ProformaCarrier) -> Table:
        """The money block: net breakdown, then one tax line, then the total.

        There is no discount row any more. The price level the seller chose is a
        different unit price per product, already printed on each line, so the
        subtotal is simply the sum of the document — which is also what makes
        "Subtotal + IVA = Total" verifiable on the page.
        """
        summary_data = []
        # With edge banding there are two cost components, and printing both is
        # what makes the subtotal verifiable on the page. Without it the boards
        # ARE the subtotal, so repeating the number a line above it says nothing
        # and the informative line is how many boards it took.
        #
        # Both rows are then guarded on carrying an actual number, which is what
        # a job cut on the client's own material needs: it buys no board, so
        # "Costo de tableros: $0.00" and "tableros utilizados: 0" would each
        # state something false about a document whose content is the cutting
        # service below.
        if carrier.edge_bandings_summary:
            if carrier.total_boards_cost:
                summary_data.append(
                    ["Costo de tableros:", f"${carrier.total_boards_cost:.2f}"]
                )
            summary_data.append(
                ["Costo de tapacantos:", f"${carrier.total_edge_banding_cost:.2f}"]
            )
        elif carrier.total_boards_used:
            summary_data.append(
                ["Total de tableros utilizados:", str(carrier.total_boards_used)]
            )
        if carrier.additional_services:
            summary_data.append(
                ["Servicios adicionales:", f"${carrier.services_total:.2f}"]
            )
        summary_data.append(["Subtotal:", f"${carrier.subtotal:.2f}"])
        summary_data.append(
            [f"IVA ({carrier.tax_rate * 100:g}%):", f"${carrier.tax_amount:.2f}"]
        )
        summary_data.append(["Costo total estimado:", f"${carrier.total_cost:.2f}"])
        return _totals_table(summary_data)

    @staticmethod
    def _payment_section(carrier: ProformaCarrier, heading_style) -> list:
        """ "FORMA DE PAGO" block (informational): the methods used + total.

        Returns ``[]`` when there's no payment registered, to omit the block on
        ephemeral quotes and on orders not yet sent to the queue. An order
        registered before bank transfer existed leaves that amount ``None``.
        """
        cash = carrier.payment_cash_amount or 0
        transfer = carrier.payment_transfer_amount or 0
        credit = carrier.payment_credit_amount or 0
        if cash <= 0 and transfer <= 0 and credit <= 0:
            return []
        rows: List[List[str]] = []
        if cash > 0:
            rows.append(["Efectivo:", f"${cash:.2f}"])
        if transfer > 0:
            rows.append(["Transferencia:", f"${transfer:.2f}"])
        if credit > 0:
            rows.append(["A crédito:", f"${credit:.2f}"])
        rows.append(["Total:", f"${cash + transfer + credit:.2f}"])
        return [*_section("FORMA DE PAGO", heading_style), _totals_table(rows)]

    @staticmethod
    def _build_boards_total_table(
        carrier: ProformaCarrier, palette: Palette = BRAND_PALETTE
    ) -> Table:
        """Total SHEETS to cut, no costs (production sheet and dispatch sheet).

        Counted off the materials summary rather than from ``total_boards_used``,
        which answers a commercial question — how many boards the client buys —
        and therefore excludes every retazo. The shop cuts the retazos too, and on
        a job made only of the client's material that number was 0 next to a table
        listing two sheets.
        """
        sheets = sum(entry.get("count", 0) for entry in carrier.materials_summary or [])
        return _totals_table(
            [["Total de hojas a cortar:", str(sheets)]],
            palette=palette,
        )

    @staticmethod
    def _build_cut_summary_table(
        carrier: ProformaCarrier,
        palette: Palette = BRAND_PALETTE,
        pad: int = 7,
    ) -> Table:
        """Linear meters of cut and edge banding per sheet + overall total (workshop).

        One row per cutting pattern (deduplicated) with the per-sheet values; the
        TOTAL row is the sum across all physical sheets.
        """
        groups = carrier.layout_groups
        if not (isinstance(groups, list) and groups):
            groups = group_layouts(carrier.layouts or [])

        data = [["Patrón", "Planchas", "Corte (m)", "Canto (m)"]]
        for group in groups:
            stats = (group.get("layout") or {}).get("statistics") or {}
            data.append(
                [
                    f"#{group.get('pattern_id', '?')}",
                    str(group.get("count", 0)),
                    f"{stats.get('cut_linear_m', 0):.2f}",
                    f"{stats.get('edge_banding_linear_m', 0):.2f}",
                ]
            )
        data.append(
            [
                "TOTAL",
                str(carrier.total_boards_used),
                f"{carrier.total_cut_linear_m:.2f}",
                f"{carrier.total_edge_banding_linear_m:.2f}",
            ]
        )

        table = Table(
            data,
            colWidths=[
                CONTENT_WIDTH - 3.6 * inch,
                1.2 * inch,
                1.2 * inch,
                1.2 * inch,
            ],
            repeatRows=1,
        )
        style = _data_table_style(header_size=10, body_size=9, palette=palette, pad=pad)
        # Highlights the TOTAL row (last) as a totals box.
        style.add("BACKGROUND", (0, -1), (-1, -1), palette.accent_fill)
        style.add("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")
        style.add("TEXTCOLOR", (0, -1), (-1, -1), palette.text)
        table.setStyle(style)
        return table

    @staticmethod
    def _build_layout_pages(
        carrier: ProformaCarrier,
        mono: bool = False,
        frame_width: float = CONTENT_WIDTH,
        max_height: float = 9.3 * inch,
    ) -> List:
        """One image per pattern, each alone on a full page using the whole sheet.

        ``frame_width``/``max_height`` describe the frame the images are drawn
        into: the callers print the diagrams on landscape sheets, where the frame
        is wider (770pt vs 523pt) but *shorter* (522pt vs 769pt) than a portrait
        one — so both bounds have to travel together or a tall board overflows.

        Nothing else shares a diagram sheet (no heading, no header), so every
        pattern is drawn at the same, maximum size.
        """
        layouts = carrier.layouts
        if not (isinstance(layouts, list) and layouts):
            return []

        # Uses the persisted groups; recomputes them for old optimizations saved
        # before the ``layout_groups`` field existed.
        groups = carrier.layout_groups
        if not (isinstance(groups, list) and groups):
            groups = group_layouts(layouts)

        flowables: List = []
        for idx, group in enumerate(groups):
            if idx > 0:
                flowables.append(PageBreak())

            img_buffer, (img_w, img_h) = VisualizationService.generate_layout_image(
                group, mono=mono
            )
            draw_width = frame_width
            draw_height = draw_width * (img_h / img_w)
            if draw_height > max_height:
                draw_height = max_height
                draw_width = draw_height * (img_w / img_h)

            image = Image(img_buffer, width=draw_width, height=draw_height)
            image.hAlign = "CENTER"
            flowables.append(image)

        return flowables


def merge_pdfs(buffers: List[io.BytesIO]) -> io.BytesIO:
    """Concatenates every page of each PDF buffer into a single PDF.

    Used by the consolidated print packet to stitch the order document, the
    production sheet, the dispatch sheet and the attachment pages into one file.
    """
    writer = PdfWriter()
    for buf in buffers:
        buf.seek(0)
        for page in PdfReader(buf).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out


def image_to_pdf_buffer(data: bytes) -> io.BytesIO:
    """Wraps a raster image (a screenshot annex) into a one-page A4 PDF.

    The image is scaled to fit the page margins (never upscaled) and centered, so
    it merges cleanly into the consolidated packet like any other PDF page.
    """
    reader = ImageReader(io.BytesIO(data))
    img_w, img_h = reader.getSize()
    max_w = PAGE_WIDTH - 2 * LEFT_MARGIN
    max_h = PAGE_HEIGHT - 2 * LEFT_MARGIN
    scale = min(max_w / img_w, max_h / img_h, 1.0)
    draw_w, draw_h = img_w * scale, img_h * scale
    x = (PAGE_WIDTH - draw_w) / 2
    y = (PAGE_HEIGHT - draw_h) / 2
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.drawImage(
        reader, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto"
    )
    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return buffer


def attachment_to_pdf_part(data: bytes, content_type: str) -> Optional[io.BytesIO]:
    """Turns an attachment's bytes into a mergeable PDF buffer, or ``None``.

    A PDF is passed through (after a structural read); an image is wrapped into a
    page. Returns ``None`` if the bytes can't be parsed/rendered, so a single
    corrupt annex is skipped instead of breaking the whole consolidated packet.
    """
    try:
        if content_type == "application/pdf":
            buffer = io.BytesIO(data)
            PdfReader(buffer)  # validate it has a readable page structure
            buffer.seek(0)
            return buffer
        return image_to_pdf_buffer(data)
    except Exception:
        return None


def pdf_response(
    buffer: io.BytesIO, filename: str, fmt: str = "pdf"
) -> Union[StreamingResponse, dict]:
    """Returns the PDF as a download (``pdf``) or wrapped in base64 JSON (``base64``)."""
    if fmt.lower() == "base64":
        content = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return {
            "format": "base64",
            "content": content,
            "filename": filename,
            "mimeType": "application/pdf",
        }
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _edge_banding_notation(req: dict) -> str:
    """Workshop notation for a piece's edge banding (``2L1C CS CSH``), or ``-`` if none.

    ``band_type``/``alias`` are frozen into the requirement at compute time
    (see ``OptimizationService._dump_requirement``); snapshots predating either
    simply render without that part.
    """
    spec = req.get("edge_banding")
    if not spec:
        return "-"
    text = edge_banding_notation(
        spec.get("sides") or [], spec.get("band_type"), spec.get("alias")
    )
    return text or "-"


def _reference_lines(carrier: ProformaCarrier, meta_style: ParagraphStyle) -> List:
    """ "Ref: ..." line for the document's meta block, or ``[]`` if there's none.

    Rides along with the N°/date block on every document, so the commercial
    reference (project/site name) identifies the job at a glance when the same
    client has several running. The text is user-typed and ``Paragraph`` parses
    mini-HTML, so it's escaped before being inserted.
    """
    notes = (carrier.notes or "").strip()
    if not notes:
        return []
    style = ParagraphStyle(
        "DocRef",
        parent=meta_style,
        fontSize=max(meta_style.fontSize - 1.5, 7),
        leading=max(meta_style.leading - 2, 9),
        textColor=TEXT_GREY,
        spaceBefore=2,
    )
    return [Paragraph(f"Ref: {escape(notes)}", style)]


def _heading_style(styles) -> ParagraphStyle:
    return ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=BRAND_BLACK,
        spaceAfter=2,
        spaceBefore=4,
        fontName="Helvetica-Bold",
    )


def _cell_style(styles) -> ParagraphStyle:
    return ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontSize=9,
        leading=11,
        textColor=TEXT_GREY,
        alignment=TA_LEFT,
    )


def _client_material_rows(carrier: ProformaCarrier) -> List[dict]:
    """Summary lines for material the client brought in (never billed)."""
    return [
        entry
        for entry in carrier.materials_summary or []
        if entry.get("source") == MaterialSource.client_offcut.value
    ]


def _billable_material_rows(carrier: ProformaCarrier) -> List[dict]:
    """Summary lines that belong in the priced table."""
    return [
        entry
        for entry in carrier.materials_summary or []
        if entry.get("source") != MaterialSource.client_offcut.value
    ]


def _section(
    title: str,
    heading_style,
    palette: Palette = BRAND_PALETTE,
    space_after: int = 5,
) -> List:
    """Section title with a colored rule underneath."""
    return [
        Paragraph(title, heading_style),
        HRFlowable(
            width="100%",
            thickness=1.2,
            color=palette.accent,
            spaceBefore=2,
            spaceAfter=space_after,
        ),
    ]


def _totals_table(rows: List[List[str]], palette: Palette = BRAND_PALETTE) -> Table:
    """Highlighted totals box (key on the left, value on the right)."""
    table = Table(rows, colWidths=[CONTENT_WIDTH - 2.0 * inch, 2.0 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), palette.accent_fill),
                ("BOX", (0, 0), (-1, -1), 1, palette.accent),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#F5C9C3")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 11),
                ("TEXTCOLOR", (0, 0), (-1, -1), palette.text),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    return table


def _data_table_style(
    header_size: int,
    body_size: int,
    palette: Palette = BRAND_PALETTE,
    pad: int = 5,
) -> TableStyle:
    """Common style for data tables: accent header + zebra rows."""
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), palette.accent),
            ("TEXTCOLOR", (0, 0), (-1, 0), palette.header_text),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), header_size),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), body_size),
            ("TOPPADDING", (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, palette.zebra]),
        ]
    )
