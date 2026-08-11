"""What a client is told when something breaks, and how to find out why.

Two problems, one mechanism.

An unhandled exception reaches the client as a 500. Whatever text comes with
it is written by whichever library failed, and that text has no obligation to
be safe: a driver error carries the connection string, an ORM error carries
the SQL, a path error carries the filesystem layout. None of that is a
deliberate disclosure decision, which is exactly why it should not be made by
accident.

The other problem is the opposite one — a user reporting "it failed" with no
way to find the corresponding log line. So every request carries an id,
returned in the response and stamped on the log entry. The client gets a
reference, and nothing else.

In development the detail is kept: a stack trace in the browser is worth more
than a reference number when the developer is the one reading both.
"""
import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Give every request an id, and return it on every response.

    An inbound id is accepted so a trace started at the proxy survives, but it
    is length-capped and stripped of anything that is not id-shaped: it ends up
    in log lines, and a header that reaches the log unfiltered is how a log
    file gets forged entries.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable]
    ):
        supplied = request.headers.get(REQUEST_ID_HEADER, "")
        safe = "".join(c for c in supplied if c.isalnum() or c in "-_")[:64]
        request_id = safe or uuid.uuid4().hex
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def install_error_handlers(app: FastAPI) -> None:
    app.add_middleware(RequestIDMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        # exc_info: the whole point is that the detail lives here and not in
        # the response. Losing it would trade one problem for a worse one.
        logger.exception(
            "Unhandled error on %s %s [request_id=%s]",
            request.method, request.url.path, request_id,
        )
        detail = (
            "An internal error occurred. Quote this reference when reporting it."
            if settings.is_production
            else f"{type(exc).__name__}: {exc}"
        )
        return JSONResponse(
            status_code=500,
            content={"detail": detail, "request_id": request_id},
            headers={REQUEST_ID_HEADER: request_id},
        )


__all__ = ["REQUEST_ID_HEADER", "RequestIDMiddleware", "install_error_handlers"]
