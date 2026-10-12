"""What an order still owes, and the payments that bring it down.

An order's sale freezes how it was paid for: cash, transfer and CREDIT
(``orders.payment_*_amount``). The credit used to read as owed for ever, though
the client usually settles it on delivery. Every payment after the sale is a row
of ``order_payments``, and the balance is the credit sold less those rows.

One definition, read three ways: off a loaded order
(``OrderModel.credit_balance``), in SQL (``credit_balance_expr`` -- the listing's
filter and the statistics' snapshot) and before a write (``check_against_balance``).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import case, func, select

from src.modules.orders.model import (
    BALANCE_TOLERANCE,
    PAYMENT_METHOD_LABELS,
    OrderModel,
    OrderPaymentModel,
    OrderStatus,
    PaymentMethod,
)
from src.shared.audit import Actor
from src.shared.exceptions import ValidationError

Amounts = list[tuple[PaymentMethod, float]]


def payment_amounts(payment) -> Amounts:
    """The cash and transfer amounts of a payment input that are worth a row.

    Reads ``cash_amount``/``transfer_amount`` off whatever carries them (the
    payment endpoint's body or the dispatch's). Credit is never one of them:
    it is what is owed, not a way of paying it.
    """
    if payment is None:
        return []
    pairs = (
        (PaymentMethod.cash, getattr(payment, "cash_amount", None)),
        (PaymentMethod.transfer, getattr(payment, "transfer_amount", None)),
    )
    return [(method, round(amount, 2)) for method, amount in pairs if (amount or 0) > 0]


def check_against_balance(order: OrderModel, amounts: Amounts) -> float:
    """What the order would still owe after ``amounts``. Raises if they exceed it.

    A gate: call it before writing anything. ``field="payment"`` rides to the
    envelope so the dialog can hang the message on its inputs.
    """
    balance = order.credit_balance
    paying = round(sum(amount for _, amount in amounts), 2)
    if paying > balance + BALANCE_TOLERANCE:
        raise ValidationError(
            f"El pago (${paying:.2f}) excede el saldo pendiente (${balance:.2f})",
            field="payment",
        )
    remaining = round(balance - paying, 2)
    return remaining if remaining > BALANCE_TOLERANCE else 0.0


def register_payments(
    order: OrderModel,
    amounts: Amounts,
    actor: Actor,
    now: datetime,
    note: Optional[str] = None,
) -> list[OrderPaymentModel]:
    """Appends one row per method to the order, uncommitted. No gate of its own."""
    rows = [
        OrderPaymentModel(
            method=method.value,
            amount=amount,
            paid_at=now,
            note=(note or "").strip() or None,
            received_by=actor.user_id,
            received_by_label=actor.label,
        )
        for method, amount in amounts
    ]
    order.payments.extend(rows)
    return rows


def describe_amounts(amounts: Amounts) -> str:
    """``efectivo $50.00 · transferencia $70.00``, for the history row."""
    return " · ".join(
        f"{PAYMENT_METHOD_LABELS[method]} ${amount:.2f}" for method, amount in amounts
    )


# ------------------------------------------------------------------------ SQL
def collected_expr():
    """Sum of an order's standing payments, correlated to ``orders``."""
    return (
        select(func.coalesce(func.sum(OrderPaymentModel.amount), 0.0))
        .where(
            OrderPaymentModel.order_id == OrderModel.id,
            OrderPaymentModel.voided_at.is_(None),
        )
        .correlate(OrderModel)
        .scalar_subquery()
    )


def credit_balance_expr():
    """``OrderModel.credit_balance`` as a SQL expression (not clamped at zero)."""
    return case(
        (OrderModel.status == OrderStatus.cancelled.value, 0.0),
        else_=func.coalesce(OrderModel.payment_credit_amount, 0.0) - collected_expr(),
    )


def has_balance():
    """Filter: orders the client still owes on."""
    return credit_balance_expr() > BALANCE_TOLERANCE
