"""The listing's event predicates (``orders/order_events.py``), compiled, no database.

Which rows each one reads is what matters here; that they select the right
orders is ``tests/test_orders_event_filter.py``.
"""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.modules.orders.model import OrderModel
from src.modules.orders.order_events import OrderEvent, order_event_filter

_START, _END = datetime(2026, 6, 1, 5), datetime(2026, 6, 8, 5)


def _sql(event, start=_START, end=_END, actor_id=7) -> str:
    condition = order_event_filter(event, start, end, actor_id)
    query = select(OrderModel.id).where(condition)
    return str(query.compile(dialect=postgresql.dialect()))


def test_nothing_to_filter_is_none():
    assert all(order_event_filter(event) is None for event in OrderEvent)


@pytest.mark.parametrize(
    "event,reads",
    [
        (OrderEvent.created, ["orders.created_at", "orders.created_by"]),
        (OrderEvent.dispatched, ["orders.dispatched_at", "orders.dispatched_by"]),
        (
            OrderEvent.paid,
            ["orders.queued_at", "order_status_history.actor_user_id"],
        ),
        (OrderEvent.in_process, ["order_status_history.created_at"]),
        (OrderEvent.finished, ["order_status_history.created_at"]),
        (OrderEvent.cancelled, ["order_status_history.created_at"]),
        (OrderEvent.cut_done, ["order_activities.finished_by"]),
        (OrderEvent.banding_done, ["order_activities.finished_by"]),
        (OrderEvent.additional_done, ["order_activities.finished_by"]),
    ],
)
def test_each_event_reads_where_it_is_written(event, reads):
    sql = _sql(event)
    for column in reads:
        assert column in sql, column


@pytest.mark.parametrize(
    "event", [OrderEvent.in_process, OrderEvent.finished, OrderEvent.cancelled]
)
def test_a_history_event_skips_same_status_rows(event):
    assert (
        "order_status_history.from_status IS DISTINCT FROM "
        "order_status_history.to_status" in _sql(event)
    )


def test_the_subqueries_correlate_to_the_order():
    # Inside a query over ``orders`` the EXISTS must not drag its own copy of
    # the table into FROM: that would be a cross join, true for every order.
    for event in OrderEvent:
        sql = _sql(event)
        assert sql.count("FROM orders") == 1, event


def test_either_end_may_be_open():
    sql = _sql(OrderEvent.finished, start=None, actor_id=None)
    assert "order_status_history.created_at <" in sql
    assert "order_status_history.created_at >=" not in sql
