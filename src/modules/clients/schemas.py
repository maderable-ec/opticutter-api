from typing import List, Optional

from pydantic import Field, field_validator

from src.modules.clients.tax_id import identifier_error
from src.shared.schemas import CamelModel


def _validate_identifier(value: Optional[str]) -> Optional[str]:
    """Rejects a mistyped cédula/RUC, accepts a foreign document.

    An all-digits identifier is held to the check-digit algorithm; anything
    with a letter is taken as a passport and accepted as typed. Nine digits is
    a mistyped cédula, not a passport, so it still fails.

    Only a create or an update runs this — clients already stored keep whatever
    they have until someone edits them.
    """
    if value is None:
        return value
    cleaned = value.strip()
    error = identifier_error(cleaned)
    if error:
        raise ValueError(error)
    return cleaned


class ClientFields(CamelModel):
    """The client's columns, with no validation beyond the column widths.

    Split out from ``ClientBase`` so ``ClientResponse`` can inherit the shape
    WITHOUT the cédula check: a row stored before the rule existed (or by an
    older client) must still be readable, and ``response_model`` validation runs
    on the way out. Only what a caller *sends* is validated.
    """

    identifier: str = Field(
        ..., min_length=1, max_length=32, description="Client external identifier"
    )
    first_name: Optional[str] = Field(
        None, max_length=64, description="Client first name"
    )
    last_name: Optional[str] = Field(
        None, max_length=64, description="Client last name"
    )
    source: Optional[str] = Field(
        None, max_length=64, description="Client source (e.g. instagram, referral)"
    )
    phone: Optional[str] = Field(
        None, max_length=32, description="Client mobile phone (celular)"
    )
    email: Optional[str] = Field(
        None, max_length=128, description="Client email (optional)"
    )


class ClientBase(ClientFields):
    """The shape a caller sends: same fields, cédula/RUC enforced."""

    _check_identifier = field_validator("identifier")(_validate_identifier)


class ClientCreate(ClientBase):
    """Schema for creating a new client."""


class ClientUpdate(CamelModel):
    """Schema for updating an existing client."""

    identifier: Optional[str] = Field(
        None, min_length=1, max_length=32, description="Client external identifier"
    )
    first_name: Optional[str] = Field(
        None, max_length=64, description="Client first name"
    )
    last_name: Optional[str] = Field(
        None, max_length=64, description="Client last name"
    )
    source: Optional[str] = Field(
        None, max_length=64, description="Client source (e.g. instagram, referral)"
    )
    phone: Optional[str] = Field(
        None, max_length=32, description="Client mobile phone (celular)"
    )
    email: Optional[str] = Field(
        None, max_length=128, description="Client email (optional)"
    )

    _check_identifier = field_validator("identifier")(_validate_identifier)


class ClientResponse(ClientFields):
    """Schema for client responses.

    Inherits ``ClientFields``, not ``ClientBase``: see that docstring for why a
    response must not re-run the cédula check.
    """

    id: int = Field(..., description="Client ID")


class ClientSyncIssue(CamelModel):
    """One source row the sync has something to report about.

    Same shape for both severities — ``ClientSyncResult.issues`` (the row was
    skipped) and ``.warnings`` (the row was imported with a field dropped). The
    severity is the list it lands in, not the payload.

    Carries the vendor's cédula and name, because fixing it means finding that
    client in the external system.
    """

    code: str
    name: str
    message: str


class ClientSyncResult(CamelModel):
    """Summary of a sync against the external client system."""

    created: int
    updated: int
    # Rows whose data the sync could not use: no cédula, no name, a duplicate,
    # or a cédula/RUC that fails the check digit. Skipped, never fatal — the
    # source is a live database and a bad row can't be fixed and re-uploaded
    # before every run. See `issues`.
    skipped_invalid: int = 0
    # Rows the source has taken out of service (est != 1). Reported rather than
    # silently dropped, so a shrinking import has an explanation.
    skipped_inactive: int = 0
    issues: List[ClientSyncIssue] = []
    # Rows that WERE imported but with a field dropped: an unusable phone, a
    # malformed e-mail, a name too long for the column. Nothing was skipped —
    # it's reported because a client with no phone can't be quoted
    # (`clients.service.require_phone`) and nobody would know why.
    warnings: List[ClientSyncIssue] = []
    # True when nothing was written — the pass ran and rolled back.
    dry_run: bool = False
