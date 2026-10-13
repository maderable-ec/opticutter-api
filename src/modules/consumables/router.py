"""The shop floor's consumables: what is on each machine, and registering a change.

The yield over a period is a statistic and lives with the rest of them
(``GET /analytics/consumables``); this is the shop floor's side.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.modules.consumables.schemas import (
    BranchConsumables,
    ConsumableChangeCreate,
    ConsumableChangeVoid,
)
from src.modules.consumables.service import ConsumableService, consumable_service
from src.modules.users.dependencies import get_branch_scope, require_permission
from src.modules.users.model import UserModel
from src.modules.users.permissions import RESOURCE_ROLES
from src.shared.audit import staff_actor
from src.shared.responses import ERROR_RESPONSES, DataResponse, ok

router = APIRouter(
    prefix="/consumables", tags=["consumables"], responses=ERROR_RESPONSES
)


def _can_manage(user: UserModel) -> bool:
    """Whether the user corrects the record: a past change, or voiding any."""
    return user.has_any_role(*RESOURCE_ROLES["consumables:manage"])


@router.get("/status", response_model=DataResponse[list[BranchConsumables]])
def get_consumables_status(
    branch_id: Optional[int] = Query(
        default=None,
        alias="branchId",
        description="Restricts to a branch (the admin's; the shop floor sees its own)",
    ),
    svc: ConsumableService = Depends(consumable_service),
    current_user: UserModel = Depends(require_permission("consumables:register")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """What is on each machine and what it has done since it went on.

    Per branch, the consumables the viewer may register (``CONSUMABLE_ROLES``):
    the last change, the metres since, and the ``gate`` a change right now
    would meet -- ``blocked`` (one was just registered), ``early`` (it needs
    the reason) or ``free``.
    """
    return ok(svc.status(staff_actor(current_user), branch_scope, branch_id))


@router.post(
    "/changes", response_model=DataResponse[BranchConsumables], status_code=201
)
def register_consumable_change(
    data: ConsumableChangeCreate,
    svc: ConsumableService = Depends(consumable_service),
    current_user: UserModel = Depends(require_permission("consumables:register")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Registers a blade mounted or the glue pot loaded.

    409 when one of the same kind was registered within the last minute; 422
    with ``field="reason"`` when the previous one is under four working hours
    old and no reason came. Answers with the branch as it now stands.
    """
    return ok(
        svc.register(
            data,
            staff_actor(current_user),
            branch_scope,
            can_manage=_can_manage(current_user),
        )
    )


@router.post(
    "/changes/{change_id}/void", response_model=DataResponse[BranchConsumables]
)
def void_consumable_change(
    change_id: int,
    data: Optional[ConsumableChangeVoid] = None,
    svc: ConsumableService = Depends(consumable_service),
    current_user: UserModel = Depends(require_permission("consumables:register")),
    branch_scope: Optional[int] = Depends(get_branch_scope),
):
    """Takes a change back; the row stays, marked. Idempotent.

    Whoever registered it undoes it on their own for a few minutes; past
    that it is the admin's, with the reason.
    """
    return ok(
        svc.void(
            change_id,
            data.reason if data else None,
            staff_actor(current_user),
            branch_scope,
            can_manage=_can_manage(current_user),
        )
    )
