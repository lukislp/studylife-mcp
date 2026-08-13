import time
from collections import defaultdict, deque

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# RFC 7591 dynamic client registration is intentionally unauthenticated (any MCP
# client self-registers without prior credentials) - once this server is publicly
# reachable, that endpoint is the one an anonymous scanner/bot can hit repeatedly
# for free. This caps it per source IP; UNUSED_CLIENT_TTL_SECONDS in oauth_store.py
# bounds the resulting storage growth for whatever gets through.
REGISTRATION_RATE_LIMIT_MAX_REQUESTS = 5
REGISTRATION_RATE_LIMIT_WINDOW_SECONDS = 60 * 60


class RegistrationRateLimitMiddleware:
    """Per-IP fixed-window rate limit for a single path (the OAuth registration
    endpoint). In-memory only - this deployment runs a single replica (see
    k8s/04-app.yaml's `strategy: Recreate`), so there's no cross-instance state to
    share, and a redeploy resetting the counters is an acceptable trade-off at this
    traffic scale. Not a security boundary on its own: the client IP is read from
    X-Forwarded-For when present, which a caller could in principle spoof - this
    only needs to slow down casual scanning, not resist a targeted attacker, since
    real StudyLife data access still requires a valid StudyLife API key regardless.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        max_requests: int = REGISTRATION_RATE_LIMIT_MAX_REQUESTS,
        window_seconds: float = REGISTRATION_RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._app = app
        self._path = path
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self._path:
            await self._app(scope, receive, send)
            return

        client_ip = _client_ip(Request(scope))
        now = time.monotonic()
        hits = self._hits[client_ip]
        while hits and hits[0] < now - self._window_seconds:
            hits.popleft()

        if len(hits) >= self._max_requests:
            response = PlainTextResponse(
                "Too many registration attempts, try again later.", status_code=429
            )
            await response(scope, receive, send)
            return

        hits.append(now)
        await self._app(scope, receive, send)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
