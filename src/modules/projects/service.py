from datetime import datetime
from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from src.modules.projects.model import (
    PROJECT_TRANSITIONS,
    STATUS_LABELS,
    ProjectModel,
    ProjectStatus,
)
from src.modules.projects.schemas import ProjectCreate, ProjectUpdate
from src.modules.users.enums import UserRole
from src.modules.users.model import UserModel
from src.shared.crud import CRUDService
from src.shared.database import get_db
from src.shared.exceptions import BusinessRuleError, EntityNotFoundError

# Columns a ``null`` in the body must not reach: they are NOT NULL, and an
# explicit null means "leave it", as an absent key does.
_REQUIRED = frozenset({"name", "payload"})


def owner_scope(user: UserModel) -> Optional[int]:
    """Which projects ``user`` reaches: ``None`` = all of them, else only theirs.

    The admin and the seller see every project (the seller, to quote the ready
    ones); the designer, only their own. What each can DO is the permission
    matrix's (the seller has no ``projects:write``); this decides which rows.
    """
    if user.has_any_role(UserRole.ADMIN, UserRole.SELLER):
        return None
    return user.id


class ProjectService(CRUDService[ProjectModel, ProjectCreate, ProjectUpdate]):
    """The designers' projects, scoped by owner.

    Mirrors ``BranchScopedMixin`` with the owner in place of the branch: a
    project out of scope is a uniform 404, never a 403, so it doesn't reveal
    that it exists.
    """

    model = ProjectModel

    def get_owned_or_404(self, id: int, scope: Optional[int]) -> ProjectModel:
        """Gets a project verifying it belongs to the caller (when scoped)."""
        project = self.get_or_404(id)
        if scope is not None and project.owner_id != scope:
            raise EntityNotFoundError(self.model.__name__, id)
        return project

    def create_owned(self, data: ProjectCreate, owner_id: int) -> ProjectModel:
        """Creates a project in progress, owned by ``owner_id`` (the caller)."""
        project = ProjectModel(
            **data.model_dump(),
            owner_id=owner_id,
            status=ProjectStatus.in_progress.value,
        )
        return self._persist(project)

    def list_projects(
        self,
        scope: Optional[int] = None,
        status: Optional[List[ProjectStatus]] = None,
        owner_filter: Optional[int] = None,
        search: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[ProjectModel], int]:
        """Lists projects, most recently touched first; ``(items, total)``.

        ``owner_filter`` narrows an unscoped listing (admin/seller) to one
        designer; a scoped one is already locked to its owner and ignores it.
        """
        query = self.db.query(ProjectModel).options(
            joinedload(ProjectModel.owner), selectinload(ProjectModel.quotes)
        )
        if scope is not None:
            query = query.filter(ProjectModel.owner_id == scope)
        elif owner_filter is not None:
            query = query.filter(ProjectModel.owner_id == owner_filter)
        if status:
            query = query.filter(ProjectModel.status.in_([s.value for s in status]))
        if search:
            pattern = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    ProjectModel.name.ilike(pattern),
                    ProjectModel.client_name.ilike(pattern),
                )
            )
        query = query.order_by(ProjectModel.updated_at.desc(), ProjectModel.id.desc())
        return self._paginate(query, limit, offset)

    def update_owned(
        self, id: int, data: ProjectUpdate, scope: Optional[int] = None
    ) -> ProjectModel:
        """Overwrites a project, only while it is in progress.

        A ready project is in the sellers' queue: changing it under a seller who
        is quoting it would quote something else. Its designer reopens it first.
        """
        project = self.get_owned_or_404(id, scope)
        if project.status != ProjectStatus.in_progress.value:
            label = STATUS_LABELS[ProjectStatus(project.status)]
            raise BusinessRuleError(
                f"El proyecto está {label}: solo se edita un proyecto en curso.",
                field="status",
            )
        for field, value in data.model_dump(exclude_unset=True).items():
            if value is None and field in _REQUIRED:
                continue
            setattr(project, field, value)
        return self._persist(project)

    def change_status(
        self, id: int, status: ProjectStatus, scope: Optional[int] = None
    ) -> ProjectModel:
        """Moves a project along ``PROJECT_TRANSITIONS``; idempotent.

        Handing it to the sellers stamps ``ready_at``. ``quoted`` is never a
        target here: only creating a pre-order from the project sets it.
        """
        project = self.get_owned_or_404(id, scope)
        current = ProjectStatus(project.status)
        if status == current:
            return project
        if status not in PROJECT_TRANSITIONS[current]:
            raise BusinessRuleError(
                f"Un proyecto {STATUS_LABELS[current]} no puede pasar a "
                f"{STATUS_LABELS[status]}.",
                field="status",
            )
        project.status = status.value
        if status == ProjectStatus.ready:
            project.ready_at = datetime.utcnow()
        return self._persist(project)


def project_service(db: Session = Depends(get_db)) -> ProjectService:
    """Provider for ``ProjectService`` injection in routes."""
    return ProjectService(db)
