"""What the shop's machines wear out: the saw blade, the scoring blade, the glue.

Business wants to know how many of each the shop goes through and how much
work each one gives. The work is already on record (the sheets cut, the
bandings closed); the one thing nobody registered is the CHANGE -- a blade
swapped, the glue pot loaded. That is all this table holds: one row per
change, and everything else is read off the production between two of them.

One saw and one edge bander per branch, so a change belongs to a branch and
nobody picks a machine.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.modules.users.enums import UserRole
from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class ConsumableType(str, Enum):
    saw_blade = "saw_blade"
    scoring_blade = "scoring_blade"
    glue = "glue"


# The two discs of the saw: they wear with the metres it cuts.
BLADE_TYPES = frozenset({ConsumableType.saw_blade, ConsumableType.scoring_blade})

CONSUMABLE_LABELS: dict[ConsumableType, str] = {
    ConsumableType.saw_blade: "sierra",
    ConsumableType.scoring_blade: "incisor",
    ConsumableType.glue: "goma",
}

# Who registers each one, the second layer under ``consumables:register`` (the
# same scheme as ``ACTIVITY_ROLES``): the operator runs the saw, the bander the
# edge bander.
CONSUMABLE_ROLES: dict[ConsumableType, tuple[UserRole, ...]] = {
    ConsumableType.saw_blade: (UserRole.ADMIN, UserRole.OPERATOR),
    ConsumableType.scoring_blade: (UserRole.ADMIN, UserRole.OPERATOR),
    ConsumableType.glue: (UserRole.ADMIN, UserRole.BANDER),
}


class BladeCondition(str, Enum):
    """What went onto the saw: a blade out of the box, or one back from sharpening."""

    new = "new"
    sharpened = "sharpened"


class ChangeReason(str, Enum):
    """Why a consumable came off EARLY (see ``rules.change_gate``).

    A blade that broke at 80 m is not a blade that wore out at 80 m: without
    the reason the first one drags the average of every other.
    """

    damaged = "damaged"
    poor_cut = "poor_cut"
    ran_out = "ran_out"
    pot_cleaning = "pot_cleaning"
    other = "other"


CHANGE_REASON_LABELS: dict[ChangeReason, str] = {
    ChangeReason.damaged: "Se dañó",
    ChangeReason.poor_cut: "No corta bien",
    ChangeReason.ran_out: "Se acabó",
    ChangeReason.pot_cleaning: "Limpieza de la olla",
    ChangeReason.other: "Otro motivo",
}

_BLADE_REASONS = (ChangeReason.damaged, ChangeReason.poor_cut, ChangeReason.other)

# The reasons each consumable offers, in the order the dialog shows them.
REASONS_BY_TYPE: dict[ConsumableType, tuple[ChangeReason, ...]] = {
    ConsumableType.saw_blade: _BLADE_REASONS,
    ConsumableType.scoring_blade: _BLADE_REASONS,
    ConsumableType.glue: (
        ChangeReason.ran_out,
        ChangeReason.pot_cleaning,
        ChangeReason.other,
    ),
}


class ConsumableChangeModel(TimestampMixin, AuditMixin, Base):
    """One change of a consumable: a blade mounted, or the glue pot loaded.

    The row OPENS a life -- the stretch up to the next change of the same type
    in the same branch -- and says what went in (``condition`` for a blade,
    ``quantity`` in kg for the glue). The metres that life gave are never
    stored: they are read off the production, so a piece marked late or a
    change voided moves them on its own.

    Never deleted: a mistake is voided, with who and why, like a payment.
    """

    __tablename__ = "consumable_changes"
    __table_args__ = (
        # Every read is "this branch's changes of this type, in order".
        Index(
            "ix_consumable_changes_branch_type_at", "branch_id", "type", "changed_at"
        ),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branches.id"))
    type: Mapped[str] = mapped_column(String(16))
    # When it happened, which is not always when it was registered: the admin
    # can enter a change somebody forgot.
    changed_at: Mapped[datetime] = mapped_column(DateTime)
    changed_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    changed_by_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Blades only.
    condition: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Glue only: the kg that went into the pot. Frozen here, so retuning the
    # standard load in settings never rewrites what was loaded.
    quantity: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    voided_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    voided_by_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    void_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
