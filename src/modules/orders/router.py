from datetime import date
from typing import List, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse

from src.modules.optimizations.carrier import ProformaCarrier
from src.modules.optimizations.proforma import (
    ProformaService,
    attachment_to_pdf_part,
    merge_pdfs,
    pdf_response,
)
from src.modules.orders import attachment_storage
from src.modules.orders.attachment_service import (
    AttachmentService,
    attachment_service,
)
from src.modules.orders.model import OrderStatus
from src.modules.orders.schemas import (
    AttachmentResponse,
    BandingStatusResponse,
    BandingUpdate,
    CuttingPlanResponse,
    OrderBranchUpdate,
    OrderExportResponse,
    OrderInvoiceUpdate,
    OrderResponse,
    OrderStatusUpdate,
    PieceCutResponse,
    PieceCutUpdate,
    WorkshopQueueItem,
)
from src.modules.orders.service import OrderService, order_service
from src.modules.settings.service import SettingsService, settings_service
from src.modules.users.dependencies import get_branch_scope, require_permission
from src.modules.users.model import UserModel
from src.shared.audit import staff_actor
from src.shared.pagination import PageParams
from src.shared.responses import (
    ERROR_RESPONSES,
    DataResponse,
    PaginatedResponse,
    ok,
    page,
)

router = APIRouter(prefix="/orders", tags=["orders"], responses=ERROR_RESPONSES)

# Read/proforma: admin + seller + operator. Write (create, invoice, export):
# admin + seller. State transition: admin + seller + operator (TRANSITION_ROLES
# filters by specific transition in the service). Cutting plan (view + production
# sheet): admin + seller + operator. Marking pieces: admin + operator.
_READ = Depends(require_permission("orders:read"))
_WRITE = Depends(require_permission("orders:write"))
_CUTTING = Depends(require_permission("cutting_plan"))
_WORKSHOP = Depends(require_permission("orders:workshop"))

_FORMAT_QUERY = Query(
    default="pdf",
    description="Output format: 'pdf' (file) or 'base64' (JSON)",
    pattern="^(pdf|base64)$",
)


