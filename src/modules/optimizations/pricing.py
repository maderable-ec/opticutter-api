"""Price-tier discount applied on top of an already-optimized payload.

Pure layer (no DB or framework): cut geometry is cached by hash and is
price-agnostic; the discount is applied **after** ``compute()`` as a
deterministic transform, keeping the optimization cache shared across tiers.

Business rules (decided with the user):
- **Document-level**: line items stay at list price; the discount is a single
  ``subtotal → discount_amount → total`` adjustment.
- **Only the catalog boards the seller marked**: the base is the boards whose
  ``material_key`` is in ``discounted_keys`` (``applyDiscount`` on the material
  input). **Nothing is discounted by default** — a client may negotiate the
  melamina and not the MDF, so the choice is made board by board while quoting.
  Edge banding, offcuts and manual measurements are charged at list price and
  can't be selected at all.

The selection is resolved **here** rather than baked into the payload precisely
because the payload is cached by input hash and the flag is *not* in that hash:
a checkbox must re-price the same cut plan, never re-run the search. That also
means the flag can't reach the render layer through ``materials_summary`` — it
would go stale for a whole ``OPT_RESULT_TTL_SECONDS``.
"""

from typing import Iterable, Optional


def build_pricing(
    payload: dict,
    tier: dict,
    additional_services: Optional[list] = None,
    discounted_keys: Optional[Iterable[str]] = None,
) -> dict:
    """Computes the pricing block (document-level discount) for a given ``tier``.

    ``tier`` is a resolved price tier ``{code, name, rate, ...}`` (see
    ``SettingsService.resolve_price_tier``). ``discounted_keys`` are the material
    keys the seller marked as discountable (``OptimizeRequest.discounted_material_keys``);
    empty/None means no discount at all. ``additional_services`` is the list of
    billed services (``{unit_price, quantity, ...}``); they are **not** cut geometry
    and are added on top of the total, **after** the discount (the tier discount
    applies only to the marked catalog boards). Returns a serializable dict that is
    exposed in the response and frozen into the order's snapshot/columns.
    """
    rate = float(tier.get("rate", 0.0))
    keys = set(discounted_keys or ())
    # Base = the marked catalog boards. The ``product_id`` check stays alongside
    # the selection so a key that somehow names an offcut/manual material (both
    # carry ``product_id`` None) still can't discount it. A material's half-board
    # line shares its ``material_key``, so it follows the same mark by construction.
    discount_base = round(
        sum(
            m.get("total_cost", 0.0)
            for m in payload.get("materials_summary") or []
            if m.get("product_id") is not None and m.get("material_key") in keys
        ),
        2,
    )
    boards = payload.get("total_boards_cost", 0.0)
    edge = payload.get("total_edge_banding_cost", 0.0)
    subtotal = round(boards + edge, 2)
    discount_amount = round(discount_base * rate, 2)
    # Additional services: qty × unit price, not discounted (added at the end).
    services_total = round(
        sum(
            s.get("unit_price", 0.0) * s.get("quantity", 0)
            for s in additional_services or []
        ),
        2,
    )
    total = round(subtotal - discount_amount + services_total, 2)
    return {
        "price_tier_code": tier.get("code"),
        "price_tier_name": tier.get("name"),
        "discount_rate": rate,
        "discount_base": discount_base,
        "subtotal": subtotal,
        "discount_amount": discount_amount,
        "services_total": services_total,
        "total": total,
    }
