import hashlib
import time
from collections import defaultdict, deque
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from studylife_mcp.metrics import RATE_LIMIT_REJECTIONS_TOTAL

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

# How often RateLimitMiddleware._maybe_prune_stale_buckets() sweeps its own _hits dict
# for keys nobody has hit inside their own window (audit A15 item 5). Without this, a
# bucket is created the first time a given key (bearer-token hash or IP) is seen and
# then NEVER removed, even once every entry inside its deque has aged out of the
# window - one process-lifetime dict entry per distinct caller ever seen, unbounded.
# Gated by monotonic time and checked lazily on the request path (this middleware has
# no background task of its own, unlike OAuthStore's run_periodic_cleanup) rather than
# swept on every single request, which would be pure overhead for a limiter this size.
RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS = 60 * 60


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
        self._last_prune = time.monotonic()

    def _maybe_prune_stale_buckets(self, now: float) -> None:
        """Age-based pruning of _hits entries nobody has hit inside their own window -
        see RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS above. A key's own deque is only
        ever trimmed when THAT key is hit again (the while-loop below), so a key that's
        never seen again keeps its now-stale entry forever without this; this instead
        does a full pass across every key, on a timer, regardless of which key (if any)
        triggered it."""
        if now - self._last_prune < RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        stale_keys = [
            k for k, hits in self._hits.items() if not hits or hits[-1] < now - self._window_seconds
        ]
        for k in stale_keys:
            del self._hits[k]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self._path:
            await self._app(scope, receive, send)
            return

        key = self._key_func(Request(scope))
        now = time.monotonic()
        self._maybe_prune_stale_buckets(now)
        hits = self._hits[key]
        while hits and hits[0] < now - self._window_seconds:
            hits.popleft()

        if len(hits) >= self._max_requests:
            RATE_LIMIT_REJECTIONS_TOTAL.labels(path=self._path).inc()
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
    can't bypass rate limiting entirely by omitting the header.

    Keys on sha256(token) rather than the raw bearer token itself (audit A15 item 5) -
    this dict lives in process memory for the middleware's whole lifetime (see
    RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS above for how long unused entries can
    linger), and a live access token is a real credential; there's no reason for it to
    also sit around in plaintext as a dict key (visible in a heap dump/debugger) when a
    stable, non-reversible digest works exactly as well for bucketing."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return f"token:{hashlib.sha256(token.encode()).hexdigest()}"
    return f"ip:{client_ip_key(request)}"
