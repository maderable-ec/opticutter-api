"""Catalog price level applied on top of an already-optimized payload.

Pure layer (no DB, no framework, no ``src.cutting``): the cut geometry is cached
by hash and the level is **not** in that hash, so — exactly like ``whole_boards``
— it is applied **after** ``compute()`` as a deterministic transform, keeping one
cache entry per cut plan no matter which level the seller is quoting.

Business rule (decided with the user): the vendor's inventory publishes three
sale prices per article (``ven``/``pv2``/``pv3``, all net of tax). The seller
picks ONE level per quote, and marks board by board which ones get it
(``applyPriceLevel``); everything unmarked — plus every edge banding, offcut and
manual measurement — is billed at the list price. So the level is not a
percentage off a base: it is a different unit price, per product, and the
documents print it directly instead of a "Descuento" line.

**The half board is the one exception, and it is deliberate**: a medio tablero is
always billed off the LIST price (half the sheet plus its markup), whatever level
the rest of the quote is on. The level is a concession on the whole plank; the
half already carries its own markup because the shop keeps the other half, so
letting the level through would rebate the same board twice — and the shop sells
the half precisely to the client who is not buying enough to have earned a level.
A half the client takes whole (``whole_boards``) is NOT covered by this: once
promoted it is a full sheet delivered and billed as one, so it gets the level.

**Why this cannot resolve the level up front**, in ``MaterialResolver``: the
resolved ``cost_per_unit`` feeds the search AND the cache hash, so flipping a
checkbox would re-run a job that costs seconds to half a minute on a real quote
— with the client sitting at the counter. The geometry is price-scale invariant
anyway (one material per pool, so scaling its bin costs cannot change which
partition wins), which is what makes re-pricing the finished plan exact rather
than an approximation.

**Why nothing goes stale**: ``pv2``/``pv3`` are deliberately NOT denormalized
into the payload. The caller reads them off the freshly resolved materials on
every request (cache hit included — they are needed for the hash), so a catalog
edit is visible immediately and the hash needs no salt for them, unlike the edge
banding attributes that *are* baked into the cached payload.
"""

from typing import Dict, List, Optional

from src.modules.optimizations.patterns import group_layouts
from src.modules.optimizations.schemas import MaterialSource
from src.modules.optimizations.summary import build_materials_summary


def price_at_level(
    list_price: float,
    price_2: Optional[float],
    price_3: Optional[float],
    level: int,
) -> float:
    """Unit price at ``level`` (1-3), falling back to the list price.

    ``None`` means the vendor never loaded that level for this article (it comes
    through as ``0.000000`` and the sync stores it as ``None``, reporting it as a
    warning). Falling back to the list price is both the safe answer — a board
    billed at $0 would be catastrophic — and the truthful one: this design simply
    has no reduced price at that level.
    """
    if level == 2 and price_2 is not None:
        return price_2
    if level == 3 and price_3 is not None:
        return price_3
    return list_price


def half_board_price(unit_price: float, half_board_markup_pct: float) -> float:
    """Price of half a sheet: half the board plus its markup.

    Always measured over the LIST price. ``OptimizationService._half_spec`` is
    its one caller and it builds the bin before any level exists, which is what
    makes the price of a half board a single formula over a single input — and
    why neither pass below has to recompute it.
    """
    return round(unit_price / 2.0 * (1 + half_board_markup_pct), 2)


