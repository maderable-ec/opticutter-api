from datetime import datetime, timezone
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    field_serializer,
)
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base schema: camelCase in the API contract, snake_case internally."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,  # also accepts snake_case input
        from_attributes=True,  # allows building responses from ORM models
    )

    @field_serializer("*", mode="wrap", when_used="json")
    def _utc_aware(self, value: Any, handler: SerializerFunctionWrapHandler) -> Any:
        """Stamps the UTC offset on naive datetimes, on the way out only.

        Every timestamp in this codebase is ``datetime.utcnow()`` written into a
        naive ``DateTime`` column, and a naive ISO string has no time zone for a
        client to apply: ``new Date("2026-09-07T15:00:00")`` in the browser reads
        it as LOCAL time, which in Ecuador (UTC-5) silently moves it five hours.
        Saying "UTC" on the wire is what makes the client's own arithmetic right.

        ``mode="wrap"`` is not optional: a plain serializer would return nested
        models untouched and pydantic would fall back to its generic serializer
        for them, dropping the ``alias_generator`` -- i.e. breaking camelCase in
        silence. ``when_used="json"`` keeps ``model_dump()`` in python mode
        byte-identical, which matters because the optimization hash and the
        stored pre-order/order payloads are built from such dumps.
        """
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return handler(value)
