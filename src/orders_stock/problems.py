"""RFC 9457 problem details: the one error shape of the HTTP API (specs/04-api.md, "Errors")."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

PROBLEM_TYPE_BASE = "https://github.com/mbutata/orders-stock-service/blob/main/specs/04-api.md#"
PROBLEM_MEDIA_TYPE = "application/problem+json"
RETRY_AFTER_SECONDS = 1

VALIDATION_ERROR = "validation_error"
UNKNOWN_SKU = "unknown_sku"
ORDER_REF_CONFLICT = "order_ref_conflict"
ORDER_NOT_FOUND = "order_not_found"
SKU_NOT_FOUND = "sku_not_found"
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
INTERNAL_ERROR = "internal_error"
SERVICE_UNAVAILABLE = "service_unavailable"

# code -> (status, title); titles are constant per code.
PROBLEM_CODES: dict[str, tuple[int, str]] = {
    VALIDATION_ERROR: (422, "Request validation failed"),
    UNKNOWN_SKU: (422, "Unknown SKU"),
    ORDER_REF_CONFLICT: (409, "order_ref already used with different content"),
    ORDER_NOT_FOUND: (404, "Order not found"),
    SKU_NOT_FOUND: (404, "SKU not found"),
    NOT_FOUND: (404, "Resource not found"),
    METHOD_NOT_ALLOWED: (405, "Method not allowed"),
    INTERNAL_ERROR: (500, "Internal server error"),
    SERVICE_UNAVAILABLE: (503, "Service unavailable"),
}

SCHEMA_MISMATCH_DETAIL = "The request does not match the schema."


@dataclass(frozen=True)
class ErrorLocation:
    """One entry of a problem's `errors`: exactly one of `pointer` or `parameter` is set."""

    detail: str
    pointer: str | None = None
    parameter: str | None = None

    def to_json(self) -> dict[str, str]:
        entry = {"detail": self.detail}
        if self.pointer is not None:
            entry["pointer"] = self.pointer
        else:
            entry["parameter"] = self.parameter or ""
        return entry


class ProblemError(Exception):
    """Raised anywhere in request handling to produce a problem response."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        errors: Sequence[ErrorLocation] = (),
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors = tuple(errors)
        self.headers = headers or {}


def json_pointer(path: Sequence[str | int]) -> str:
    """An RFC 6901 JSON Pointer in URI fragment form, for example `#/items/0/qty`."""
    tokens = (str(token).replace("~", "~0").replace("/", "~1") for token in path)
    return "#" + "".join("/" + quote(token, safe="~-._!$&'()*+,;=:@") for token in tokens)


def problem_response(
    code: str,
    detail: str,
    *,
    errors: Sequence[ErrorLocation] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    status, title = PROBLEM_CODES[code]
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + code,
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
    }
    if errors:
        body["errors"] = [error.to_json() for error in errors]
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_MEDIA_TYPE)


def validation_locations(error: RequestValidationError) -> list[ErrorLocation]:
    """Translate the framework's validation errors into problem `errors` entries."""
    locations = []
    for item in error.errors():
        where, *path = item["loc"]
        message = str(item["msg"])
        if where == "query":
            locations.append(ErrorLocation(message, parameter=str(path[0])))
        elif item["type"] == "json_invalid":
            locations.append(ErrorLocation(message, pointer="#"))
        else:
            locations.append(ErrorLocation(message, pointer=json_pointer(path)))
    return locations


def _handle_problem(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return problem_response(exc.code, exc.detail, errors=exc.errors, headers=exc.headers)


def _handle_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return problem_response(
        VALIDATION_ERROR, SCHEMA_MISMATCH_DETAIL, errors=validation_locations(exc)
    )


def _handle_http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    # FastAPI raises 400 for a body it cannot decode, such as Latin-1 text;
    # the contract treats that like a body that is not JSON.
    if exc.status_code == 400:
        return problem_response(
            VALIDATION_ERROR,
            SCHEMA_MISMATCH_DETAIL,
            errors=[ErrorLocation(str(exc.detail), pointer="#")],
        )
    if exc.status_code == 404:
        return problem_response(NOT_FOUND, f"No route matches {request.url.path}.")
    if exc.status_code == 405:
        return problem_response(
            METHOD_NOT_ALLOWED,
            f"{request.method} is not allowed on {request.url.path}.",
            headers=exc.headers,
        )
    logger.error("unexpected HTTP exception %s: %s", exc.status_code, exc.detail)
    return problem_response(INTERNAL_ERROR, "The server failed to complete the request.")


def _handle_database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("database unavailable: %s", exc)
    return problem_response(
        SERVICE_UNAVAILABLE,
        "The database is not reachable. Retry later.",
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # The server error middleware re-raises after this handler, so the traceback is logged.
    return problem_response(INTERNAL_ERROR, "The server failed to complete the request.")


def install_problem_handlers(app: FastAPI) -> None:
    """Make every error response, including the framework's own, a problem document."""
    app.add_exception_handler(ProblemError, _handle_problem)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http)
    # OperationalError covers connection failures, pool timeouts and statement timeouts.
    app.add_exception_handler(psycopg.OperationalError, _handle_database_unavailable)
    app.add_exception_handler(Exception, _handle_unexpected)
