import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.shared.exceptions import AppError, BulkValidationError
from src.shared.responses import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

# Readable code names for HTTP errors that don't originate from an ``AppError``
# (nonexistent routes, method not allowed, manual ``HTTPException``).
_HTTP_CODE_NAMES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
}


def _error_response(status_code: int, details: list[ErrorDetail]) -> JSONResponse:
    body = ErrorResponse(errors=details)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(by_alias=True, mode="json"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Registers centralized error handling with the ``{errors, meta}`` envelope.

    Covers the four sources: application/domain errors (``AppError``), request
    validation (``RequestValidationError``), framework ``HTTPException``, and
    any unhandled exception (catch-all 500).
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        detail = ErrorDetail(code=exc.code, message=exc.detail, field=exc.field)
        return _error_response(exc.status_code, [detail])

    @app.exception_handler(BulkValidationError)
    async def handle_bulk_validation_error(
        request: Request, exc: BulkValidationError
    ) -> JSONResponse:
        details = [
            ErrorDetail(code=exc.code, message=e["message"], field=e.get("field"))
            for e in exc.errors
        ]
        return _error_response(exc.status_code, details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            ErrorDetail(
                code="VALIDATION_ERROR",
                message=err["msg"],
                field=".".join(str(part) for part in err["loc"]),  # e.g. body.price
            )
            for err in exc.errors()
        ]
        return _error_response(422, details)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _HTTP_CODE_NAMES.get(exc.status_code, "HTTP_ERROR")
        detail = ErrorDetail(code=code, message=str(exc.detail))
        return _error_response(exc.status_code, [detail])

    @app.exception_handler(Exception)
    async def handle_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception")
        detail = ErrorDetail(
            code="INTERNAL_SERVER_ERROR", message="Error interno del servidor"
        )
        return _error_response(500, [detail])
