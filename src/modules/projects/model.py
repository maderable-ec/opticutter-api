from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class ProjectStatus(str, Enum):
    """States of a designer's project."""

    # The designer is drawing it: the only state it can be edited in.
    in_progress = "in_progress"
    # Handed to the sellers: it shows in their queue, waiting to be quoted.
    ready = "ready"
    # A seller made a pre-order out of it. Set only by that creation, never by
    # ``PATCH /status``, and read-only from then on.
    quoted = "quoted"
    # Shelved by its designer. Never deleted: a pre-order may point at it.
    archived = "archived"


# The moves ``PATCH /projects/{id}/status`` makes. ``quoted`` is no one's
# target: it is the pre-order's creation that puts a project there.
PROJECT_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = {
    ProjectStatus.in_progress: frozenset({ProjectStatus.ready, ProjectStatus.archived}),
    ProjectStatus.ready: frozenset({ProjectStatus.in_progress, ProjectStatus.archived}),
    ProjectStatus.archived: frozenset({ProjectStatus.in_progress}),
    ProjectStatus.quoted: frozenset(),
}

# What a seller can quote from. ``quoted`` again too: a rejected quote is
# re-quoted from the same project, so one project may have several pre-orders.
QUOTABLE_STATUSES = frozenset({ProjectStatus.ready, ProjectStatus.quoted})

# How the messages name each state.
STATUS_LABELS = {
    ProjectStatus.in_progress: "en curso",
    ProjectStatus.ready: "listo",
    ProjectStatus.quoted: "cotizado",
    ProjectStatus.archived: "archivado",
}


class ProjectModel(TimestampMixin, AuditMixin, Base):
    """A designer's project: a cut list drawn with the optimizer, owned by them.

    ``payload`` is the optimizer's form state, opaque, with the same contract as
    ``optimization_drafts.payload``: a project in progress has half-filled rows,
    and the seller who quotes it loads it into the same wizard. The real gate is
    the pre-order's creation (complete plan, assembly, layout adjustments).

    Scoped by ``owner_id``, not by branch: the designer sees only their own and
    the seller reads them all, to quote the ready ones.
    """

    __tablename__ = "projects"
    __table_args__ = (
        # The designer's listing (their own, by status).
        Index("ix_projects_owner_status", "owner_id", "status"),
        # The sellers' queue (every ready project).
        Index("ix_projects_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    # Who it is for, as free text: the seller picks the real client when quoting.
    client_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # The key to who sees and edits it, hence its own column and NOT NULL rather
    # than ``created_by`` (nullable, ``SET NULL``). Users are never deleted.
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(
        String(32), default=ProjectStatus.in_progress.value
    )
    payload: Mapped[dict] = mapped_column(JSON)
    # Stamped every time it is handed to the sellers.
    ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    owner: Mapped["UserModel"] = relationship(  # noqa: F821
        "UserModel", foreign_keys=[owner_id]
    )
    # The pre-orders quoted from it, through ``preorders.project_id``. By name
    # only: this module never imports ``preorders``, which imports this one.
    quotes: Mapped[list["PreOrderModel"]] = relationship(  # noqa: F821
        "PreOrderModel", viewonly=True, order_by="PreOrderModel.id"
    )

    @property
    def owner_name(self) -> Optional[str]:
        """The designer's name (their e-mail when they have none)."""
        user = self.owner
        return (user.full_name or user.email) if user is not None else None
