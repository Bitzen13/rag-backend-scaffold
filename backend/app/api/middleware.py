"""One structured log line per request, and the request id that ties it to everything else.

What is logged is deliberately narrow: method, path, status, duration, and the *id* of the key
that made the call. Not the question, not the answer, not the key. The question is user data and
belongs in `query_logs` where a deployment can turn it off (see services/usage.py); the key is a
credential and belongs nowhere but the caller's own configuration. `observability.redact` is the
backstop if something slips through anyway, but the first line of defence is not logging it.

The request id is generated here and returned as `X-Request-ID`, so an operator handed one by a
consumer can find the request in the log without knowing anything else about it.

The rate-limit headers are applied here too, rather than in the route. A route only shapes its own
success: an upstream failure or a validation error is turned into a response by an exception
handler, which would drop headers the route had set. A caller being told to slow down should not
depend on its request having succeeded.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("app.api.request")

REQUEST_ID_HEADER = "X-Request-ID"

NextCall = Callable[[Request], Awaitable[Response]]


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Logs every request once it has an outcome, whatever that outcome is."""

    async def dispatch(self, request: Request, call_next: NextCall) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        # Set before the route runs: a dependency (authorize) fills it in, and an exception
        # raised before that still leaves a line saying which key was not yet known.
        request.state.api_key_id = None
        # Filled in by the `authorize` dependency, when the request gets that far.
        request.state.rate_limit_headers = {}
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            self._log(request, request_id, 500, started, failed=True)
            raise
        response.headers[REQUEST_ID_HEADER] = request_id
        # `setdefault`: a 429 already carries these from the error it raised with.
        for name, value in getattr(request.state, "rate_limit_headers", {}).items():
            response.headers.setdefault(name, value)
        self._log(request, request_id, response.status_code, started)
        return response

    def _log(
        self,
        request: Request,
        request_id: str,
        status_code: int,
        started: float,
        *,
        failed: bool = False,
    ) -> None:
        fields = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": status_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "api_key_id": str(getattr(request.state, "api_key_id", None) or "-"),
        }
        if failed:
            logger.error("request failed", extra=fields, exc_info=True)
        else:
            logger.log(
                logging.WARNING if status_code >= 500 else logging.INFO, "request", extra=fields
            )
