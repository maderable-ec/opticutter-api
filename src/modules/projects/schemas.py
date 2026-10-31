from datetime import datetime
from typing import List, Optional

from pydantic import Field

from src.modules.projects.model import ProjectStatus
from src.shared.schemas import CamelModel


class ProjectCreate(CamelModel):
    """Create a project. It is born ``in_progress``, owned by whoever creates it.

    ``payload`` is opaque: the backend doesn't validate its internal shape (it
    is the optimizer's form state, defined by the frontend, as in a draft).
    """

    name: str = Field(..., min_length=1, max_length=128, description="Project name")
    client_name: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Who it is for, as free text (the seller picks the client)",
    )
    notes: Optional[str] = Field(default=None, max_length=512)
    payload: dict = Field(
        ..., description="Opaque optimizer form state (materials + pieces, as-is)"
    )


class ProjectUpdate(CamelModel):
    """Update (overwrite) a project in progress. All fields are optional."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    client_name: Optional[str] = Field(default=None, max_length=128)
    notes: Optional[str] = Field(default=None, max_length=512)
    payload: Optional[dict] = None


class ProjectStatusUpdate(CamelModel):
    """Move a project: hand it to the sellers, reopen it or archive it."""

    status: ProjectStatus = Field(
        ...,
        description=(
            "in_progress, ready or archived. `quoted` is set only by creating a "
            "pre-order from the project."
        ),
    )


class ProjectQuoteResponse(CamelModel):
    """A pre-order quoted from the project."""

    id: int
    code: Optional[str] = None
    status: str


class ProjectSummaryResponse(CamelModel):
    """Lightweight summary for the listing: ``payload`` is deliberately omitted."""

    id: int
    name: str
    client_name: Optional[str] = None
    notes: Optional[str] = None
    status: ProjectStatus
    owner_id: int = Field(..., description="The designer who owns it")
    owner_name: Optional[str] = Field(
        default=None, description="The designer's name (their e-mail if none)"
    )
    ready_at: Optional[datetime] = Field(
        default=None, description="Last time it was handed to the sellers"
    )
    # In the listing too: the sellers' queue says which projects already have a
    # quote, and links it, without opening each one.
    quotes: List[ProjectQuoteResponse] = Field(
        default_factory=list, description="The pre-orders quoted from it"
    )
    created_at: datetime
    updated_at: datetime


class ProjectResponse(ProjectSummaryResponse):
    """Project detail: the summary plus the full ``payload``."""

    payload: dict
