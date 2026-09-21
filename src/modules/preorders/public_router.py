"""Public client review routes: the token is the only credential.

Consumed by the Maderable frontend from the link's URL. They don't expose
internal identifiers or the client's contact details (see
``ReviewPreOrderResponse``); the breakdown and prices are recomputed live. The
client reviews the quote on-screen (no PDF download from the public link).
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Request

from src.modules.optimizations.labels import edge_notation
from src.modules.optimizations.service import CCW_ROTATION
from src.modules.preorders.model import PreOrderModel, PreOrderStatus
from src.modules.preorders.review_service import (
    PreOrderReviewService,
    preorder_review_service,
)
from src.modules.preorders.schemas import (
    ReviewActionRequest,
    ReviewCutPieceEdges,
    ReviewLayoutGroup,
    ReviewLineResponse,
    ReviewPieceEdges,
    ReviewPieceResponse,
    ReviewPlacedPiece,
    ReviewPreOrderResponse,
    ReviewServiceResponse,
    ReviewSheet,
    ReviewSpecialEdge,
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


def _edge_banding_index(payload: dict) -> dict:
    """Tapacanto display attributes by ``product_id``, from the banding summary.

    The summary is already in the payload and is keyed by the same
    ``product_id`` both piece shapes carry, so naming a piece's tape costs a
    dict lookup and no extra query. It is read here rather than injected
    upstream in ``_dump_requirement`` on purpose: optimization results are
    cached and the hash covers the *inputs*, so a quote already in Redis would
    otherwise stay nameless until it expired.
    """
    return {
        e.get("product_id"): e
        for e in payload.get("edge_bandings_summary", [])
        if e.get("product_id") is not None
    }


def _to_review_special(
    special: Optional[list], eb_by_id: dict
) -> List[ReviewSpecialEdge]:
    """The cantos especiales of a piece, named the way its auto tape is.

    Takes either shape: the requirement's (``side`` nominal) and the placed
    piece's (``side`` geometric + ``nominal_side``).
    """
    return [
        ReviewSpecialEdge(
            side=e["side"],
            nominal_side=e.get("nominal_side") or e["side"],
            band_type=e.get("band_type"),
            product_name=(eb_by_id.get(e.get("product_id")) or {}).get("product_name"),
            color=(eb_by_id.get(e.get("product_id")) or {}).get("color"),
        )
        for e in special or []
    ]


def _to_review_cut_edges(
    requirement: dict, eb_by_id: dict
) -> Optional[ReviewCutPieceEdges]:
    """Projects a cut-list piece's banding; drops the catalog identifiers.

    No rotation here, unlike ``_to_review_edges``: these sides come from the
    requirement's own ``EdgeBandingSpec`` and are nominal already. A canto
    especial adds its side to ``sides`` and takes it from the auto tape, whose
    name is dropped when it no longer bands any side.
    """
    edges = requirement.get("edge_banding") or {}
    special = requirement.get("special_edges") or []
    if not edges and not special:
        return None
    taken = {e["side"] for e in special}
    auto = [s for s in edges.get("sides") or [] if s not in taken]
    summary = (eb_by_id.get(edges.get("product_id")) or {}) if auto else {}
    return ReviewCutPieceEdges(
        sides=auto + [e["side"] for e in special if e["side"] not in auto],
        band_type=edges.get("band_type") if auto else None,
        product_name=summary.get("product_name"),
        color=summary.get("color"),
        notation=edge_notation(
            edges.get("sides") or [],
            edges.get("band_type"),
            edges.get("alias"),
            special,
        )
        or None,
        special=_to_review_special(special, eb_by_id),
    )


def _to_review_edges(
    edges: Optional[dict], rotated: bool, eb_by_id: dict
) -> Optional[ReviewPieceEdges]:
    """Keeps only what the diagram draws; drops the catalog identifiers.

    ``nominal_sides`` is recovered by undoing the rotation when the payload
    predates the field: results are cached for ``OPT_RESULT_TTL_SECONDS`` and the
    hash covers the *inputs*, so a quote already in Redis keeps its old shape and
    would otherwise leave the client's diagram without it for days.

    ``product_name`` is joined in the same way the cut list does it, so the two
    surfaces name the same tape — the diagram's own dict carries only ``code``.
    """
    if not edges:
        return None
    geo = list(edges.get("sides") or [])
    nominal = edges.get("nominal_sides")
    if nominal is None:
        nominal = [CCW_ROTATION[s] for s in geo] if rotated else list(geo)
    summary = eb_by_id.get(edges.get("product_id")) or {}
    return ReviewPieceEdges(
        sides=geo,
        nominal_sides=list(nominal),
        color=edges.get("color"),
        band_type=edges.get("band_type"),
        notation=edges.get("notation"),
        product_name=summary.get("product_name"),
        special=_to_review_special(edges.get("special"), eb_by_id),
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
    eb_by_id = _edge_banding_index(payload)
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
                        edges=_to_review_edges(
                            p.get("edges"), bool(p.get("rotated", False)), eb_by_id
                        ),
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

    Lines are already at the price level the seller chose (the boards they
    marked); the tax is the single document-level addition (``pricing``).
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
    eb_by_id = _edge_banding_index(payload)
    pieces = [
        ReviewPieceResponse(
            label=r.get("label"),
            material_code=r.get("product_code"),
            material_name=r.get("product_name"),
            height=r["height"],
            width=r["width"],
            quantity=r["quantity"],
            edges=_to_review_cut_edges(r, eb_by_id),
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
        price_level_name=pricing.get("price_level_name"),
        discount_amount=pricing.get("discount_amount", 0.0),
        list_subtotal=pricing.get("list_subtotal", 0.0),
        services_total=pricing.get("services_total", 0.0),
        tax_rate=pricing.get("tax_rate", 0.0),
        tax_amount=pricing.get("tax_amount", 0.0),
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
