from typing import Optional

from pydantic import Field

from src.shared.schemas import CamelModel


class BranchBase(CamelModel):
    code: str = Field(
        ..., min_length=1, max_length=32, description="Unique branch code"
    )
    name: str = Field(..., min_length=1, max_length=128, description="Branch name")
    address: Optional[str] = Field(
        None, max_length=256, description="Address (document letterhead)"
    )
    phone: Optional[str] = Field(None, max_length=32, description="Contact phone")
    print_labels_enabled: bool = Field(
        True, description="The branch's shop has a thermal label printer"
    )
    print_consolidated_enabled: bool = Field(
        True, description="The branch's shop has a sheet printer (consolidated packet)"
    )


class BranchCreate(BranchBase):
    """Create a branch."""


class BranchUpdate(CamelModel):
    """Partial update of a branch."""

    code: Optional[str] = Field(None, min_length=1, max_length=32)
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = Field(None, max_length=256)
    phone: Optional[str] = Field(None, max_length=32)
    is_active: Optional[bool] = Field(
        None, description="Active/inactive (logical deactivation)"
    )
    print_labels_enabled: Optional[bool] = None
    print_consolidated_enabled: Optional[bool] = None


class BranchResponse(BranchBase):
    """Public representation of a branch."""

    id: int = Field(..., description="Branch ID")
    is_active: bool = Field(..., description="Active/inactive")


class BranchRefResponse(CamelModel):
    """Compact branch reference to embed in other responses.

    Consumed by orders, pre-orders and drafts so the frontend can show which
    branch each document belongs to without dragging along the letterhead
    contact details (``address``/``phone``) on every row of a listing.

    It does carry the printing switches: the order detail dispatches the
    consolidated packet on completion, and gating that trigger from the embedded
    ref saves the frontend a separate branch request.
    """

    id: int = Field(..., description="Branch ID")
    code: str = Field(..., description="Unique branch code")
    name: str = Field(..., description="Branch name")
    print_labels_enabled: bool = Field(
        ..., description="The branch's shop has a thermal label printer"
    )
    print_consolidated_enabled: bool = Field(
        ..., description="The branch's shop has a sheet printer"
    )
