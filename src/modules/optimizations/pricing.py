"""The money block of a quote or an order: net subtotal, tax, total.

Pure layer (no DB or framework): cut geometry is cached by hash and is
price-agnostic, so everything commercial happens **after** ``compute()`` as a
deterministic transform, keeping the optimization cache shared across every
price level.

Business rules (decided with the user):
- **Lines are already final.** The seller picks one price level per quote and
  marks board by board which ones get it; ``price_levels.apply_price_level``
  rewrites those lines before this runs. So there is no discount *adjustment*
  here: the subtotal IS the sum of what the document prints. ``discount_amount``
  and ``list_subtotal`` are reported beside it and change no total — they answer
  "how much off list is this", which is the one thing a different unit price per
  product hides from the client.
- **Everything is net, once.** The catalog stores the vendor's net prices, and
  additional services — which staff registers **tax-included**, because that is
  how their price list is written — are converted to net here. One tax line then
  covers the whole document, which is how an invoice reads.

Why services are converted rather than added after the tax: they are taxable
like everything else, and leaving them outside the base would make "Subtotal +
IVA" not add up to the total on the page.
"""

from typing import Optional

from src.modules.optimizations.schemas import PRICE_LEVEL_NAMES


def build_pricing(
    payload: dict,
    price_level: int = 1,
    additional_services: Optional[list] = None,
    tax_rate: float = 0.0,
) -> dict:
    """Computes the pricing block for an already-leveled payload.

    ``payload`` must have been through ``apply_price_level`` (the optimization
    service does it inside ``compute``), so ``total_boards_cost`` is what the
    marked boards actually cost at ``price_level`` and ``price_level_discount``
    is how far below list that landed. ``price_level`` is carried
    only to name it on the document and freeze it on the order. Returns a
    serializable dict exposed in the response and frozen into the order's
    snapshot/columns.
    """
    boards = payload.get("total_boards_cost", 0.0)
    edge = payload.get("total_edge_banding_cost", 0.0)
    # Per line, not over the sum, so the services table on the PDF adds up to
    # the number that reaches the subtotal.
    services_total = round(
        sum(
            round(s.get("unit_price", 0.0) * s.get("quantity", 0) / (1 + tax_rate), 2)
            for s in additional_services or []
        ),
        2,
    )
    subtotal = round(boards + edge + services_total, 2)
    tax_amount = round(subtotal * tax_rate, 2)
    # Measured by ``price_levels.level_discount`` over the finished plan and
    # carried on the payload, so every surface reporting money gets it from the
    # one place that computed it. The reference is the whole document at level 1
    # — edge banding and services are in it because neither ever takes a level,
    # which is also what makes ``list_subtotal - discount_amount == subtotal``
    # true by construction rather than by arithmetic done twice.
    discount_amount = payload.get("price_level_discount", 0.0)
    return {
        "price_level": price_level,
        "price_level_name": PRICE_LEVEL_NAMES.get(price_level),
        "discount_amount": discount_amount,
        "list_subtotal": round(subtotal + discount_amount, 2),
        "subtotal": subtotal,
        "services_total": services_total,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "total": round(subtotal + tax_amount, 2),
    }
