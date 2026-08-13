import time
from collections import defaultdict, deque
from collections.abc import Callable

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

# /mcp is already authenticated (a valid Bearer access token is required by the SDK's own
# auth middleware before a tool call runs at all) - this isn't about anonymous abuse, it's
# about bounding a legitimate-but-buggy or compromised client hammering the server (a
# runaway loop, a misbehaving retry). 300/hour is generous headroom over realistic usage
# (StudyLife's tools are a handful of calls per conversation turn) while still capping a
# genuine flood; per-token rather than per-IP since identity already exists here and is a
# more correct bucket than an IP that could be shared across StudyLife users.
MCP_RATE_LIMIT_MAX_REQUESTS = 300
MCP_RATE_LIMIT_WINDOW_SECONDS = 60 * 60


class RateLimitMiddleware:
    """Per-key fixed-window rate limit for a single path. In-memory only - this
    deployment runs a single replica (see k8s/04-app.yaml's `strategy: Recreate`), so
    there's no cross-instance state to share, and a redeploy resetting the counters is
    an acceptable trade-off at this traffic scale. Not a security boundary on its own
    for IP-keyed limits (a caller could in principle spoof X-Forwarded-For) - this only
    needs to slow down casual abuse, not resist a targeted attacker, since real
    StudyLife data access still requires a valid StudyLife API key regardless.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        key_func: Callable[[Request], str],
        max_requests: int,
        window_seconds: float,
        rejection_message: str,
    ) -> None:
        self._app = app
        self._path = path
        self._key_func = key_func
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._rejection_message = rejection_message
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self._path:
            await self._app(scope, receive, send)
            return

        key = self._key_func(Request(scope))
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] < now - self._window_seconds:
            hits.popleft()

        if len(hits) >= self._max_requests:
            response = PlainTextResponse(self._rejection_message, status_code=429)
            await response(scope, receive, send)
            return

        hits.append(now)
        await self._app(scope, receive, send)


class RegistrationRateLimitMiddleware(RateLimitMiddleware):
    """Per-IP registration limit - see REGISTRATION_RATE_LIMIT_* above."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        max_requests: int = REGISTRATION_RATE_LIMIT_MAX_REQUESTS,
        window_seconds: float = REGISTRATION_RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        super().__init__(
            app,
            path=path,
            key_func=client_ip_key,
            max_requests=max_requests,
            window_seconds=window_seconds,
            rejection_message="Too many registration attempts, try again later.",
        )


class McpCallRateLimitMiddleware(RateLimitMiddleware):
    """Per-token /mcp call limit - see MCP_RATE_LIMIT_* above."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        max_requests: int = MCP_RATE_LIMIT_MAX_REQUESTS,
        window_seconds: float = MCP_RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        super().__init__(
            app,
            path=path,
            key_func=bearer_token_key,
            max_requests=max_requests,
            window_seconds=window_seconds,
            rejection_message="Too many requests, try again later.",
        )


def client_ip_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def bearer_token_key(request: Request) -> str:
    """Falls back to the IP for a request with no (or a malformed) Authorization
    header - that request is going to be rejected as unauthenticated by the SDK's own
    auth middleware regardless, but it still needs *some* bucket so a flood of those
    can't bypass rate limiting entirely by omitting the header."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return f"token:{auth[7:].strip()}"
    return f"ip:{client_ip_key(request)}"