@router.get("/", response_model=PaginatedResponse[OrderResponse], dependencies=[_READ])
def list_orders(
    status: Optional[List[OrderStatus]] = Query(
        default=None,
        description="Filter orders by one or more statuses (repeat the parameter)",
    ),
    branch_id: Optional[int] = Query(
        default=None,
        alias="branchId",
        description="Global roles only (admin/seller): narrows the listing to a "
        "branch (empty = all)",
    ),
    client_id: Optional[int] = Query(
        default=None,
        alias="clientId",
        description="Narrows the listing to one client",
    ),
    search: Optional[str] = Query(
        default=None,
        description="Search by order code or id, or by client identifier/name",
    ),
    created_from: Optional[date] = Query(
        default=None,
        alias="createdFrom",
        description="Only orders created on or after this day (UTC, inclusive)",
    ),
    created_to: Optional[date] = Query(
        default=None,
        alias="createdTo",
        description="Only orders created on or before this day (UTC, inclusive)",
    ),
    sort: Literal["oldest", "recent"] = Query(
        default="oldest",
        description="Listing order: 'oldest' first (FIFO, the workshop's view) "
        "or 'recent' first (the back office's)",
    ),
    paging: PageParams = Depends(),
    svc: OrderService = Depends(order_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Lists orders with optional filters, search, ordering and pagination.

    The operator only sees their branch's orders; global roles (admin/seller)
    see all of them (or filter with ``branchId``).
    """
    items, total = svc.list_orders(
        status=status,
        branch_scope=branch_scope,
        branch_filter=branch_id,
        limit=paging.limit,
        offset=paging.offset,
        search=search,
        client_filter=client_id,
        created_from=created_from,
        created_to=created_to,
        sort=sort,
    )
    return page(items, total, paging.limit, paging.offset)


@router.get(
    "/workshop-queue",
    response_model=DataResponse[List[WorkshopQueueItem]],
    dependencies=[_WORKSHOP],
)
def get_workshop_queue(
    svc: OrderService = Depends(order_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Shared shop-floor board (operator + bander): orders queued → cut, their branch.

    Self-sufficient card list (embeds client + board names + progress). Declared
    before ``/{order_id}`` so the parametric route doesn't capture it.
    """
    return ok(svc.list_workshop_queue(branch_scope=branch_scope))


@router.get(
    "/{order_id}", response_model=DataResponse[OrderResponse], dependencies=[_READ]
)
def get_order(
    order_id: int,
    svc: OrderService = Depends(order_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Gets an order by ID (404 if it belongs to another branch and you're not admin)."""
    return ok(svc.get_scoped_or_404(order_id, branch_scope))


@router.patch(
    "/{order_id}/status",
    response_model=DataResponse[OrderResponse],
)
def update_order_status(
    order_id: int,
    data: OrderStatusUpdate,
    svc: OrderService = Depends(order_service),
    current_user: UserModel = Depends(require_permission("orders:transition")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Transitions an order's state, validating the state machine."""
    return ok(
        svc.transition(
            order_id,
            data.status,
            actor=staff_actor(current_user),
            note=data.note,
            payment=data.payment,
            branch_scope=branch_scope,
        )
    )


@router.patch(
    "/{order_id}/banding",
    response_model=DataResponse[BandingStatusResponse],
)
def update_order_banding(
    order_id: int,
    data: BandingUpdate,
    svc: OrderService = Depends(order_service),
    current_user: UserModel = Depends(require_permission("orders:band")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Registers the start/finish of banding (track parallel to cutting).

    The bander advances ``in_progress`` (start) → ``done`` (finish) without
    touching the cutting status; runs while the order is in ``cutting``/``cut``.
    """
    return ok(
        svc.transition_banding(
            order_id,
            data.status,
            actor=staff_actor(current_user),
            branch_scope=branch_scope,
        )
    )


@router.get(
    "/{order_id}/cutting-plan",
    response_model=DataResponse[CuttingPlanResponse],
    dependencies=[_CUTTING],
)
def get_cutting_plan(
    order_id: int,
    svc: OrderService = Depends(order_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Cutting plan for the workshop view: physical boards, pieces and progress."""
    return ok(svc.get_cutting_plan(order_id, branch_scope=branch_scope))


@router.patch(
    "/{order_id}/cutting-plan/pieces/{piece_id}",
    response_model=DataResponse[PieceCutResponse],
)
def mark_piece_cut(
    order_id: int,
    piece_id: int,
    data: PieceCutUpdate,
    svc: OrderService = Depends(order_service),
    current_user: UserModel = Depends(require_permission("orders:cut")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Marks (or unmarks, ``cut=false``) a placed piece as cut.

    Only with the order in ``cutting``. Idempotent: re-marking changes nothing.
    """
    return ok(
        svc.mark_piece_cut(
            order_id,
            piece_id,
            data.cut,
            actor=staff_actor(current_user),
            branch_scope=branch_scope,
        )
    )


@router.post(
    "/{order_id}/invoice",
    response_model=DataResponse[OrderResponse],
    dependencies=[_WRITE],
)
def set_order_invoice(
    order_id: int,
    data: OrderInvoiceUpdate,
    svc: OrderService = Depends(order_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Associates the external invoice ID (stitches with the billing provider)."""
    return ok(
        svc.set_external_invoice_id(
            order_id, data.external_invoice_id, branch_scope=branch_scope
        )
    )


@router.patch(
    "/{order_id}/branch",
    response_model=DataResponse[OrderResponse],
)
def change_order_branch(
    order_id: int,
    data: OrderBranchUpdate,
    svc: OrderService = Depends(order_service),
    current_user: UserModel = Depends(require_permission("orders:write")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Reassigns the order to another branch (load rebalancing on saturation).

    Admin/seller only; allowed while the order is ``confirmed``/``queued`` (before
    the shop floor starts). Documents reprint under the new branch's letterhead.
    """
    return ok(
        svc.change_branch(
            order_id,
            data.branch_id,
            actor=staff_actor(current_user),
            note=data.note,
            branch_scope=branch_scope,
        )
    )


@router.get(
    "/{order_id}/export",
    response_model=DataResponse[OrderExportResponse],
    dependencies=[_WRITE],
)
def export_order(
    order_id: int,
    svc: OrderService = Depends(order_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Neutral billing document for the external provider (billing=boards)."""
    return ok(svc.build_export(order_id, branch_scope=branch_scope))


# Exempt from the JSON envelope: PDF file transport (StreamingResponse) and its
# base64 variant are "the file, transported", not domain JSON resources.
@router.get("/{order_id}/document", dependencies=[_READ])
def get_order_document(
    order_id: int,
    format: str = _FORMAT_QUERY,
    svc: OrderService = Depends(order_service),
    settings_svc: SettingsService = Depends(settings_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Order document (committed document, frozen prices) from the snapshot.

    Not a proforma (non-binding quote): it's the document for an already
    confirmed order, hence the header reads "ORDEN DE PEDIDO".
    """
    order = svc.get_scoped_or_404(order_id, branch_scope)
    carrier = ProformaCarrier.from_order(order, company=settings_svc.get_company())
    pdf_buffer = ProformaService.generate_proforma_pdf(carrier, title="ORDEN DE PEDIDO")
    return pdf_response(
        pdf_buffer, f"orden_pedido_{order.code or order.id}.pdf", format
    )


@router.get("/{order_id}/production-sheet", dependencies=[_CUTTING])
def get_order_production_sheet(
    order_id: int,
    format: str = _FORMAT_QUERY,
    svc: OrderService = Depends(order_service),
    settings_svc: SettingsService = Depends(settings_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Production sheet (cut list and layout, NO prices) for the workshop."""
    order = svc.get_scoped_or_404(order_id, branch_scope)
    carrier = ProformaCarrier.from_order(order, company=settings_svc.get_company())
    pdf_buffer = ProformaService.generate_production_sheet_pdf(carrier)
    return pdf_response(pdf_buffer, f"produccion_{order.code or order.id}.pdf", format)


@router.get("/{order_id}/dispatch-sheet", dependencies=[_READ])
def get_order_dispatch_sheet(
    order_id: int,
    format: str = _FORMAT_QUERY,
    svc: OrderService = Depends(order_service),
    settings_svc: SettingsService = Depends(settings_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Dispatch sheet (handover to the client): pieces with NO prices, a liability
    disclaimer and signatures. Shows the snapshot's dispatch date/responsible party."""
    order = svc.get_scoped_or_404(order_id, branch_scope)
    carrier = ProformaCarrier.from_order(order, company=settings_svc.get_company())
    pdf_buffer = ProformaService.generate_dispatch_sheet_pdf(carrier)
    return pdf_response(pdf_buffer, f"despacho_{order.code or order.id}.pdf", format)


@router.get("/{order_id}/consolidated", dependencies=[_READ])
def get_order_consolidated(
    order_id: int,
    format: str = _FORMAT_QUERY,
    svc: OrderService = Depends(order_service),
    att_svc: AttachmentService = Depends(attachment_service),
    settings_svc: SettingsService = Depends(settings_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Consolidated print packet: one PDF for the printer.

    Merges, in order: the order document (ORDEN DE PEDIDO, with the cut list and
    materials but without its embedded diagram), the cut diagram only (DIAGRAMA DE
    DESPIECE — just the gráfico, no repeated piece/board lists), the dispatch sheet,
    and every attachment (PDFs as-is, screenshots wrapped one per page).
    """
    order = svc.get_scoped_or_404(order_id, branch_scope)
    carrier = ProformaCarrier.from_order(order, company=settings_svc.get_company())
    parts = [
        ProformaService.generate_proforma_pdf(
            carrier, title="ORDEN DE PEDIDO", include_diagram=False
        ),
        ProformaService.generate_diagram_pdf(carrier),
        ProformaService.generate_dispatch_sheet_pdf(carrier),
    ]
    for att in att_svc.list_attachments(order_id, branch_scope=branch_scope):
        try:
            data = attachment_storage.read(att.stored_key)
        except OSError:
            continue  # file missing on disk: skip, still print the rest
        part = attachment_to_pdf_part(data, att.content_type)
        if part is not None:
            parts.append(part)

    merged = merge_pdfs(parts)
    return pdf_response(merged, f"consolidado_{order.code or order.id}.pdf", format)


# --------------------------------------------------------------------------- #
# Attachments (anexos): PDFs/screenshots attached while the order is still open
# (not completed/dispatched/cancelled). Upload/delete = admin+seller
# (orders:write); listing/download = anyone who reads the order (orders:read).
# --------------------------------------------------------------------------- #
@router.post(
    "/{order_id}/attachments",
    response_model=DataResponse[AttachmentResponse],
    status_code=201,
)
def add_order_attachment(
    order_id: int,
    file: UploadFile = File(..., description="PDF or image (PNG/JPEG) to attach"),
    svc: AttachmentService = Depends(attachment_service),
    current_user: UserModel = Depends(require_permission("orders:write")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Attaches a PDF or screenshot to an order (only while it's not closed)."""
    return ok(
        svc.add_attachment(
            order_id, file, actor=staff_actor(current_user), branch_scope=branch_scope
        )
    )


@router.get(
    "/{order_id}/attachments",
    response_model=DataResponse[List[AttachmentResponse]],
    dependencies=[_READ],
)
def list_order_attachments(
    order_id: int,
    svc: AttachmentService = Depends(attachment_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Lists an order's attachments (metadata only; bytes via the download route)."""
    return ok(svc.list_attachments(order_id, branch_scope=branch_scope))


def _content_disposition(filename: str, disposition: str = "inline") -> str:
    """Builds a ``Content-Disposition`` header safe for any filename.

    HTTP header values must be latin-1 encodable, but user filenames can carry
    other characters (e.g. macOS screenshots use U+202F). Emits an ASCII-only
    ``filename`` fallback plus the RFC 5987 ``filename*`` with the real UTF-8 name.
    """
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").replace('"', "")
    ascii_fallback = ascii_fallback or "archivo"
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


# Exempt from the JSON envelope: this streams the raw file bytes.
@router.get("/{order_id}/attachments/{attachment_id}", dependencies=[_READ])
def download_order_attachment(
    order_id: int,
    attachment_id: int,
    svc: AttachmentService = Depends(attachment_service),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Downloads (inline) the attachment's bytes with its original content type."""
    att = svc.get_attachment(order_id, attachment_id, branch_scope=branch_scope)
    return StreamingResponse(
        attachment_storage.open_stream(att.stored_key),
        media_type=att.content_type,
        headers={"Content-Disposition": _content_disposition(att.filename, "inline")},
    )


@router.delete("/{order_id}/attachments/{attachment_id}", status_code=204)
def delete_order_attachment(
    order_id: int,
    attachment_id: int,
    svc: AttachmentService = Depends(attachment_service),
    current_user: UserModel = Depends(require_permission("orders:write")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Removes an attachment (only while the order is not closed)."""
    svc.delete_attachment(
        order_id,
        attachment_id,
        actor=staff_actor(current_user),
        branch_scope=branch_scope,
    )
