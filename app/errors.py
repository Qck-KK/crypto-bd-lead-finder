"""Error answers that show no internals (BL-16).

masumi 1.2.0 turns any exception in its routes into `HTTPException(500, detail=str(e))` (or `f"Internal server
error: {e}"`), which sends Payment Service error text, database addresses and exception messages to the caller.
Here:

- Every 5xx answer is `{"detail": <fixed text>, "error_id": <random id>}`. The id is logged with the cause's class and
  a sanitized, length-limited message, so an operator can find a buyer's report in the logs.
- The status says what failed: 503 when the Payment Service cannot be reached or is overloaded, 502 when it answered
  with something unusable, 500 otherwise. The SDK raises its HTTPException inside its `except`, so the original
  exception is reachable through `__context__`; that is how a 500 from the SDK is classified.
- Answers below 500 (400, 403, 404, 413, 422, 429, ...) keep FastAPI's normal handling, and 503/429 answers that the
  start_job gate returns itself never reach these handlers.
- `POST /start_job` with an `identifier_from_purchaser` the Payment Service would refuse gets 400 before the job gate
  and before any payment request (see app.identifiers).
"""

import json
import logging
import secrets
from typing import Iterator, Optional

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .identifiers import PurchaserIdentifierError, purchaser_identifier_problem
from .logs import sanitize

logger = logging.getLogger(__name__)


class PaymentServiceError(Exception):
    """The Payment Service answered with something the agent cannot use (HTTP 502 to the caller). The message is
    written by us and never contains the Payment Service's own text."""


class PaymentServiceUnavailable(PaymentServiceError):
    """The Payment Service could not be reached, timed out or reported a server-side failure (HTTP 503)."""


GENERIC_DETAIL = "Internal server error"
_DETAILS = {502: "Payment service error", 503: "Payment service temporarily unavailable; try again later"}
RETRY_AFTER_S = 30


def _chain(exc: Optional[BaseException], seen: Optional[set[int]] = None) -> Iterator[BaseException]:
    """`exc`, then what caused it. Exception groups (Starlette's `@app.middleware` wraps errors from the routes
    in one) are followed into their members."""
    seen = set() if seen is None else seen
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, BaseExceptionGroup):
            for member in exc.exceptions:
                yield from _chain(member, seen)
        else:
            yield exc
        exc = exc.__cause__ or exc.__context__


def classify(exc: BaseException) -> tuple[int, Optional[str], BaseException]:
    """(status, buyer-safe 4xx detail or None, root cause) for an exception that would otherwise be a 500."""
    chain = list(_chain(exc))
    for e in chain:
        if isinstance(e, PurchaserIdentifierError):
            return 400, str(e), e
        if isinstance(e, PaymentServiceUnavailable):
            return 503, None, e
        if isinstance(e, PaymentServiceError):
            return 502, None, e
    causes = [e for e in chain if not isinstance(e, StarletteHTTPException)]
    return 500, None, causes[-1] if causes else exc


def error_response(request: Request, status: int, cause: BaseException) -> JSONResponse:
    """The generic 5xx answer; logs its error id with the sanitized cause."""
    error_id = secrets.token_hex(8)
    logger.error("error_id=%s %s %s -> %d: %s: %s", error_id, request.method, request.url.path, status,
                 type(cause).__name__, sanitize(cause, 300))
    headers = {"Retry-After": str(RETRY_AFTER_S)} if status == 503 else None
    return JSONResponse(status_code=status, headers=headers,
                        content={"detail": _DETAILS.get(status, GENERIC_DETAIL), "error_id": error_id})


async def _http_exception(request: Request, exc: StarletteHTTPException):
    if exc.status_code < 500:
        return await http_exception_handler(request, exc)
    status, detail, cause = classify(exc)
    if detail is not None:  # a buyer error that surfaced as a 500 (e.g. the identifier check in V2Payment)
        return JSONResponse(status_code=status, content={"detail": detail})
    if status == 500:
        status = exc.status_code  # keep 501 and others the route chose
    return error_response(request, status, cause)


async def _unhandled_exception(request: Request, exc: Exception):
    status, detail, cause = classify(exc)
    if detail is not None:
        return JSONResponse(status_code=status, content={"detail": detail})
    return error_response(request, status, cause)


class PurchaserIdentifierMiddleware:
    """400 for `POST /start_job` whose `identifier_from_purchaser` the Payment Service would refuse. Pure ASGI: the
    body is read once and replayed unchanged to the app. A body that is not a JSON object, or has no identifier,
    goes on to the SDK, which answers it as before (422/400)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != "/start_job":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        problem = _identifier_problem(body)
        if problem:
            await JSONResponse(status_code=400, content={"detail": problem})(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _identifier_problem(body: bytes) -> Optional[str]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or "identifier_from_purchaser" not in data:
        return None
    return purchaser_identifier_problem(data["identifier_from_purchaser"])


def install_error_handling(app: FastAPI) -> None:
    """Generic 5xx answers for every route, and the /start_job identifier check. Call after every other middleware
    has been added, so the identifier check runs first (Starlette puts the last-added middleware outermost)."""
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(Exception, _unhandled_exception)
    app.add_middleware(PurchaserIdentifierMiddleware)
