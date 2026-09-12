"""Shared analytics semantics: revenue states, granularity and utilities.

This is the module's backbone: every endpoint agrees on which states count as
realized/booked/lost revenue. Reuses ``OrderStatus`` and the sets already
defined in the orders module instead of repeating strings.
"""

from enum import Enum
from typing import Iterable

from src.modules.orders.model import LIVE_STATUSES, ActivityType, OrderStatus

# Realized revenue: the order reached its productive end (finished or already dispatched).
REALIZED_STATUSES = {OrderStatus.finished, OrderStatus.dispatched}

# Lost revenue: will never be charged.
LOST_STATUSES = {OrderStatus.cancelled}

# Booked pipeline: committed but not yet finished.
BOOKED_STATUSES = {
    OrderStatus.confirmed,
    OrderStatus.queued,
    OrderStatus.in_process,
}

# Pending (open, pre-production): committed but not yet in the workshop.
PENDING_STATUSES = {OrderStatus.confirmed}

# Readable label per status for breakdowns (funnel axis). User-facing copy.
# The two legacy statuses are here because the ORDER HISTORY of anything cut
# before the activities still says them; they never appear in the breakdown.
STATUS_LABELS = {
    OrderStatus.confirmed: "Confirmada",
    OrderStatus.queued: "En cola",
    OrderStatus.in_process: "En proceso",
    OrderStatus.finished: "Terminada",
    OrderStatus.dispatched: "Despachada",
    OrderStatus.cancelled: "Cancelada",
    OrderStatus.cutting: "En corte",
    OrderStatus.cut: "Cortada",
}

# The funnel axis: the statuses an order can actually be in today. Densifying
# over the whole enum instead would add two legacy rows that are always zero.
STATUS_BREAKDOWN_ORDER = LIVE_STATUSES

# --- Process stages (bottlenecks) ----------------------------------------------
# Four stages come from consecutive pairs in the status history; the three work
# stages come from ``order_activities`` (started_at → finished_at), which is
# what makes them measurable at all -- as columns, only the banding ever was.
# User-facing labels below.
STAGE_LABELS = {
    "confirm": "Confirmación → Cola",
    "queue_wait": "Espera en cola (taller)",
    "process": "En proceso",
    "cutting": "Corte",
    "banding": "Canteado",
    "additional": "Adicionales",
    "dispatch_wait": "Espera de despacho",
}

# Display order (process flow); the report then sorts by duration.
STAGE_ORDER = list(STAGE_LABELS.keys())

# Which stage each activity's own duration feeds.
ACTIVITY_STAGE = {
    ActivityType.cutting: "cutting",
    ActivityType.banding: "banding",
    ActivityType.additional: "additional",
}

# (from_status, to_status) pair from the history → named stage. The legacy pairs
# are kept so the report still measures the orders cut before the activities
# existed: back then the cut WAS a pair of statuses.
STATUS_PAIR_TO_STAGE = {
    (OrderStatus.confirmed.value, OrderStatus.queued.value): "confirm",
    (OrderStatus.queued.value, OrderStatus.in_process.value): "queue_wait",
    (OrderStatus.in_process.value, OrderStatus.finished.value): "process",
    (OrderStatus.finished.value, OrderStatus.dispatched.value): "dispatch_wait",
    # Legacy history (pre-activities).
    (OrderStatus.queued.value, OrderStatus.cutting.value): "queue_wait",
    (OrderStatus.cutting.value, OrderStatus.cut.value): "cutting",
    (OrderStatus.cut.value, OrderStatus.finished.value): "process",
}


class Granularity(str, Enum):
    """Bucket size for the time series."""

    day = "day"
    week = "week"
    month = "month"


def status_values(statuses: Iterable[OrderStatus]) -> list[str]:
    """Projects a set of statuses to their string values (for ``.in_(...)``)."""
    return [s.value for s in statuses]


def safe_div(num: float, denom: float) -> float:
    """Safe division: returns ``0.0`` if the denominator is zero."""
    return num / denom if denom else 0.0


def percentile(values: list[float], q: float) -> float:
    """``q`` percentile (0..1) via linear interpolation; ``0.0`` if there are no samples.

    Useful for spotting bottlenecks: p90 reveals the slow tail that the
    average hides (a few orders that took much longer).
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
