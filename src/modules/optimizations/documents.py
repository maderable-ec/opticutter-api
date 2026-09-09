import base64
import io
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Tuple, Union
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
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from src.modules.optimizations.carrier import DocumentCarrier
from src.modules.optimizations.labels import edge_banding_notation
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


# There is no themed palette object any more: the order emits ONE document and it
# is branded, so the constants above ARE the palette. The indirection existed for
# the monochrome sibling that died with the production sheet, half its fields had
# no reader left, and the rest of the file had gone back to naming the constants
# directly anyway. The B/W cut diagram keeps its own greys in ``visualization``.

PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT_MARGIN = RIGHT_MARGIN = 0.5 * inch
CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

TOP_MARGIN = 0.4 * inch
BOTTOM_MARGIN = 0.45 * inch

# Typography and spacing of the ORDEN DE PEDIDO, gathered here rather than left
# inline in each builder: the document has to carry the piece list, the money, the
# disclaimer and the signatures on as few sheets as possible, and how tight it
# runs is one decision, retuned by looking at a printed sheet. Everything below is
# in points.
FOOTER_BASELINE = 0.35 * inch  # running footer, stamped on the merged packet

BODY_SIZE = 8  # table cells
HEADER_SIZE = 9  # table header row
CELL_LEADING = 9.5  # wrapped cells (Paragraph inside a table)
ROW_PAD = 2  # above and below every table row
CLIENT_SIZE = 8.5  # the client block, which is read at a glance
CLIENT_PAD = 1.5
TOTALS_SIZE = 9  # totals box and FORMA DE PAGO
TOTALS_PAD = 2.5
SECTION_SIZE = 10.5  # section titles
SECTION_LEADING = 12  # explicit: Heading2 would hand down ~17.2 for a 13pt letter
BLOCK_SPACER = 0.07 * inch  # between one section and the next
# The one gap that is deliberately NOT tight: the disclaimer is what the client
# is signing under, and running the signature lines up against it reads as one
# block of small print.
SIGNATURE_GAP = 0.3 * inch

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

# Delivery disclaimer, printed above the signatures: by signing it the client
# accepts the goods as delivered in good order and releases the company from later
# claims.
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


@lru_cache(maxsize=None)
def _asset(path: Path) -> ImageReader:
    """The letterhead assets, decoded once per process.

    They are immutable files shipped with the package, and the watermark alone was
    being re-read and re-decoded on every page of every document.
    """
    return ImageReader(str(path))


def _scaled_image(path: Path, width: float) -> Image:
    """Image scaled to ``width`` while preserving its aspect ratio."""
    img_width, img_height = _asset(path).getSize()
    height = width * img_height / img_width
    return Image(str(path), width=width, height=height)


def _draw_watermark(cnv, page_width: float, page_height: float) -> None:
    """Faint centered watermark (drawn underneath the content)."""
    reader = _asset(WATERMARK_PATH)
    img_width, img_height = reader.getSize()
    wm_width = 3.8 * inch
    wm_height = wm_width * img_height / img_width
    cnv.drawImage(
        reader,
        (page_width - wm_width) / 2,
        (page_height - wm_height) / 2,
        width=wm_width,
        height=wm_height,
        mask="auto",
    )


def _draw_footer_accent(cnv, page_width: float) -> None:
    """Angular orange band with a black notch on the bottom edge (letterhead style)."""
    band_h = 12
    slant = 20
    x0 = page_width * 0.52

    cnv.setFillColor(BRAND_BLACK)
    notch = cnv.beginPath()
    notch.moveTo(x0 - 44, 0)
    notch.lineTo(x0 + 6, 0)
    notch.lineTo(x0 + 6 + slant, band_h)
    notch.lineTo(x0 - 44 + slant, band_h)
    notch.close()
    cnv.drawPath(notch, fill=1, stroke=0)

    cnv.setFillColor(BRAND_ORANGE)
    band = cnv.beginPath()
    band.moveTo(x0, 0)
    band.lineTo(page_width, 0)
    band.lineTo(page_width, band_h)
    band.lineTo(x0 + slant, band_h)
    band.close()
    cnv.drawPath(band, fill=1, stroke=0)


