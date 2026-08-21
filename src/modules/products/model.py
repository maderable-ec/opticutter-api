from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class ProductType(str, Enum):
    """Product types sold.

    Each type contributes its own ``attributes`` schema (see
    ``products.registry``); adding a new type requires no database migration.
    """

    BOARD = "board"  # melamine board (the optimizer's only input)
    EDGE_BANDING = "edge_banding"  # edge banding (future)


class ProductModel(TimestampMixin, AuditMixin, Base):
    """Unified catalog: common columns + per-type ``attributes``.

    The common fields (``code``, ``name``, ``price``, ``type``, ``is_active``)
    are queryable/constrainable columns; what's specific to each type lives in
    the JSON ``attributes``, validated by its Pydantic schema at the API boundary.
    """

    __tablename__ = "products"
    __table_args__ = (CheckConstraint("price >= 0", name="price_non_negative"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    # Set only by the external catalog sync (``products/catalog_sync.py``), as
    # "{CATEGORIA}:{CODIGO}" (e.g. "TABLEROS:1033") — never exposed on the
    # manual create/update schemas. Its presence is what marks a product as
    # sync-managed vs. hand-created, which drives the sync's deactivation pass.
    external_code: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[Optional[str]] = mapped_column(String(256))
    price: Mapped[float] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
