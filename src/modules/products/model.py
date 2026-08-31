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

    The common fields (``code``, ``name``, the three prices, ``type``,
    ``is_active``) are queryable/constrainable columns; what's specific to each
    type lives in the JSON ``attributes``, validated by its Pydantic schema at
    the API boundary.
    """

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("price_2 IS NULL OR price_2 >= 0", name="price_2_non_negative"),
        CheckConstraint("price_3 IS NULL OR price_3 >= 0", name="price_3_non_negative"),
    )

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
    # The three sale prices the vendor publishes per article, all NET of tax
    # (``marticulo.ven``/``pv2``/``pv3``). ``price`` is level 1 — the list price,
    # what every edge banding and every board the seller doesn't mark is billed
    # at; ``price_2``/``price_3`` are the reduced levels and are NULL when the
    # source never loaded them (it writes 0.000000), in which case billing falls
    # back to ``price`` (see ``optimizations/price_levels.price_at_level``).
    # Storing them net is what makes the documents' "Subtotal / IVA / Total"
    # arithmetic possible; the tax rate lives in the ``settings`` singleton.
    price: Mapped[float] = mapped_column(Float)
    price_2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_3: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
