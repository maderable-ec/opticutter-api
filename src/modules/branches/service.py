from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.branches.model import BranchModel
from src.modules.branches.schemas import BranchCreate, BranchUpdate
from src.shared.crud import CRUDService, ListSort
from src.shared.database import get_db
from src.shared.exceptions import EntityNotFoundError, ValidationError


def resolve_branch_for_create(
    db: Session,
    branch_scope: Optional[int],
    requested_branch_id: Optional[int],
    default_branch_id: Optional[int] = None,
) -> int:
    """Resolves and validates the target branch for a creation (order/pre-order/draft).

    - scoped (``branch_scope`` not None, e.g. operator): always their own branch
      (ignores what's requested in the body, so they can't create in another branch).
    - global (``branch_scope`` None: admin or seller): uses ``requested_branch_id``
      if given; otherwise falls back to ``default_branch_id`` (the creator's
      **base branch**). The admin has no base branch (``None``), so they must
      provide ``branchId``; the seller defaults to their own and can override it
      with ``branchId``.

    Verifies the branch exists and is active.
    """
    if branch_scope is not None:
        branch_id: Optional[int] = branch_scope
    elif requested_branch_id is not None:
        branch_id = requested_branch_id
    else:
        branch_id = default_branch_id
    if branch_id is None:
        raise ValidationError(
            "Debes indicar la sucursal de destino (branchId).", field="branchId"
        )
    branch = db.get(BranchModel, branch_id)
    if branch is None:
        raise EntityNotFoundError("Branch", branch_id)
    if not branch.is_active:
        raise ValidationError("La sucursal indicada está inactiva.", field="branchId")
    return branch_id


class BranchService(CRUDService[BranchModel, BranchCreate, BranchUpdate]):
    """Branch CRUD + specific searches."""

    model = BranchModel
    conflict_messages = {"code": "El código de sucursal ya existe"}

    def get_by_code(self, code: str) -> Optional[BranchModel]:
        """Gets a branch by its code."""
        return self.db.query(BranchModel).filter(BranchModel.code == code).first()

    def list_branches(
        self,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        sort: ListSort = "name",
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[BranchModel], int]:
        """Lists branches with optional search/active filter and ordering.

        ``is_active`` was a column the listing showed and could toggle but not
        filter by -- and a branch is retired by unsetting it, never deleted, so
        the inactive ones accumulate in the middle of the list.
        """
        query = self.db.query(BranchModel)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                BranchModel.code.ilike(pattern) | BranchModel.name.ilike(pattern)
            )
        if is_active is not None:
            query = query.filter(BranchModel.is_active.is_(is_active))
        query = self._apply_sort(query, sort, BranchModel.name)
        return self._paginate(query, limit, offset)


def branch_service(db: Session = Depends(get_db)) -> BranchService:
    """Provider for ``BranchService`` injection in routes."""
    return BranchService(db)