def _draw_page_decoration(cnv, doc) -> None:
    """Watermark and footer accent on every sheet of the ORDEN DE PEDIDO.

    The footer *text* is not drawn here: it is stamped on the merged packet by
    ``stamp_packet_footer``, which is the only place that knows the real page
    count. A document that numbered its own pages restarted at 1 halfway through
    the printout, once for the order and again for the diagram.
    """
    cnv.saveState()
    _draw_watermark(cnv, PAGE_WIDTH, PAGE_HEIGHT)
    _draw_footer_accent(cnv, PAGE_WIDTH)
    cnv.restoreState()


class _CutterDoc(BaseDocTemplate):
    """A4 portrait document for the ORDEN DE PEDIDO's lists, with compact margins
    to save paper.

    One page template, on purpose. It used to register a landscape sibling too,
    but no story ever queued it (that takes a ``NextPageTemplate``, which this
    module does not even import): the cut diagrams travel as their own pages,
    drawn straight onto a canvas by ``generate_diagram_pdf``. A document that
    genuinely mixed orientations would need the pair back — and every footer
    position read off the *active* template rather than the page size.
    """

    def __init__(self, buffer: io.BytesIO, on_page):
        super().__init__(
            buffer,
            pagesize=A4,
            topMargin=TOP_MARGIN,
            bottomMargin=BOTTOM_MARGIN,
            leftMargin=LEFT_MARGIN,
            rightMargin=RIGHT_MARGIN,
        )
        self.addPageTemplates(
            [
                PageTemplate(
                    id="portrait",
                    pagesize=A4,
                    onPage=on_page,
                    frames=[
                        Frame(
                            LEFT_MARGIN,
                            BOTTOM_MARGIN,
                            CONTENT_WIDTH,
                            PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN,
                            id="portrait_frame",
                        )
                    ],
                )
            ]
        )


