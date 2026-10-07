"""The events of an order's life the listing filters by: when, and by whom.

An event is a moment the office asks about ("finished last week", "entered the
shop on Tuesday") together with the person who made it happen, so a figure
somebody disputes can be checked order by order. It is a generic filter, not an
analytics figure: nothing here has to add up to a report.

Three families, by where the moment is written down:

- a column of the order (``created_at``/``created_by``, ``dispatched_at``/
  ``dispatched_by``);
- a status history row, which carries its instant and its actor. Only real
  moves count: marking a priority or moving the branch writes a row with
  ``from == to``, and reading those as events would say an order entered the
  queue again;
- an activity row, closed with ``finished_at``/``finished_by``. The legacy
  ``cut`` rows need no branch of their own: their activity was backfilled at
  the same instant.

``paid`` is the mixed one. Its day is ``queued_at``, frozen on the FIRST entry
to the queue, so the admin rollback ``in_process -> queued`` is not a second
payment; its actor is whoever registered the payment, on the
``confirmed -> queued`` row.

When and who go into ONE predicate, so "banding closed by B" never matches an
order whose banding was closed by A and whose cut was closed by B.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import and_, exists
from sqlalchemy.sql.elements import ColumnElement

from src.modules.orders.model import (
    ActivityType,
    OrderActivityModel,
    OrderModel,
    OrderStatus,
    OrderStatusHistoryModel,
)


class OrderEvent(str, Enum):
    created = "created"
    paid = "paid"
    in_process = "in_process"
    cut_done = "cut_done"
    banding_done = "banding_done"
    additional_done = "additional_done"
    finished = "finished"
    dispatched = "dispatched"
    cancelled = "cancelled"


# The status a history row moves to for each history event. ``cutting`` is the
# legacy name of entering the shop, before ``in_process`` absorbed it.
_HISTORY_EVENTS: dict[OrderEvent, tuple[OrderStatus, ...]] = {
    OrderEvent.in_process: (OrderStatus.in_process, OrderStatus.cutting),
    OrderEvent.finished: (OrderStatus.finished,),
    OrderEvent.cancelled: (OrderStatus.cancelled,),
}

_ACTIVITY_EVENTS: dict[OrderEvent, ActivityType] = {
    OrderEvent.cut_done: ActivityType.cutting,
    OrderEvent.banding_done: ActivityType.banding,
    OrderEvent.additional_done: ActivityType.additional,
}


def _within(column, start: Optional[datetime], end: Optional[datetime]) -> list:
    """``[start, end)``, either end open."""
    conditions = []
    if start is not None:
        conditions.append(column >= start)
    if end is not None:
        conditions.append(column < end)
    return conditions


def _history_row(*conditions) -> ColumnElement[bool]:
    """EXISTS a real move (``from != to``) of the order meeting ``conditions``."""
    return exists().where(
        OrderStatusHistoryModel.order_id == OrderModel.id,
        OrderStatusHistoryModel.from_status.is_distinct_from(
            OrderStatusHistoryModel.to_status
        ),
        *conditions,
    )


def order_event_filter(
    event: OrderEvent,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    actor_id: Optional[int] = None,
) -> Optional[ColumnElement[bool]]:
    """Orders where ``event`` happened in ``[start, end)``, by ``actor_id``.

    Every argument but the event is optional; with none of them there is
    nothing to filter and the result is ``None``. The bounds are naive UTC, as
    every timestamp is stored.
    """
    if start is None and end is None and actor_id is None:
        return None

    if event is OrderEvent.created:
        conditions = _within(OrderModel.created_at, start, end)
        if actor_id is not None:
            conditions.append(OrderModel.created_by == actor_id)
    elif event is OrderEvent.dispatched:
        conditions = _within(OrderModel.dispatched_at, start, end)
        if actor_id is not None:
            conditions.append(OrderModel.dispatched_by == actor_id)
    elif event is OrderEvent.paid:
        conditions = _within(OrderModel.queued_at, start, end)
        if actor_id is not None:
            conditions.append(
                _history_row(
                    OrderStatusHistoryModel.from_status == OrderStatus.confirmed.value,
                    OrderStatusHistoryModel.to_status == OrderStatus.queued.value,
                    OrderStatusHistoryModel.actor_user_id == actor_id,
                )
            )
    elif event in _HISTORY_EVENTS:
        row = [
            OrderStatusHistoryModel.to_status.in_(
                [s.value for s in _HISTORY_EVENTS[event]]
            ),
            *_within(OrderStatusHistoryModel.created_at, start, end),
        ]
        if actor_id is not None:
            row.append(OrderStatusHistoryModel.actor_user_id == actor_id)
        conditions = [_history_row(*row)]
    else:
        row = [
            OrderActivityModel.order_id == OrderModel.id,
            OrderActivityModel.type == _ACTIVITY_EVENTS[event].value,
            OrderActivityModel.finished_at.isnot(None),
            *_within(OrderActivityModel.finished_at, start, end),
        ]
        if actor_id is not None:
            row.append(OrderActivityModel.finished_by == actor_id)
        conditions = [exists().where(*row)]
    return and_(*conditions)
