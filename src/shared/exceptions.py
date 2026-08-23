class AppError(Exception):
    """Application error translatable to an HTTP response.

    Application/domain layers raise these exceptions; the handler registered
    in ``shared.errors`` translates them into the ``{errors, meta}`` envelope
    using ``status_code`` (HTTP) and ``code`` (machine-readable).
    """

    status_code = 400
    code = "APPLICATION_ERROR"
    field: str | None = None

    def __init__(self, detail: str, *, field: str | None = None):
        self.detail = detail
        if field is not None:
            self.field = field
        super().__init__(detail)


class EntityNotFoundError(AppError):
    """The requested entity was not found."""

    status_code = 404
    code = "NOT_FOUND"

    def __init__(self, entity: str, entity_id):
        super().__init__(f"{entity} {entity_id} no encontrado")


class ConflictError(AppError):
    """A uniqueness/integrity constraint was violated."""

    status_code = 409
    code = "CONFLICT"


class AuthenticationError(AppError):
    """Missing or invalid credentials (not authenticated)."""

    status_code = 401
    code = "UNAUTHORIZED"


class AuthorizationError(AppError):
    """Authenticated but without permission for the action (insufficient role)."""

    status_code = 403
    code = "FORBIDDEN"


class BusinessRuleError(AppError):
    """A business rule was violated."""

    status_code = 422
    code = "BUSINESS_RULE_ERROR"


class ValidationError(AppError):
    """Domain validation error."""

    status_code = 422
    code = "VALIDATION_ERROR"


class ExternalServiceError(AppError):
    """A third-party system this operation depends on is unreachable or failing.

    Distinct from a 5xx of our own: nothing is wrong with this service, so the
    caller can retry once the external system recovers.
    """

    status_code = 503
    code = "EXTERNAL_SERVICE_ERROR"


class BulkValidationError(AppError):
    """Multiple row-level validation errors reported together (e.g. a bulk import).

    Unlike a plain ``AppError`` (one message), this carries every problem found
    in a single validation pass so the caller can fix all of them before
    retrying, instead of a slow fix-one-resubmit-repeat loop.
    """

    status_code = 422
    code = "VALIDATION_ERROR"

    def __init__(self, detail: str, errors: list[dict]):
        # each item: {"field": <row/context>, "message": <problem>}
        super().__init__(detail)
        self.errors = errors
