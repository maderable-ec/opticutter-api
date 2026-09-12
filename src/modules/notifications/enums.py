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
