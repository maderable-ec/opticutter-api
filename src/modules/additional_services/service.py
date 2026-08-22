from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.additional_services.model import AdditionalServiceModel
from src.modules.additional_services.schemas import (
    AdditionalServiceCreate,
    AdditionalServiceUpdate,
)
from src.shared.crud import CRUDService, ListSort
from src.shared.database import get_db


class AdditionalServiceService(
    CRUDService[
        AdditionalServiceModel, AdditionalServiceCreate, AdditionalServiceUpdate
    ]
):
    """Additional services CRUD + name search / active filter."""

    model = AdditionalServiceModel
    conflict_messages = {"name": "El servicio ya existe"}

    def search_paginated(
        self,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        limit: int = 20,
        offset: int = 0,
        sort: ListSort = "name",
    ) -> Tuple[List[AdditionalServiceModel], int]:
        """Lists services filtered by name search and/or active flag; ``(items, total)``."""
        query = self.db.query(AdditionalServiceModel)
        if search:
            query = query.filter(AdditionalServiceModel.name.ilike(f"%{search}%"))
        if is_active is not None:
            query = query.filter(AdditionalServiceModel.is_active.is_(is_active))
        query = self._apply_sort(query, sort, AdditionalServiceModel.name)
        return self._paginate(query, limit, offset)


def additional_service_service(
    db: Session = Depends(get_db),
) -> AdditionalServiceService:
    """``AdditionalServiceService`` provider for route injection."""
    return AdditionalServiceService(db)