class DocumentService:
    @staticmethod
    def generate_order_document_pdf(carrier: DocumentCarrier) -> io.BytesIO:
        """The order's ONLY document: client, pieces, priced materials, the money,
        and the delivery block the client signs on handover.

        It carries no diagram of its own: the cut layout travels as its own
        landscape pages inside ``build_order_packet``, which is what the endpoint
        and the print queue both serve. The dispatch sheet used to repeat the
        piece list, the payment block and the letterhead just to carry the last
        three blocks here; it is gone.
        """
        buffer = io.BytesIO()
        doc = _CutterDoc(buffer, _draw_page_decoration)

        styles = getSampleStyleSheet()
        heading_style = _heading_style(styles)
        cell_style = _cell_style(styles)

        story = []
        story.extend(DocumentService._build_header(carrier, styles))
        story.append(Spacer(1, BLOCK_SPACER))

        story.extend(_section("INFORMACIÓN DEL CLIENTE", heading_style))
        story.append(DocumentService._build_client_table(carrier))
        story.append(DocumentService._dispatch_line(carrier, styles))
        story.append(Spacer(1, BLOCK_SPACER))

        story.extend(_section("DETALLE DE REQUERIMIENTOS", heading_style))
        story.append(DocumentService._build_requirements_table(carrier, cell_style))
        story.append(Spacer(1, BLOCK_SPACER))

        # Omitted outright when nothing is billed — a job cut entirely on the
        # client's material would otherwise print a "RESUMEN DE MATERIALES"
        # heading over an empty table, immediately above the block that does list
        # what was cut.
        billable = _billable_material_rows(carrier)
        edge_bandings = carrier.edge_bandings_summary or []
        if billable or edge_bandings:
            story.extend(_section("RESUMEN DE MATERIALES", heading_style))
            story.append(
                DocumentService._build_materials_table(
                    billable, edge_bandings, cell_style
                )
            )
            story.append(Spacer(1, BLOCK_SPACER))

        # The client's own retazos: listed so the document says what was cut, but
        # never priced. Putting them in the table above would fill a Subtotal
        # column with $0.00 rows that read as an error — what the shop actually
        # charges for this job is the cutting and the banding, further down.
        client_material = _client_material_rows(carrier)
        if client_material:
            story.extend(_section("MATERIAL DEL CLIENTE", heading_style))
            story.append(
                DocumentService._build_client_material_table(
                    client_material, cell_style
                )
            )
            story.append(Spacer(1, BLOCK_SPACER))

        if carrier.additional_services:
            story.extend(_section("SERVICIOS ADICIONALES", heading_style))
            story.append(DocumentService._build_services_table(carrier, cell_style))
            story.append(Spacer(1, BLOCK_SPACER))

        story.append(DocumentService._build_totals_table(carrier))

        payment_block = DocumentService._payment_section(carrier, heading_style)
        if payment_block:
            story.append(Spacer(1, BLOCK_SPACER))
            story.extend(payment_block)

        story.append(Spacer(1, BLOCK_SPACER))
        story.append(
            Paragraph(
                "Valores en USD. Los precios no incluyen IVA; "
                "el impuesto se detalla en el total.",
                ParagraphStyle(
                    "Note",
                    parent=styles["Normal"],
                    fontSize=7.5,
                    leading=9,
                    textColor=colors.grey,
                    alignment=TA_LEFT,
                ),
            )
        )

        # The handover, in the order it is filled in: what was agreed, what the
        # client accepts by signing, and the signatures themselves.
        story.append(Spacer(1, BLOCK_SPACER))
        story.extend(_section("DESCARGO DE RESPONSABILIDAD", heading_style))
        story.append(
            Paragraph(
                DISPATCH_DISCLAIMER,
                ParagraphStyle(
                    "Disclaimer",
                    parent=styles["Normal"],
                    fontSize=7,
                    leading=8.5,
                    textColor=colors.grey,
                    alignment=TA_JUSTIFY,
                ),
            )
        )
        story.append(
            KeepTogether(
                [
                    Spacer(1, SIGNATURE_GAP),
                    DocumentService._build_signature_block(styles),
                ]
            )
        )

        doc.build(story)
        buffer.seek(0)
        return buffer

    @staticmethod
    def _dispatch_line(carrier: DocumentCarrier, styles) -> Paragraph:
        """Delivery date and who handed the goods over, on one line.

        Printed on every order, not only on a dispatched one: the shop prints this
        document when the cutting is done, which is *before* anyone delivers
        anything, so the two fields come out as rules to fill in by hand and get
        signed at the counter. It used to fall back to ``datetime.now()``, which
        stamped today's date as the dispatch date on an order nobody had
        dispatched — a made-up fact on a document the client signs.
        """
        blank = "_" * 18
        date = (
            carrier.dispatch_date.strftime("%d/%m/%Y")
            if carrier.dispatch_date
            else blank
        )
        return Paragraph(
            f"<b>Fecha de despacho:</b> {date}"
            f" &nbsp;&nbsp; <b>Despachado por:</b> "
            f"{escape(carrier.dispatched_by_label) if carrier.dispatched_by_label else blank}",
            ParagraphStyle(
                "DispatchMeta",
                parent=styles["Normal"],
                fontSize=CLIENT_SIZE,
                leading=CLIENT_SIZE + 2,
                textColor=TEXT_GREY,
                alignment=TA_LEFT,
                spaceBefore=4,
            ),
        )

    @staticmethod
    def generate_diagram_pdf(carrier: DocumentCarrier) -> io.BytesIO:
        """Cutting diagram only (the *gráfico*), B/W, WITHOUT the piece/board lists.

        Used by the order packet: the cut list, boards and edge banding already
        appear in the ORDEN DE PEDIDO, so here we print just the visual layout.
        One pattern per landscape sheet, and nothing else on the sheet — no
        header, no section title, not even a footer (the packet stamps the running
        one later, being the only layer that knows the page count).

        Drawn straight onto a ``canvas``: with one image per page and no flowing
        content, the platypus document/frame/page-break machinery bought nothing
        and forced every diagram through a PNG round trip. Going to the canvas
        also means one board is rendered, drawn and released at a time, instead of
        holding every ~12 MB image alive until the build.
        """
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=LANDSCAPE_SIZE)
        for group, board_name in _diagram_pages(carrier):
            image = VisualizationService.render_layout(group, board_name=board_name)
            img_w, img_h = image.size
            # The landscape frame is wider (770pt vs 523pt) but *shorter* (522pt
            # vs 769pt) than a portrait one, so both bounds are applied or a tall
            # board overflows.
            draw_width = LAND_CONTENT_WIDTH
            draw_height = draw_width * (img_h / img_w)
            if draw_height > LAND_FRAME_HEIGHT:
                draw_height = LAND_FRAME_HEIGHT
                draw_width = draw_height * (img_w / img_h)
            pdf.drawImage(
                ImageReader(image),
                LEFT_MARGIN + (LAND_CONTENT_WIDTH - draw_width) / 2,
                LAND_HEIGHT - TOP_MARGIN - FRAME_PADDING - draw_height,
                width=draw_width,
                height=draw_height,
                mask="auto",
            )
            pdf.showPage()
        pdf.save()
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
                    ("TOPPADDING", (0, 0), (-1, 0), 5),
                    ("TOPPADDING", (0, 1), (-1, 1), 26),
                ]
            )
        )
        return table

    @staticmethod
    def _build_header(carrier: DocumentCarrier, styles) -> List:
        """MADERABLE letterhead: logo + contact, black rule and title bar."""
        logo = _scaled_image(LOGO_PATH, 1.9 * inch)
        logo.hAlign = "LEFT"

        header_table = Table(
            [[logo, DocumentService._build_contact_block(carrier, styles)]],
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
                    Paragraph("ORDEN DE PEDIDO", title_style),
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
    def _build_contact_block(carrier: DocumentCarrier, styles) -> Table:
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
    def _build_client_table(carrier: DocumentCarrier) -> Table:
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
                    ("FONTSIZE", (0, 0), (-1, -1), CLIENT_SIZE),
                    ("LEADING", (0, 0), (-1, -1), CLIENT_SIZE + 2),
                    ("TEXTCOLOR", (0, 0), (-1, -1), TEXT_GREY),
                    ("BACKGROUND", (0, 0), (0, -1), ZEBRA_GREY),
                    ("TOPPADDING", (0, 0), (-1, -1), CLIENT_PAD),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), CLIENT_PAD),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return client_table

    @staticmethod
    def _build_requirements_table(carrier: DocumentCarrier, cell_style) -> Table:
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
        req_table.setStyle(_data_table_style())
        return req_table

    @staticmethod
    def _build_materials_table(
        boards: List[dict], edge_bandings: List[dict], cell_style
    ) -> Table:
        """Single materials summary: boards (quantity in units) and edge banding
        (quantity in meters) in one table with code, description, quantity, unit
        price and subtotal. Spans the full content width.

        Takes the rows rather than the carrier — like
        ``_build_client_material_table`` — because the caller has to select them
        anyway to decide whether the section is printed at all, and it is the
        caller's guard that makes an empty table unreachable here.

        Everything here is billed. The client's own retazos are rendered apart by
        ``_build_client_material_table``; they have no price by definition, and a
        priced table whose column adds up to zero invites exactly the question
        the seller does not want to answer."""
        mat_data = [["Código", "Descripción", "Cantidad", "P. Unit.", "Subtotal"]]

        for entry in boards:
            mat_data.append(
                [
                    entry.get("product_code") or "N/A",
                    Paragraph(
                        _material_label(entry, entry.get("product_code") or "N/A"),
                        cell_style,
                    ),
                    f"{entry.get('count', 0)} u",
                    f"${entry.get('cost_per_unit', 0):.2f}",
                    f"${entry.get('total_cost', 0):.2f}",
                ]
            )
        for entry in edge_bandings:
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
        mat_table.setStyle(_data_table_style())
        return mat_table

    @staticmethod
    def _build_services_table(carrier: DocumentCarrier, cell_style) -> Table:
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
        table.setStyle(_data_table_style())
        return table

    @staticmethod
    def _build_client_material_table(entries: List[dict], cell_style) -> Table:
        """The client's own material: what was cut on, without a price.

        Its own shape (``Descripción``, dimensions, thickness, sheets) rather
        than the priced table's, because it answers a different question — which
        material this work ran on — and deliberately carries no money column.
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
                        _material_label(entry, "Material del cliente"),
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
        table.setStyle(_data_table_style())
        return table

    @staticmethod
    def _build_totals_table(carrier: DocumentCarrier) -> Table:
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
    def _payment_section(carrier: DocumentCarrier, heading_style) -> list:
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


def _diagram_pages(carrier: DocumentCarrier) -> Iterator[Tuple[dict, Optional[str]]]:
    """Every cutting pattern to print, each with the name of the board it is cut on.

    Uses the persisted groups; recomputes them for old optimizations saved before
    the ``layout_groups`` field existed.
    """
    layouts = carrier.layouts
    if not (isinstance(layouts, list) and layouts):
        return

    groups = carrier.layout_groups
    if not (isinstance(groups, list) and groups):
        groups = group_layouts(layouts)

    names = _board_names(carrier)
    for group in groups:
        material = (group.get("layout") or group).get("material") or {}
        key = (material.get("material_key"), bool(material.get("half_board", False)))
        yield group, names.get(key)


def stamp_packet_footer(merged: io.BytesIO, reference: str) -> io.BytesIO:
    """Stamps the running footer on every page of the merged packet.

    The packet is printed and handed over as ONE body, so its pages are numbered
    once, end to end — including the annexes. Each document used to number its
    own, so a printout read "Página 1" for the order and "Página 1" again for the
    first diagram; and only reportlab knew how to draw a footer, which is exactly
    the layer that cannot know how many pages follow it.

    The order number rides here too: a sheet that gets separated from the rest on
    the shop floor still says which order it belongs to.
    """
    reader = PdfReader(merged)
    total = len(reader.pages)
    stamped = PdfWriter()
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    for number, page in enumerate(reader.pages, 1):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        overlay = io.BytesIO()
        pdf = canvas.Canvas(overlay, pagesize=(width, height))
        pdf.setFont("Helvetica", 8)
        pdf.setFillColor(colors.grey)
        pdf.drawString(
            LEFT_MARGIN, FOOTER_BASELINE, f"Generado el {generated} · N° {reference}"
        )
        pdf.drawRightString(
            width - RIGHT_MARGIN, FOOTER_BASELINE, f"Página {number} de {total}"
        )
        pdf.save()
        overlay.seek(0)
        page.merge_page(PdfReader(overlay).pages[0])
        stamped.add_page(page)
    out = io.BytesIO()
    stamped.write(out)
    out.seek(0)
    return out


def build_order_packet(
    carrier: DocumentCarrier, annexes: Iterable[Tuple[bytes, str]]
) -> io.BytesIO:
    """The order's whole paperwork as ONE pdf: document, diagram and annexes.

    This is what ``GET /orders/{id}/document`` serves and what the print queue
    spools, from one implementation: the two used to be the same twenty-eight
    lines copied, which is how they drift.

    ``annexes`` are ``(bytes, content_type)`` pairs, already read from storage by
    the caller — the render layer stays DB- and disk-free. One that cannot be
    parsed is skipped rather than breaking the packet.
    """
    parts = [
        DocumentService.generate_order_document_pdf(carrier),
        # A quote with no patterns contributes nothing: a canvas saved without a
        # single ``showPage`` is a zero-page pdf, so the merge simply skips it.
        DocumentService.generate_diagram_pdf(carrier),
    ]
    for data, content_type in annexes:
        part = attachment_to_pdf_part(data, content_type)
        if part is not None:
            parts.append(part)
    return stamp_packet_footer(merge_pdfs(parts), carrier.reference)


def merge_pdfs(buffers: List[io.BytesIO]) -> io.BytesIO:
    """Concatenates every page of each PDF buffer into a single PDF.

    Used by the order packet to stitch the ORDEN DE PEDIDO, the cut diagram and
    the attachment pages into one file.
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
    it merges cleanly into the order packet like any other PDF page.
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
    corrupt annex is skipped instead of breaking the whole order packet.
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


