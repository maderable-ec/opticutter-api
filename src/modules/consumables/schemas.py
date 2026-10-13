from datetime import datetime, timezone
from typing import Optional

from pydantic import Field, field_validator

from src.modules.consumables.model import BladeCondition, ChangeReason, ConsumableType
from src.modules.consumables.rules import Gate
from src.shared.schemas import CamelModel


class ConsumableChangeCreate(CamelModel):
    """A blade mounted, or the glue pot loaded.

    ``condition`` is required for a blade and ``quantity`` (kg) belongs to the
    glue, where it defaults to the standard load. ``reason`` is required when
    the change comes early (``rules.change_gate``). ``branchId`` is the admin's
    to send: the shop floor registers on its own branch. ``changedAt`` enters a
    change that happened before now, and is the admin's alone.
    """

    type: ConsumableType
    branch_id: Optional[int] = None
    condition: Optional[BladeCondition] = None
    quantity: Optional[float] = Field(None, gt=0, le=50)
    reason: Optional[ChangeReason] = None
    note: Optional[str] = Field(None, max_length=255)
    changed_at: Optional[datetime] = None

    @field_validator("changed_at")
    @classmethod
    def _naive_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        # Every timestamp here is naive UTC; the client sends its own with ``Z``.
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class ConsumableChangeVoid(CamelModel):
    """Why a change is taken back. Not asked of whoever undoes their own."""

    reason: Optional[str] = Field(None, max_length=255)


class ConsumableChangeResponse(CamelModel):
    id: int
    branch_id: int
    type: ConsumableType
    changed_at: datetime
    changed_by_label: Optional[str] = None
    condition: Optional[BladeCondition] = None
    quantity: Optional[float] = None
    reason: Optional[ChangeReason] = None
    note: Optional[str] = None


class ConsumableStatus(CamelModel):
    """One consumable of a branch as it stands: what is on the machine and what
    it has done since it went on."""

    type: ConsumableType
    # What registering a change right now would meet.
    gate: Gate
    # Null until the first change is registered: there is nothing to count from.
    last_change: Optional[ConsumableChangeResponse] = None
    # Since the last change. Blades: metres cut and boards processed (the
    # sheets whose last piece was marked since). Glue: metres banded and
    # bandings closed. Null without a last change.
    meters: Optional[float] = None
    boards: Optional[float] = None
    orders: Optional[int] = None
    worked_hours: Optional[float] = None
    # Whether the viewer can take the last change back on their own.
    can_undo: bool = False
    # Glue only: the kg the control starts at.
    standard_quantity: Optional[float] = None


class BranchConsumables(CamelModel):
    """The consumables of one branch the viewer may register."""

    branch_id: int
    branch_name: str
    items: list[ConsumableStatus]


# ----------------------------------------------------------------------- report
class ConsumableLife(CamelModel):
    """What one change put to work, up to the change that replaced it."""

    # The change that opened it: the one to void if it was a mistake.
    change_id: int
    branch_id: int
    branch_name: str
    type: ConsumableType
    started_at: datetime
    started_by_label: Optional[str] = None
    condition: Optional[BladeCondition] = None
    quantity: Optional[float] = None
    # Null while it is the one in use.
    ended_at: Optional[datetime] = None
    ended_by_label: Optional[str] = None
    # Why it came off, when it came off early.
    end_reason: Optional[ChangeReason] = None
    end_note: Optional[str] = None
    meters: float
    boards: Optional[float] = None
    orders: Optional[int] = None
    worked_hours: float
    in_use: bool


class ConditionYield(CamelModel):
    """The lives of one blade condition closed in range, and what they gave."""

    lives: int = 0
    avg_meters: Optional[float] = None
    avg_boards: Optional[float] = None


class ConsumableSummary(CamelModel):
    """One consumable of one branch over the range.

    ``changes`` are the units that went IN during the range. The yield is read
    off the lives that CLOSED in it -- a life is dated by its end, like every
    figure of the statistics -- leaving out the ones that came off damaged
    (``damaged_lives``): a blade that broke says nothing of how long one lasts.
    """

    type: ConsumableType
    changes: int = 0
    # Blades: how many of those were new, how many back from sharpening.
    new_count: Optional[int] = None
    sharpened_count: Optional[int] = None
    # Glue: the kg loaded in the range.
    quantity: Optional[float] = None
    closed_lives: int = 0
    damaged_lives: int = 0
    avg_meters: Optional[float] = None
    avg_boards: Optional[float] = None
    new: Optional[ConditionYield] = None
    sharpened: Optional[ConditionYield] = None
    # Glue: metres banded per kg, over the loads used up in the range.
    meters_per_kg: Optional[float] = None
    # The one on the machine today: a snapshot, not a figure of the range.
    current: Optional[ConsumableLife] = None


class BranchConsumableReport(CamelModel):
    branch_id: int
    branch_name: str
    items: list[ConsumableSummary]


class ConsumableReport(CamelModel):
    """Consumption and yield per branch, and the lives behind the figures.

    ``lives``: every life closed in the range plus the ones in use, newest
    first.
    """

    branches: list[BranchConsumableReport]
    lives: list[ConsumableLife]
