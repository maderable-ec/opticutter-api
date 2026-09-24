from enum import Enum
from typing import Iterable


class UserRole(str, Enum):
    """Role of an internal user (closed to four values).

    The canonical value is the Spanish string stored in the DB (``"administrador"`` /
    ``"vendedor"`` / ``"operador"`` / ``"canteador"``). ``_missing_`` accepts the
    input case-insensitively, replicating the ``BandType`` pattern
    (``src/modules/products/types/edge_banding.py``), so the field stays closed
    to these values both when creating/updating users and when filtering.

    ``operador`` and ``canteador`` are workshop roles (bound to a branch): the
    operator cuts; the bander applies edge banding on the banding line.

    A user holds a LIST of roles, and the declaration order below is the
    canonical one: it orders what is stored and names the PRIMARY role (the
    first), which is what the legacy single-``role`` fields still report.
    """

    ADMIN = "administrador"
    SELLER = "vendedor"
    OPERATOR = "operador"
    BANDER = "canteador"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            norm = value.strip().lower()
            for member in cls:
                if member.value.lower() == norm:
                    return member
        return None


# Global roles see and operate every branch; workshop roles are bound to one.
GLOBAL_ROLES = frozenset({UserRole.ADMIN, UserRole.SELLER})
# The only roles that combine on one user: a bander learning to cut holds both.
# The global ones stay exclusive -- the admin already holds every permission,
# and a seller who also cut would need a branch scope nobody has defined.
WORKSHOP_ROLES = frozenset({UserRole.OPERATOR, UserRole.BANDER})

_RANK = {role: index for index, role in enumerate(UserRole)}


def canonical_roles(values: Iterable) -> list[str]:
    """Coerces, deduplicates and orders roles canonically (``UserRole`` order).

    The single definition of the order: the stored list and the primary role
    (its first element) both come from here.
    """
    return [role.value for role in sorted({UserRole(v) for v in values}, key=_RANK.get)]
