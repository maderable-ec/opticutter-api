"""Stock alert and low-stock report contracts.

Both surfaces answer the same two questions about one (product, branch) pair —
is it under the threshold, and is there enough for what is being quoted — so
they share the vocabulary: ``available`` is what the warehouse holds,
``threshold`` is the configured floor for that product type, and ``unit`` says
what the two numbers are counted in, which is NOT the same for a board and a
tapacanto.
"""

from typing import List, Optional

from pydantic import Field

from src.modules.branches.schemas import BranchRefResponse
from src.shared.schemas import CamelModel


class StockCheckItem(CamelModel):
    """One catalog product the quote consumes, and how much of it."""

    product_id: int = Field(..., description="Catalog product id")
    quantity: float = Field(
        ...,
        ge=0,
        description=(
            "How much the quote needs: sheets for a board, linear metres for "
            "edge banding (the same units the order lines are billed in)"
        ),
    )


class StockCheckRequest(CamelModel):
    """Stock question for one branch.

    Materials outside the catalog (offcuts, manual measurements) have no
    ``productId`` and simply aren't sent: they are not stocked anywhere.
    """

    branch_id: int = Field(..., description="Branch whose warehouse is consulted")
    items: List[StockCheckItem] = Field(default_factory=list)


class StockAlert(CamelModel):
    """One product worth telling the seller about, with both reasons."""

    product_id: int
    product_code: Optional[str] = None
    product_name: Optional[str] = None
    type: str = Field(..., description="Product type (board / edge_banding)")
    unit: str = Field(..., description="sheets | linear_m")
    required: float = Field(..., description="What the quote consumes")
    available: float = Field(..., description="What the branch's warehouse holds")
    threshold: float = Field(..., description="Configured floor for this type")
    below_threshold: bool
    insufficient: bool = Field(
        ..., description="The branch holds less than the quote needs"
    )


class StockCheckResult(CamelModel):
    """Answer to a stock question.

    ``checked`` is false — with an empty ``alerts`` — whenever the question
    could not be answered at all: the branch has no warehouse configured, or the
    vendor's database did not respond. That is deliberately NOT an error: the
    alert is informational and must never stand between a seller and a quote.
    """

    branch: Optional[BranchRefResponse] = None
    checked: bool
    alerts: List[StockAlert] = Field(default_factory=list)


class LowStockItem(CamelModel):
    """One product below its threshold in one branch."""

    product_id: int
    code: str
    name: str
    type: str
    # Material subtype off the product's ``attributes`` (MDP, Plywood, Canto
    # Solido, ...). Optional because it is: a board registered before the field
    # existed, or one whose vendor ``tip`` the sync did not recognise, has none.
    subtype: Optional[str] = None
    unit: str
    branch: BranchRefResponse
    available: float
    threshold: float


class LowStockThresholds(CamelModel):
    """The thresholds the report was measured against."""

    board: float
    edge_banding: float


class LowStockReport(CamelModel):
    """Every product under its threshold, across the branches consulted.

    ``checked`` mirrors ``StockCheckResult``: false means the vendor's database
    was unreachable, not that everything is well stocked.
    """

    checked: bool
    thresholds: LowStockThresholds
    items: List[LowStockItem] = Field(default_factory=list)
