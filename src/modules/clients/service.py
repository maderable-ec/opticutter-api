from typing import List, Optional, Tuple

from fastapi import Depends
from sqlalchemy.orm import Session

from src.modules.clients.model import ClientModel
from src.modules.clients.schemas import ClientCreate, ClientUpdate
from src.shared.crud import CRUDService, ListSort
from src.shared.database import get_db
from src.shared.exceptions import BusinessRuleError


def require_phone(client: ClientModel) -> None:
    """Require a registered phone number before issuing commercial documents.

    Hard business rule: neither the proforma nor the order can be generated
    without a valid mobile phone number. Email is optional and never blocks.
    """
    if not (client.phone and client.phone.strip()):
        raise BusinessRuleError(
            "El cliente no tiene un número de celular registrado. Solicita y "
            "registra su celular antes de generar la proforma o el pedido."
        )


class ClientService(CRUDService[ClientModel, ClientCreate, ClientUpdate]):
    """Client CRUD + specific search queries."""

    model = ClientModel
    conflict_messages = {"identifier": "El identificador ya existe"}

    def list_clients(
        self,
        search: Optional[str] = None,
        sort: ListSort = "name",
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[ClientModel], int]:
        """Lists clients with optional search and ordering; ``(items, total)``.

        ``search`` matches the identifier, the first name or the last name. One
        method rather than the ``if search: search_paginated() else:
        list_paginated()`` the router used to branch on -- that shape stops
        composing the moment a listing has a second filter.
        """
        query = self.db.query(ClientModel)
        if search:
            pattern = f"%{search}%"
            query = query.filter(
                ClientModel.identifier.ilike(pattern)
                | ClientModel.first_name.ilike(pattern)
                | ClientModel.last_name.ilike(pattern)
            )
        query = self._apply_sort(
            query, sort, ClientModel.last_name, ClientModel.first_name
        )
        return self._paginate(query, limit, offset)


def client_service(db: Session = Depends(get_db)) -> ClientService:
    """``ClientService`` provider for route injection."""
    return ClientService(db)
