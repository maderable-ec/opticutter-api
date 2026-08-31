"""The money block of a quote or an order: net subtotal, tax, total.

Pure layer (no DB or framework): cut geometry is cached by hash and is
price-agnostic, so everything commercial happens **after** ``compute()`` as a
deterministic transform, keeping the optimization cache shared across every
price level.

Business rules (decided with the user):
- **Lines are already final.** The seller picks one price level per quote and
  marks board by board which ones get it; ``price_levels.apply_price_level``
  rewrites those lines before this runs. So there is no discount adjustment
  here: the subtotal IS the sum of what the document prints.
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
    marked boards actually cost at ``price_level``. ``price_level`` is carried
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
    return {
        "price_level": price_level,
        "price_level_name": PRICE_LEVEL_NAMES.get(price_level),
        "subtotal": subtotal,
        "services_total": services_total,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "total": round(subtotal + tax_amount, 2),
    }
