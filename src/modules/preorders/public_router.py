"""Public client review routes: the token is the only credential.

Consumed by the Maderable frontend from the link's URL. They don't expose
internal identifiers or the client's contact details (see
``ReviewPreOrderResponse``); the breakdown and prices are recomputed live. The
client reviews the quote on-screen (no PDF download from the public link).
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Request

from src.modules.preorders.model import PreOrderModel, PreOrderStatus
from src.modules.preorders.review_service import (
    PreOrderReviewService,
    preorder_review_service,
)
from src.modules.preorders.schemas import (
    ReviewActionRequest,
    ReviewLayoutGroup,
    ReviewLineResponse,
    ReviewPieceEdges,
    ReviewPieceResponse,
    ReviewPlacedPiece,
    ReviewPreOrderResponse,
    ReviewServiceResponse,
    ReviewSheet,
)
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok

router = APIRouter(
    prefix="/public/review", tags=["public-review"], responses=ERROR_RESPONSES
)


def _client_meta(request: Request) -> dict:
    """Client IP and user-agent for action auditing."""
    forwarded = request.headers.get("x-forwarded-for")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else None)
    )
    return {"ip": ip, "user_agent": request.headers.get("user-agent")}


def _to_review_edges(edges: Optional[dict]) -> Optional[ReviewPieceEdges]:
    """Keeps only what the diagram draws; drops the catalog identifiers."""
    if not edges:
        return None
    return ReviewPieceEdges(
        sides=list(edges.get("sides") or []),
        color=edges.get("color"),
        band_type=edges.get("band_type"),
        notation=edges.get("notation"),
    )


def _to_review_layouts(payload: dict) -> List[ReviewLayoutGroup]:
    """Projects the optimization's cutting patterns for the client's diagram.

    The payload already carries ``layout_groups`` (deduplicated by pattern, so
    one entry per unique arrangement rather than per physical sheet). The
    material is resolved to its display name here so the internal
    ``material_key`` never reaches the client, and the guillotine ``cuts`` are
    dropped: saw travel is production information.
    """
    names = {
        m.get("material_key"): (m.get("product_name") or m.get("product_code"))
        for m in payload.get("materials_summary", [])
    }
    groups = []
    for group in payload.get("layout_groups", []):
        layout = group.get("layout", {})
        material = layout.get("material", {})
        pieces = layout.get("placed_pieces", [])
        groups.append(
            ReviewLayoutGroup(
                count=group.get("count", 1),
                sheet_numbers=group.get("sheet_numbers", []),
                sheet=ReviewSheet(
                    material_name=names.get(group.get("material_key")),
                    width=material.get("width", 0),
                    height=material.get("height", 0),
                    thickness=material.get("thickness", 0),
                    half_board=bool(material.get("half_board", False)),
                ),
                placed_pieces=[
                    ReviewPlacedPiece(
                        piece_id=str(p.get("piece_id", "")),
                        x=p.get("x", 0),
                        y=p.get("y", 0),
                        width=p.get("width", 0),
                        height=p.get("height", 0),
                        rotated=bool(p.get("rotated", False)),
                        original_width=p.get("original_width", 0),
                        original_height=p.get("original_height", 0),
                        edges=_to_review_edges(p.get("edges")),
                    )
                    for p in pieces
                ],
                remainders=layout.get("remainders", []),
                pieces_count=len(pieces),
            )
        )
    return groups


def _to_review_response(
    preorder: PreOrderModel, payload: dict, pricing: dict
) -> ReviewPreOrderResponse:
    """Sanitized projection of the pre-order + its recomputed optimization.

    Lines are shown at list price; the price tier discount is a single
    document-level adjustment (``pricing``).
    """
    client = preorder.client
    client_name = (
        " ".join(part for part in [client.first_name, client.last_name] if part) or None
    )
    lines = [
        ReviewLineResponse(
            product_code=m.get("product_code"),
            product_name=m.get("product_name"),
            quantity=m["count"],
            unit_price=m["cost_per_unit"],
            line_total=m["total_cost"],
        )
        for m in payload.get("materials_summary", [])
    ] + [
        ReviewLineResponse(
            product_code=e.get("product_code"),
            product_name=e.get("product_name"),
            quantity=1,
            unit_price=e["total_cost"],
            line_total=e["total_cost"],
        )
        for e in payload.get("edge_bandings_summary", [])
    ]
    services = [
        ReviewServiceResponse(
            name=s.get("name", ""),
            quantity=s.get("quantity", 0),
            unit_price=s.get("unit_price", 0.0),
            line_total=round(s.get("unit_price", 0.0) * s.get("quantity", 0), 2),
        )
        for s in preorder.additional_services or []
    ]
    pieces = [
        ReviewPieceResponse(
            label=r.get("label"),
            material_code=r.get("product_code"),
            material_name=r.get("product_name"),
            height=r["height"],
            width=r["width"],
            quantity=r["quantity"],
            edges=r.get("edge_banding"),
        )
        for r in payload.get("requirements", [])
    ]
    return ReviewPreOrderResponse(
        reference=preorder.code,
        status=PreOrderStatus(preorder.status),
        order_code=preorder.order.code if preorder.order is not None else None,
        client_note=preorder.client_note,
        notes=preorder.notes,
        client_name=client_name,
        currency="USD",
        subtotal=pricing["subtotal"],
        price_tier_name=pricing.get("price_tier_name"),
        discount_rate=pricing.get("discount_rate", 0.0),
        discount_amount=pricing.get("discount_amount", 0.0),
        services_total=pricing.get("services_total", 0.0),
        total=pricing["total"],
        total_boards_used=payload.get("total_boards_used", 0),
        total_pieces=sum(p.quantity for p in pieces),
        created_at=preorder.created_at,
        sent_at=preorder.sent_at,
        confirmed_at=preorder.confirmed_at,
        expires_at=preorder.expires_at,
        lines=lines,
        additional_services=services,
        pieces=pieces,
        layout_groups=_to_review_layouts(payload),
    )


@router.get("/{token}", response_model=DataResponse[ReviewPreOrderResponse])
def get_review(
    token: str, svc: PreOrderReviewService = Depends(preorder_review_service)
):
    """Sanitized detail of the quote associated with the token (live prices)."""
    preorder = svc.get_review(token)
    payload, _ = svc.preorders.compute_payload(preorder)
    pricing = svc.preorders.build_pricing_for(preorder, payload)
    return ok(_to_review_response(preorder, payload, pricing))


@router.post("/{token}/confirm", response_model=DataResponse[ReviewPreOrderResponse])
def confirm_review(
    token: str,
    request: Request,
    data: Optional[ReviewActionRequest] = None,
    svc: PreOrderReviewService = Depends(preorder_review_service),
):
    """The client confirms: creates the immutable Order; benign retry."""
    note = data.note if data else None
    preorder = svc.confirm(token, note=note, meta=_client_meta(request))
    payload, _ = svc.preorders.compute_payload(preorder)
    pricing = svc.preorders.build_pricing_for(preorder, payload)
    return ok(_to_review_response(preorder, payload, pricing))


@router.post("/{token}/reject", response_model=DataResponse[ReviewPreOrderResponse])
def reject_review(
    token: str,
    request: Request,
    data: Optional[ReviewActionRequest] = None,
    svc: PreOrderReviewService = Depends(preorder_review_service),
):
    """The client rejects the quote (``sent → rejected``)."""
    note = data.note if data else None
    preorder = svc.reject(token, note=note, meta=_client_meta(request))
    payload, _ = svc.preorders.compute_payload(preorder)
    pricing = svc.preorders.build_pricing_for(preorder, payload)
    return ok(_to_review_response(preorder, payload, pricing))


@router.post(
    "/{token}/request-changes", response_model=DataResponse[ReviewPreOrderResponse]
)
def request_changes_review(
    token: str,
    data: Optional[ReviewActionRequest] = None,
    svc: PreOrderReviewService = Depends(preorder_review_service),
):
    """The client requests adjustments (``sent → changes_requested``); the link stays alive."""
    note = data.note if data else None
    preorder = svc.request_changes(token, note=note)
    payload, _ = svc.preorders.compute_payload(preorder)
    pricing = svc.preorders.build_pricing_for(preorder, payload)
    return ok(_to_review_response(preorder, payload, pricing))
