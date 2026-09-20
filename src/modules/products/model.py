from enum import Enum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class ProductType(str, Enum):
    """Product types sold.

    Each type contributes its own ``attributes`` schema (see
    ``products.registry``); adding a new type requires no database migration.
    """

    BOARD = "board"  # melamine board (the optimizer's only input)
    EDGE_BANDING = "edge_banding"  # edge banding (future)


class ProductFamilyModel(TimestampMixin, AuditMixin, Base):
    """A design group: the boards and edge bandings that coordinate with each other.

    This is the board<->tapacanto coordination, and it lives HERE rather than in
    the vendor's catalog. It used to be a free-text ``family`` key inside each
    product's ``attributes`` bag, seeded from SIFAC's ``marticulo.obs`` — which
    made it unmanageable from this side for a mechanical reason: the sync's
    update does ``product.attributes = row.attributes`` (a wholesale replacement
    of the bag), so anything typed into the product form was wiped on the next
    pass. A row of our own, pointed at by a real column, is immune by
    construction: the sync only ever writes the columns it names.

    It also kills a whole class of silent bug. Matching on a text key meant a
    board left on "Cashmere" and its tape moved to "CSH" simply stopped
    coordinating — no error, just an empty picker. A foreign key cannot be
    mistyped.

    The group is the TAPE's design, not the board's, and that is deliberate: 23
    of the 134 MDP boards hang off a family named after another design (ROBLE
    BARROCO DORADO under "Olmo Panela"), which is how the shop handles a tape
    that was discontinued or a colour close enough to substitute. Members are
    1:N (one family per product) because no board has ever needed two tape
    designs at once; a membership table would be the additive change if one ever
    does.

    ``normalized_name`` is a stored column rather than a functional unique index
    on ``lower(name)`` because ``normalize_family`` casefolds, which is not
    Postgres' ``lower()``. With an index there would be two definitions of "the
    same family" and they would diverge the day someone types a name outside
    ASCII; with the column, the rule lives once in Python and the database only
    enforces uniqueness of what it is handed.
    """

    __tablename__ = "product_families"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The name as written, capitalization preserved for display ("Olmo Panela").
    name: Mapped[str] = mapped_column(String(64))
    # ``normalize_family(name)`` — the matching key, and the UNIQUE one.
    normalized_name: Mapped[str] = mapped_column(String(64), unique=True)
    # Why this family exists / what it substitutes, for whoever inherits the
    # catalog ("reemplaza a Roble Barroco, descontinuado").
    description: Mapped[Optional[str]] = mapped_column(String(256))

    products: Mapped[list["ProductModel"]] = relationship(
        back_populates="family",
        # The FK is ON DELETE SET NULL, so deleting a family unassigns its
        # members. ``passive_deletes`` is what lets the database do that in one
        # statement instead of SQLAlchemy loading every child and nulling it out
        # one by one in Python.
        passive_deletes=True,
    )


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
        # Promoting ``alias`` out of the JSON bag opened it to every product
        # type; it is an edge-banding field and only ever was (a board has no
        # alias — ``BoardAttributes`` never declared one). Enforced here so the
        # schema layer isn't the only thing saying so.
        CheckConstraint(
            "alias IS NULL OR type = 'edge_banding'",
            name="alias_only_on_edge_banding",
        ),
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
    # The design group this product coordinates through (``ProductFamilyModel``).
    # Nullable, and NULL is a meaningful value rather than a gap: the 76 non-MDP
    # boards (plywood, OSB, MDF fondo) have no coordinated banding at all.
    # WRITTEN BY THE SYNC ONLY ON CREATE — never on update. That omission is the
    # whole point of the column; see ``catalog_sync._apply``.
    family_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("product_families.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Short design code the workshop notation prints ("1L CS CSH", see
    # ``optimizations/labels.py``) so two banded designs are told apart on the
    # thermal label and the cut diagram. Edge banding only (CHECK above), and
    # independent of the family, which coordinates but is never printed.
    # Also sync-on-create-only, for the same reason as ``family_id``.
    alias: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    family: Mapped[Optional["ProductFamilyModel"]] = relationship(
        back_populates="products",
        # Many-to-one against a 75-row table, and ``ProductResponse`` embeds the
        # family name: without eager loading a 20-row page costs 20 extra queries.
        lazy="joined",
    )
