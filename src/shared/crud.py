from typing import Generic, List, Literal, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from src.shared.context import get_current_user_id
from src.shared.database import Base
from src.shared.exceptions import ConflictError, EntityNotFoundError
from src.shared.mixins import AuditMixin

ModelT = TypeVar("ModelT", bound=Base)

# The sort every CRUD listing offers. ``name`` is the default everywhere: these are
# reference catalogues, read alphabetically. ``recent``/``oldest`` exist because the
# back office also asks "what did we just add?".
ListSort = Literal["name", "recent", "oldest"]
CreateT = TypeVar("CreateT", bound=BaseModel)
UpdateT = TypeVar("UpdateT", bound=BaseModel)


class CRUDService(Generic[ModelT, CreateT, UpdateT]):
    """Generic CRUD service over an ORM model.

    Subclasses define ``model`` and, optionally, ``conflict_messages``
    (constraint substring -> readable message) and entity-specific methods.

    Replaces the CRUD logic repeated across per-entity services and repositories.
    """

    model: Type[ModelT]
    conflict_messages: dict = {}

    def __init__(self, db: Session):
        self.db = db

    def get(self, id: int) -> Optional[ModelT]:
        return self.db.get(self.model, id)

    def get_or_404(self, id: int) -> ModelT:
        obj = self.get(id)
        if obj is None:
            raise EntityNotFoundError(self.model.__name__, id)
        return obj

    def list_paginated(
        self, limit: int = 20, offset: int = 0
    ) -> Tuple[List[ModelT], int]:
        """A page of records plus its total count: ``(items, total)``."""
        return self._paginate(self.db.query(self.model), limit, offset)

    def _paginate(
        self, query: Query, limit: int, offset: int
    ) -> Tuple[List[ModelT], int]:
        """Counts the total and returns the page; reusable by filtered searches.

        Applies no ordering of its own: callers that page a listing must order it
        themselves (``_apply_sort`` does it). Paging an unordered query lets
        Postgres repeat or skip rows between pages.
        """
        total = query.count()
        items = query.offset(offset).limit(limit).all()
        return items, total

    def _apply_sort(self, query: Query, sort: str, *name_columns) -> Query:
        """Orders a listing by ``sort``, always with a primary-key tiebreaker.

        The tiebreaker is not cosmetic. Without a *total* order, a paged query has
        no defined answer for "the next 20 rows" and Postgres may hand back one it
        already sent -- the bug ``test_list_is_ordered_by_name_and_pages_do_not_overlap``
        was written to pin for products, and which every other CRUD listing had.
        """
        if sort == "recent":
            return query.order_by(self.model.id.desc())
        if sort == "oldest":
            return query.order_by(self.model.id.asc())
        return query.order_by(*name_columns, self.model.id.asc())

    def create(self, data: CreateT) -> ModelT:
        return self._persist(self.model(**data.model_dump()))

    def update(self, id: int, data: UpdateT) -> ModelT:
        obj = self.get_or_404(id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(obj, field, value)
        return self._persist(obj)

    def _stamp_actor(self, obj: ModelT) -> None:
        """Stamps ``created_by``/``updated_by`` from the request's user.

        Centralized in ``_persist`` to also cover services that call it
        directly (e.g. ``ProductService``). Distinguishes create from update by
        the object's identity (transient = no PK yet = creation). No-op for
        models without ``AuditMixin`` or requests without a user (public).
        """
        if not isinstance(obj, AuditMixin):
            return
        user_id = get_current_user_id()
        if user_id is None:
            return
        if inspect(obj).identity is None:  # transient: this is a create
            obj.created_by = user_id
        obj.updated_by = user_id

    def delete(self, id: int) -> None:
        self.db.delete(self.get_or_404(id))
        self.db.commit()

    def _persist(self, obj: ModelT) -> ModelT:
        self._stamp_actor(obj)
        try:
            self.db.add(obj)
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(self._conflict_detail(exc)) from exc
        self.db.refresh(obj)
        return obj

    def _conflict_detail(self, exc: IntegrityError) -> str:
        text = str(exc.orig) if exc.orig else str(exc)
        for needle, message in self.conflict_messages.items():
            if needle in text:
                return message
        return "Violación de restricción de integridad"
