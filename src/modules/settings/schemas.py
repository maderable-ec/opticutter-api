from typing import List, Optional

from pydantic import Field

from src.shared.schemas import CamelModel


# --- Cutting parameters --------------------------------------------------------
class CuttingSettingsResponse(CamelModel):
    """Current cutting parameters (mm) that feed the optimizer."""

    kerf: float = Field(..., ge=0, description="Saw blade width (kerf) in mm")
    top_trim: float = Field(..., ge=0, description="Top trim in mm")
    bottom_trim: float = Field(..., ge=0, description="Bottom trim in mm")
    left_trim: float = Field(..., ge=0, description="Left trim in mm")
    right_trim: float = Field(..., ge=0, description="Right trim in mm")
    edge_banding_waste_factor: float = Field(
        ..., ge=0, description="Edge banding waste over net length (0.10 = +10%)"
    )
    half_board_markup_pct: float = Field(
        ...,
        ge=0,
        description="Markup over price/2 when billing a half board (0.10 = +10%)",
    )


class CuttingSettingsUpdate(CamelModel):
    """Partial update of the cutting parameters."""

    kerf: Optional[float] = Field(None, ge=0)
    top_trim: Optional[float] = Field(None, ge=0)
    bottom_trim: Optional[float] = Field(None, ge=0)
    left_trim: Optional[float] = Field(None, ge=0)
    right_trim: Optional[float] = Field(None, ge=0)
    edge_banding_waste_factor: Optional[float] = Field(None, ge=0)
    half_board_markup_pct: Optional[float] = Field(None, ge=0)


# --- Pre-orders (mutable quote) -------------------------------------------------
class PreOrderSettingsResponse(CamelModel):
    """Current pre-order config: validity period and open-orders cap per client."""

    preorder_validity_days: int = Field(
        ..., ge=1, description="Validity period of a pre-order (quote), in days"
    )
    max_open_preorders_per_client: int = Field(
        ..., ge=1, description="Cap on open pre-orders per client (anti-abuse)"
    )


class PreOrderSettingsUpdate(CamelModel):
    """Partial update of the pre-order config."""

    preorder_validity_days: Optional[int] = Field(None, ge=1)
    max_open_preorders_per_client: Optional[int] = Field(None, ge=1)


# --- Taxes ----------------------------------------------------------------------
class TaxSettingsResponse(CamelModel):
    """Current sales tax rate applied to every quote and order."""

    tax_rate: float = Field(..., ge=0, le=1, description="Sales tax rate (0.15 = 15%)")


class TaxSettingsUpdate(CamelModel):
    """Partial update of the tax settings.

    Catalog prices are stored net, so this rate is what produces every total.
    Changing it only affects quotes computed from here on: each order freezes
    the rate it was billed at.
    """

    tax_rate: Optional[float] = Field(None, ge=0, le=1)


# --- Low-stock thresholds ---------------------------------------------------------
class StockSettingsResponse(CamelModel):
    """Thresholds below which a product counts as low on stock.

    One per product type because the UNITS are not the same: the vendor counts
    boards in whole SHEETS and edge banding in LINEAR METRES, so a single
    number could never mean anything for both.
    """

    board: float = Field(..., ge=0, description="Boards: sheets in the branch")
    edge_banding: float = Field(
        ..., ge=0, description="Edge banding: linear metres in the branch"
    )


class StockSettingsUpdate(CamelModel):
    """Partial update of the low-stock thresholds.

    Feeds both the quote alert and the low-stock report, so raising one here
    quiets (or opens) the two at once.
    """

    board: Optional[float] = Field(None, ge=0)
    edge_banding: Optional[float] = Field(None, ge=0)


# --- Company data -----------------------------------------------------------------
class Branch(CamelModel):
    """A branch shown on the document letterhead."""

    name: str = Field(..., min_length=1, max_length=128)
    address: str = Field(..., min_length=1, max_length=256)


class CompanySettingsResponse(CamelModel):
    """Current company data (document letterhead)."""

    name: str = Field(..., max_length=128)
    tagline: str = Field(..., max_length=256)
    email: str = Field(..., max_length=128)
    phone: str = Field(..., max_length=128)
    branches: List[Branch] = Field(default_factory=list)


class CompanySettingsUpdate(CamelModel):
    """Partial update of the company data."""

    name: Optional[str] = Field(None, max_length=128)
    tagline: Optional[str] = Field(None, max_length=256)
    email: Optional[str] = Field(None, max_length=128)
    phone: Optional[str] = Field(None, max_length=128)
    branches: Optional[List[Branch]] = None