def apply_price_level(payload: dict, level_prices: Dict[str, float]) -> dict:
    """Re-prices the marked catalog boards at the seller's chosen level.

    ``level_prices`` maps a material key to the unit price to bill it at (the
    caller resolves the level and the per-board marks; see ``price_at_level``).
    Returns the payload **unchanged (same object)** when nothing moves — the
    common case, since SIFAC publishes the same number in ``ven``/``pv2``/``pv3``
    for more than half the catalog — and never mutates the input otherwise.

    Half boards are skipped: they keep the list price they were cut at (see the
    module docstring). The markup is therefore not a parameter here — this pass
    never computes a half board's price, it only declines to touch it.
    """
    if not level_prices:
        return payload

    materials = payload.get("materials") or []
    # Only the keys whose unit price actually changes. Filtering on the payload's
    # own ``source`` (not on the caller's promise) keeps an offcut or a manual
    # measurement out of this even if a key somehow named one.
    changed: Dict[str, float] = {}
    for material in materials:
        key = material.get("material_key")
        if key not in level_prices:
            continue
        if material.get("source") != MaterialSource.catalog.value:
            continue
        new_price = level_prices[key]
        if new_price == material.get("cost_per_unit"):
            continue
        changed[key] = new_price

    if not changed:
        return payload

    # The resolved materials DO move to the level, even though a half board does
    # not: this is the whole sheet's price, and ``whole_boards._promote`` reads it
    # to bill a half the client decided to take whole — as a full board, at the
    # level. It is also what the next pass compares against.
    new_materials = [
        (
            {**m, "cost_per_unit": changed[m["material_key"]]}
            if m.get("material_key") in changed
            else m
        )
        for m in materials
    ]

    layouts: List[dict] = []
    cost_delta = 0.0
    for layout in payload.get("layouts") or []:
        material = layout.get("material") or {}
        key = material.get("material_key")
        if key not in changed:
            layouts.append(layout)
            continue
        if material.get("half_board"):
            # Billed off the list price at every level. It already carries the
            # ``cost_per_unit`` ``_half_spec`` gave it — half the LIST price plus
            # the markup — so the sheet is carried over untouched, by reference.
            layouts.append(layout)
            continue
        unit = changed[key]
        cost_delta += unit - material.get("cost_per_unit", 0.0)
        # ``placed_pieces``/``cuts``/``remainders`` carry over by reference:
        # re-pricing must be incapable of moving a piece, and sharing the lists
        # makes that structural.
        layouts.append({**layout, "material": {**material, "cost_per_unit": unit}})

    result = dict(payload)
    result["materials"] = new_materials
    result["layouts"] = layouts
    # A delta, not a re-sum, for the same reason as ``whole_boards``: it keeps
    # this pass independent of how the base total was composed. Safe because only
    # catalog materials are re-priced, so every sheet this touches is one the
    # base already counted at its list price.
    result["total_boards_cost"] = round(
        payload.get("total_boards_cost", 0.0) + cost_delta, 2
    )
    result["materials_summary"] = build_materials_summary(layouts, new_materials)
    # Rebuilt for the same reason ``whole_boards`` rebuilds it: after the cache's
    # JSON round trip ``layout_groups[i]["layout"]`` is a separate copy of
    # ``layouts[i]``, so leaving it behind would hand the render layer a sheet
    # still carrying the list price. Signatures ignore cost, so the pattern ids
    # and sheet numbers come back identical.
    result["layout_groups"] = group_layouts(layouts)
    return result


def level_discount(payload: dict, list_prices: Dict[str, float]) -> float:
    """How much the quote is discounted against the list price (level 1).

    Informative only: the lines are already final — the level is a different
    unit price per product, not a percentage off a base — so this changes no
    total. It exists because the seller has to be able to say *how much* the
    client is getting off, which is the one thing the level's design hides.

    ``list_prices`` maps a material key to its level-1 unit price. The caller
    builds it from the resolved materials, for the marked catalog boards only,
    and **that restriction is the whole filter**: a client retazo, a manual
    measurement and an unmarked board are never in it, so they can't contribute
    — the same discipline as ``apply_price_level``'s ``source`` check, done at
    the one point that still knows what a material is.

    Measured over the FINAL payload, not inside ``apply_price_level``: the
    ``cost_delta`` that one accumulates is the discount *before*
    ``whole_boards.apply_whole_boards`` runs, and the promotion re-bills a half
    sheet as the whole board at the **level's** price. That is the one way a
    half board ever contributes here, and only the finished plan says so.
    """
    if not list_prices:
        return 0.0

    discount = 0.0
    for layout in payload.get("layouts") or []:
        material = layout.get("material") or {}
        # A half board is billed off the list price, so it discounts nothing.
        # Skipped explicitly rather than left to subtract to zero: what the seller
        # is owed a number for is what the client saves, and a half saves nothing.
        if material.get("half_board"):
            continue
        list_price = list_prices.get(material.get("material_key"))
        if list_price is None:
            continue
        discount += list_price - material.get("cost_per_unit", 0.0)
    return round(discount, 2)
