"""Actor identity for the (append-only) history tables.

An ``Actor`` describes who originates an audited transition: ``staff`` (with FK
to the user + frozen label), ``client`` (public action via review link) or
``system`` (automation such as expiry). History tables store ``actor`` (the
type), ``actor_user_id`` (nullable FK) and ``actor_label`` (a readable snapshot
that survives the user being deleted/renamed).
"""

from dataclasses import dataclass
from typing import Optional, Tuple

ACTOR_STAFF = "staff"
ACTOR_CLIENT = "client"
ACTOR_SYSTEM = "system"


@dataclass(frozen=True)
class Actor:
    """Origin of an audited action (type + optional attribution)."""

    type: str
    user_id: Optional[int] = None
    label: Optional[str] = None
    # Empty for the client and the system: the role gates skip them.
    roles: Tuple[str, ...] = ()


def staff_actor(user) -> Actor:
    """Staff actor from the authenticated ``UserModel`` (FK + snapshot)."""
    return Actor(
        ACTOR_STAFF,
        user_id=user.id,
        label=user.full_name or user.email,
        roles=tuple(user.roles),
    )


def client_actor(label: Optional[str] = None) -> Actor:
    """Client actor (public action via review link)."""
    return Actor(ACTOR_CLIENT, label=label or "Cliente")


def system_actor() -> Actor:
    """System actor (automation without a user, e.g. expiry)."""
    return Actor(ACTOR_SYSTEM)