def _reference_lines(carrier: DocumentCarrier, meta_style: ParagraphStyle) -> List:
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
        fontSize=SECTION_SIZE,
        # Explicit: Heading2 hands down a leading sized for its own 14pt letter,
        # which reserved ~17pt of height per title across seven sections.
        leading=SECTION_LEADING,
        textColor=BRAND_BLACK,
        spaceAfter=1,
        spaceBefore=3,
        fontName="Helvetica-Bold",
    )


def _cell_style(styles) -> ParagraphStyle:
    return ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontSize=BODY_SIZE,
        leading=CELL_LEADING,
        textColor=TEXT_GREY,
        alignment=TA_LEFT,
    )


def _plain_material_label(entry: dict, fallback: str) -> str:
    """The material's printed name, flagged when the sheet is cut untrimmed.

    Every surface that names a board goes through here, because the shop squares
    a board by reflex: a plan that deliberately uses the full sheet has to say so
    on the paper the operator holds, not only in the quote that priced it. Sits
    next to the "(medio tablero)" suffix the summary already builds, and stays a
    suffix rather than a column — neither table has room for one.
    """
    name = entry.get("product_name") or fallback
    return name if not entry.get("skip_trim") else f"{name} · sin refilar"


def _material_label(entry: dict, fallback: str) -> str:
    """``_plain_material_label`` for a reportlab ``Paragraph``: the mark greyed.

    Only the table cells get the markup. The diagram is drawn with Pillow, which
    has no mini-HTML and would letter the ``<font>`` tag onto the sheet.
    """
    label = _plain_material_label(entry, fallback)
    if not entry.get("skip_trim"):
        return label
    return label.replace(
        " · sin refilar", " <font color='#6B7280'>· sin refilar</font>"
    )


