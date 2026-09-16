from typing import Optional

from sqlalchemy import Boolean, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database import Base
from src.shared.mixins import AuditMixin, TimestampMixin


class BranchModel(TimestampMixin, AuditMixin, Base):
    """Business branch (warehouse): root entity of the multi-branch isolation.

    Branches used to only exist as a letterhead JSON in ``settings``; now
    they're a real entity that orders, pre-orders, drafts and users point to
    (``branch_id``). Staff (seller/operator) is bound to a branch; the admin
    isn't (sees and operates all of them). Deactivation is logical
    (``is_active``) to avoid breaking historical FKs.

    It also carries the branch's **printing capability**: whether its shop has a
    thermal label printer and/or a sheet printer. The print job resolves its
    branch from ``order.branch_id``, so these flags gate the enqueue for every
    role -- including the global ones (admin/seller), whose own branch scope is
    ``None``.
    """

    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[Optional[str]] = mapped_column(String(256))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # The vendor's warehouse this branch's stock lives in (``mbodega.cod``, which
    # is what ``binventario.bod`` points at: 1 = Bodega local/Sucúa, 2 = Macas).
    # A dedicated column and not ``code``, which is free text the admin edits:
    # the day somebody renames "SUCUA" the inventory would silently answer for
    # the wrong warehouse. NULL means "this branch doesn't consult stock" -- the
    # alert stays quiet and the report skips it, which is the correct answer for
    # a branch the vendor's system doesn't know about.
    warehouse_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Printing switches (see ``print_jobs.service``). Default ON so the existing
    # branches keep printing after the deploy; the admin unticks the ones with no
    # hardware, which stops the payload from ever being rendered or spooled.
    print_labels_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    print_consolidated_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
