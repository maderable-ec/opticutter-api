"""Order attachments (anexos): metadata in Postgres, bytes on local disk.

Branch-scoped through the parent order (a resource from another branch returns a
uniform 404, like ``OrderService``). Attachments can only be added/removed while
the order is not in a terminal state (completed/dispatched/cancelled).
"""

import io
from pathlib import PurePosixPath, PureWindowsPath
from typing import List, Optional, Tuple

from fastapi import Depends, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from src.modules.orders import attachment_storage as storage
from src.modules.orders.model import (
    TERMINAL_STATUSES,
    OrderAttachmentModel,
    OrderModel,
    OrderStatus,
)
from src.shared.audit import Actor, system_actor
from src.shared.branch_scope import BranchScopedMixin
from src.shared.config import config
from src.shared.database import get_db
from src.shared.exceptions import (
    BusinessRuleError,
    EntityNotFoundError,
    ValidationError,
)

# Content types that must decode as a real image (guards against a disguised blob).
_IMAGE_TYPES = {"image/png", "image/jpeg"}


def _safe_filename(name: Optional[str]) -> str:
    """Keeps only the base name of the client-supplied filename (display only).

    The on-disk name is a uuid (see ``storage.build_key``); this is what the user
    sees on download, so we still strip any path component from either OS.
    """
    raw = (name or "archivo").strip() or "archivo"
    base = PureWindowsPath(PurePosixPath(raw).name).name
    return base[:255] or "archivo"


class AttachmentService(BranchScopedMixin):
    """CRUD of order attachments with the order's terminal-state gate."""

    model = OrderModel

    def __init__(self, db: Session):
        self.db = db

    def get_or_404(self, order_id: int) -> OrderModel:
        order = self.db.get(OrderModel, order_id)
        if order is None:
            raise EntityNotFoundError("Order", order_id)
        return order

    def _editable_order(self, order_id: int, branch_scope: Optional[int]) -> OrderModel:
        """Loads the (branch-scoped) order and rejects terminal states."""
        order = self.get_scoped_or_404(order_id, branch_scope)
        if OrderStatus(order.status) in TERMINAL_STATUSES:
            raise BusinessRuleError(
                "No se pueden modificar los anexos de una orden completada, "
                "despachada o cancelada"
            )
        return order

    def list_attachments(
        self, order_id: int, branch_scope: Optional[int] = None
    ) -> List[OrderAttachmentModel]:
        self.get_scoped_or_404(order_id, branch_scope)
        return (
            self.db.query(OrderAttachmentModel)
            .filter(OrderAttachmentModel.order_id == order_id)
            .order_by(OrderAttachmentModel.id)
            .all()
        )

    def iter_annex_bytes(
        self, order_id: int, branch_scope: Optional[int] = None
    ) -> List[Tuple[bytes, str]]:
        """Every attachment's bytes + content type, for the order packet.

        An attachment whose file is missing from disk is skipped: the metadata
        row outliving its bytes must not stop the shop from printing the order.
        Kept here rather than at the two call sites (the endpoint and the print
        queue) that used to carry the same loop.
        """
        annexes: List[Tuple[bytes, str]] = []
        for att in self.list_attachments(order_id, branch_scope=branch_scope):
            try:
                annexes.append((storage.read(att.stored_key), att.content_type))
            except OSError:
                continue
        return annexes

    def get_attachment(
        self, order_id: int, attachment_id: int, branch_scope: Optional[int] = None
    ) -> OrderAttachmentModel:
        self.get_scoped_or_404(order_id, branch_scope)
        att = self.db.get(OrderAttachmentModel, attachment_id)
        if att is None or att.order_id != order_id:
            raise EntityNotFoundError("OrderAttachment", attachment_id)
        return att

    def add_attachment(
        self,
        order_id: int,
        upload: UploadFile,
        actor: Optional[Actor] = None,
        branch_scope: Optional[int] = None,
    ) -> OrderAttachmentModel:
        actor = actor or system_actor()
        self._editable_order(order_id, branch_scope)

        content_type = (upload.content_type or "").lower()
        data = self._read_and_validate(upload, content_type)

        stored_key = storage.build_key(order_id, content_type)
        storage.save(stored_key, data)
        att = OrderAttachmentModel(
            order_id=order_id,
            filename=_safe_filename(upload.filename),
            stored_key=stored_key,
            content_type=content_type,
            size_bytes=len(data),
        )
        if actor.user_id is not None:
            att.created_by = actor.user_id
        self.db.add(att)
        try:
            self.db.commit()
        except Exception:
            # Never leave an orphan file if the metadata row fails to persist.
            self.db.rollback()
            storage.remove(stored_key)
            raise
        self.db.refresh(att)
        return att

    def delete_attachment(
        self,
        order_id: int,
        attachment_id: int,
        actor: Optional[Actor] = None,
        branch_scope: Optional[int] = None,
    ) -> None:
        self._editable_order(order_id, branch_scope)
        att = self.get_attachment(order_id, attachment_id, branch_scope)
        stored_key = att.stored_key
        self.db.delete(att)
        self.db.commit()
        # Remove the file only after the row is gone (a dangling row is worse
        # than a dangling file).
        storage.remove(stored_key)

    def _read_and_validate(self, upload: UploadFile, content_type: str) -> bytes:
        if content_type not in config.ATTACHMENT_ALLOWED_TYPES:
            raise ValidationError(
                f"Tipo de archivo no permitido: {content_type or 'desconocido'}. "
                "Solo se aceptan PDF, PNG o JPEG."
            )
        # Bounded read: pull at most max+1 bytes so an oversized upload is rejected
        # without ever buffering the whole body in memory.
        max_bytes = config.MAX_ATTACHMENT_MB * 1024 * 1024
        data = upload.file.read(max_bytes + 1)
        if not data:
            raise ValidationError("El archivo está vacío")
        if len(data) > max_bytes:
            raise ValidationError(
                f"El archivo supera el máximo de {config.MAX_ATTACHMENT_MB} MB"
            )
        # Verify the bytes actually match the declared type (a client can send any
        # content_type header): PDFs by magic number, images by decoding them.
        if content_type == "application/pdf" and not data.startswith(b"%PDF-"):
            raise ValidationError("El archivo no es un PDF válido")
        if content_type in _IMAGE_TYPES:
            try:
                Image.open(io.BytesIO(data)).verify()
            except (UnidentifiedImageError, OSError) as exc:
                raise ValidationError(
                    "La imagen está dañada o no es un archivo de imagen válido"
                ) from exc
        return data


def attachment_service(db: Session = Depends(get_db)) -> AttachmentService:
    """``AttachmentService`` provider for route injection."""
    return AttachmentService(db)
