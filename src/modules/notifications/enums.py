from enum import Enum


class NotificationType(str, Enum):
    """Closed set of notification event types (extensible).

    The canonical value is the stable machine string sent to the frontend. New
    events (other transitions, banding, etc.) add a member here without touching
    the model or the endpoints.
    """

    # The order finished every activity. Keeps its wire value: the dashboard maps
    # it, and the event is the same one -- only the state's name changed.
    order_completed = "order.completed"
    order_queued = "order.queued"
    # The client accepted the quote from the public review link, which is the
    # only way an order is born -- so this is the event that closes a sale.
    order_confirmed = "order.confirmed"
    # Two halves of one move, kept as two types because the recipients are
    # disjoint (a user belongs to one branch) and each side reads differently.
    order_branch_arrived = "order.branch_arrived"
    order_branch_left = "order.branch_left"