def _board_names(carrier: DocumentCarrier) -> dict:
    """``(material_key, half_board)`` → the board's name for its diagram header.

    Keyed on the pair and not on the key alone because ``build_materials_summary``
    splits a half board into its own line, with "(medio tablero)" already in the
    name — the same board cut both ways is two entries and two diagram headers.
    """
    return {
        (entry.get("material_key"), bool(entry.get("half_board", False))): (
            _plain_material_label(entry, entry.get("product_code") or "")
        )
        for entry in carrier.materials_summary or []
    }


def _client_material_rows(carrier: DocumentCarrier) -> List[dict]:
    """Summary lines for material the client brought in (never billed)."""
    return [
        entry
        for entry in carrier.materials_summary or []
        if entry.get("source") == MaterialSource.client_offcut.value
    ]


def _billable_material_rows(carrier: DocumentCarrier) -> List[dict]:
    """Summary lines that belong in the priced table."""
    return [
        entry
        for entry in carrier.materials_summary or []
        if entry.get("source") != MaterialSource.client_offcut.value
    ]


def _section(title: str, heading_style) -> List:
    """Section title with a colored rule underneath."""
    return [
        Paragraph(title, heading_style),
        HRFlowable(
            width="100%",
            thickness=1.2,
            color=BRAND_CORAL,
            spaceBefore=1,
            spaceAfter=3,
        ),
    ]


def _totals_table(rows: List[List[str]]) -> Table:
    """Highlighted totals box (key on the left, value on the right)."""
    table = Table(rows, colWidths=[CONTENT_WIDTH - 2.0 * inch, 2.0 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_CORAL),
                ("BOX", (0, 0), (-1, -1), 1, BRAND_CORAL),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#F5C9C3")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), TOTALS_SIZE),
                ("TEXTCOLOR", (0, 0), (-1, -1), BRAND_BLACK),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), TOTALS_PAD),
                ("BOTTOMPADDING", (0, 0), (-1, -1), TOTALS_PAD),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    return table


def _data_table_style() -> TableStyle:
    """Common style for data tables: accent header + zebra rows."""
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_CORAL),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), HEADER_SIZE),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), BODY_SIZE),
            ("TOPPADDING", (0, 0), (-1, -1), ROW_PAD),
            ("BOTTOMPADDING", (0, 0), (-1, -1), ROW_PAD),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA_GREY]),
        ]
    )
