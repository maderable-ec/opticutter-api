"""Stock rules over the vendor's warehouse quantities.

The single home of the two questions this feature answers — *is this product
under its threshold?* and *is there enough for what is being quoted?* — so the
quote alert, the low-stock report and the administrators' notification cannot
drift apart by answering them three slightly different ways.

Nothing is persisted. The vendor's ``binventario`` is the source of truth for
quantities; the local catalog supplies identity (code, name, type) and the
``settings`` singleton supplies the thresholds.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.inventory.external_inventory import StockRow, fetch_stock
from src.modules.inventory.schemas import (
    LowStockItem,
    LowStockReport,
    LowStockThresholds,
    StockAlert,
    StockCheckItem,
    StockCheckResult,
)
from src.modules.products.model import ProductModel, ProductType
from src.modules.settings.service import SettingsService
from src.shared.cache import cache
from src.shared.config import config
from src.shared.database import get_db
from src.shared.exceptions import EntityNotFoundError, ExternalServiceError

logger = logging.getLogger(__name__)

FetchStock = Callable[[], List[StockRow]]

# Bumped whenever the shape below changes, so a deploy never reads a stale
# structure written by the previous build.
_CACHE_KEY = "inventory:stock:v1"

# What the two numbers of an alert are counted in. Boards are whole sheets;
# edge banding is the linear metres the quote bills. Reported rather than
# inferred downstream because the frontend writes "3 láminas" or "40 m" with it.
_UNITS = {
    ProductType.BOARD.value: "sheets",
    ProductType.EDGE_BANDING.value: "linear_m",
}


class StockService:
    """Reads the vendor's stock and applies the configured thresholds.

    ``fetch`` is injectable so the whole service can be exercised without a live
    MySQL — the same device ``sync_catalog_from_external`` uses. Resolved at
    call time rather than bound as a default argument, so a test that patches
    the module's ``fetch_stock`` reaches the instances the app builds for
    itself, not just the ones it constructs by hand.
    """

    def __init__(self, db: Session, fetch: Optional[FetchStock] = None):
        self.db = db
        self._fetch = fetch

    # -- stock map ---------------------------------------------------------- #
    def _stock_map(self) -> Dict[str, Dict[int, float]]:
        """``{external_code: {warehouse_code: quantity}}`` for every warehouse.

        Cached in Redis for ``STOCK_CACHE_TTL_SECONDS``. One read covers both
        warehouses, so this runs once per window no matter how many quotes,
        pre-order reads and order details ask in it — which matters because the
        other end of the connection is a third party's production database, not
        ours. Redis degrades on its own (``cache`` returns ``None`` and the call
        simply reads through), so a dead cache costs latency, never an answer.
        """
        cached = cache.get_json(_CACHE_KEY)
        if cached is not None:
            # JSON object keys are strings; the warehouse code is an int.
            return {
                code: {int(bod): qty for bod, qty in by_bod.items()}
                for code, by_bod in cached.items()
            }

        read = self._fetch or fetch_stock
        stock: Dict[str, Dict[int, float]] = {}
        for row in read():
            stock.setdefault(row.external_code, {})[row.warehouse_code] = row.quantity

        if config.STOCK_CACHE_TTL_SECONDS > 0:
            cache.set_json(_CACHE_KEY, stock, ttl=config.STOCK_CACHE_TTL_SECONDS)
        return stock

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _available(
        stock: Dict[str, Dict[int, float]],
        external_code: Optional[str],
        warehouse_code: int,
    ) -> Optional[float]:
        """Quantity in that warehouse, or ``None`` when it cannot be known.

        Two different absences, deliberately told apart. A product with no
        ``external_code`` was created by hand and never synced, so the vendor
        has no opinion about it — reporting it as zero would put a permanent
        false alarm in the report. An article the vendor *does* know but has no
        row for in that warehouse has genuinely never been stocked there, and
        zero is the honest answer (68 of Macas' articles are in exactly that
        state today).
        """
        if not external_code:
            return None
        by_warehouse = stock.get(external_code)
        if by_warehouse is None:
            return 0.0
        return by_warehouse.get(warehouse_code, 0.0)

    def _branch(self, branch_id: int) -> Optional[BranchModel]:
        return self.db.get(BranchModel, branch_id)

    # -- the quote alert ---------------------------------------------------- #
    def check(
        self, branch_id: int, items: Sequence[StockCheckItem]
    ) -> StockCheckResult:
        """Alerts for the products a quote consumes, in one branch.

        A product is reported when it is under its threshold, when the branch
        holds less than the quote needs, or both — the two reasons ride
        separately so the copy can say which one fired.
        """
        branch = self._branch(branch_id)
        if branch is None:
            raise EntityNotFoundError("Sucursal", branch_id)
        if branch.warehouse_code is None:
            return StockCheckResult(branch=branch, checked=False)
        if not items:
            return StockCheckResult(branch=branch, checked=True, alerts=[])

        try:
            stock = self._stock_map()
        except ExternalServiceError:
            logger.warning("Stock check skipped: external inventory unavailable")
            return StockCheckResult(branch=branch, checked=False)

        thresholds = SettingsService(self.db).get_stock_thresholds()
        required_by_product: Dict[int, float] = {}
        for item in items:
            required_by_product[item.product_id] = (
                required_by_product.get(item.product_id, 0.0) + item.quantity
            )

        products = self._products(required_by_product.keys())
        alerts: List[StockAlert] = []
        for product in products:
            threshold = thresholds.get(product.type)
            if threshold is None:
                continue  # a type with no configured floor is not alerted on
            available = self._available(
                stock, product.external_code, branch.warehouse_code
            )
            if available is None:
                continue
            required = required_by_product[product.id]
            below = available < threshold
            insufficient = available < required
            if not (below or insufficient):
                continue
            alerts.append(
                StockAlert(
                    product_id=product.id,
                    product_code=product.code,
                    product_name=product.name,
                    type=product.type,
                    unit=_UNITS.get(product.type, "unit"),
                    required=required,
                    available=available,
                    threshold=threshold,
                    below_threshold=below,
                    insufficient=insufficient,
                )
            )

        # Worst first: what the branch cannot cover at all, then the lowest.
        alerts.sort(key=lambda a: (not a.insufficient, a.available, a.product_id))
        return StockCheckResult(branch=branch, checked=True, alerts=alerts)

    def _products(self, product_ids: Iterable[int]) -> List[ProductModel]:
        ids = list(product_ids)
        if not ids:
            return []
        return self.db.query(ProductModel).filter(ProductModel.id.in_(ids)).all()

    # -- the order's notification ------------------------------------------- #
    def alerts_for_order(self, order) -> List[StockAlert]:
        """Low-stock alerts for an order's billed catalog lines. Never raises.

        Best-effort **by design**: this runs after a committed transition, to
        decide whether the office gets a notification. A warehouse that is not
        configured, a vendor database that is down or any other surprise has to
        come back as "nothing to say" — a notification is an accelerator, and it
        must not be able to undo a payment that is already recorded.

        ``order.lines`` is the right source and not the snapshot: it already
        carries one row per billed product with ``quantity`` in the units this
        service compares (sheets for boards, linear metres for edge banding),
        and ``product_id`` is NULL for exactly the materials that are not
        stocked anywhere — a client's offcut or a manual measurement.
        """
        try:
            items = [
                StockCheckItem(product_id=line.product_id, quantity=line.quantity)
                for line in order.lines
                if line.product_id is not None and line.quantity > 0
            ]
            if not items:
                return []
            result = self.check(order.branch_id, items)
            return result.alerts if result.checked else []
        except Exception:  # pragma: no cover - defensive, see docstring
            logger.exception("Low-stock lookup failed for order %s", order.id)
            return []

    # -- the report --------------------------------------------------------- #
    def low_stock(
        self, branch_id: Optional[int] = None, product_type: Optional[str] = None
    ) -> LowStockReport:
        """Every catalog product under its threshold, per branch.

        ``branch_id`` omitted means every active branch that has a warehouse
        configured. A branch without one contributes nothing rather than a page
        of zeros: the vendor's system simply has no inventory for it.
        """
        thresholds = SettingsService(self.db).get_stock_thresholds()
        report_thresholds = LowStockThresholds(
            board=thresholds[ProductType.BOARD.value],
            edge_banding=thresholds[ProductType.EDGE_BANDING.value],
        )

        branches = self._report_branches(branch_id)
        if not branches:
            return LowStockReport(checked=True, thresholds=report_thresholds, items=[])

        try:
            stock = self._stock_map()
        except ExternalServiceError:
            logger.warning("Low-stock report skipped: external inventory unavailable")
            return LowStockReport(checked=False, thresholds=report_thresholds, items=[])

        query = self.db.query(ProductModel).filter(
            ProductModel.is_active.is_(True),
            ProductModel.external_code.isnot(None),
        )
        if product_type is not None:
            query = query.filter(ProductModel.type == product_type)
        products = query.order_by(ProductModel.name).all()

        items: List[LowStockItem] = []
        for branch in branches:
            for product in products:
                threshold = thresholds.get(product.type)
                if threshold is None:
                    continue
                available = self._available(
                    stock, product.external_code, branch.warehouse_code
                )
                if available is None or available >= threshold:
                    continue
                items.append(
                    LowStockItem(
                        product_id=product.id,
                        code=product.code,
                        name=product.name,
                        type=product.type,
                        subtype=(product.attributes or {}).get("subtype"),
                        unit=_UNITS.get(product.type, "unit"),
                        branch=branch,
                        available=available,
                        threshold=threshold,
                    )
                )

        # Branch, then type, then the emptiest first: the buying order.
        items.sort(key=lambda i: (i.branch.id, i.type, i.available, i.product_id))
        return LowStockReport(checked=True, thresholds=report_thresholds, items=items)

    def _report_branches(self, branch_id: Optional[int]) -> List[BranchModel]:
        query = self.db.query(BranchModel).filter(
            BranchModel.warehouse_code.isnot(None)
        )
        if branch_id is not None:
            query = query.filter(BranchModel.id == branch_id)
        else:
            query = query.filter(BranchModel.is_active.is_(True))
        return query.order_by(BranchModel.id).all()


def stock_service(db: Session = Depends(get_db)) -> StockService:
    """``StockService`` provider for route injection."""
    return StockService(db)
