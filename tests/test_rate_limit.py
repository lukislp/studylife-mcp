import hashlib

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from studylife_mcp.rate_limit import (
    McpCallRateLimitMiddleware,
    RegistrationRateLimitMiddleware,
    bearer_token_key,
)


def _make_app(*, max_requests: int, window_seconds: float) -> Starlette:
    async def register(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    async def other(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("other ok")

    app = Starlette(
        routes=[
            Route("/register", register, methods=["POST"]),
            Route("/other", other, methods=["POST"]),
        ]
    )
    app.add_middleware(
        RegistrationRateLimitMiddleware,
        path="/register",
        max_requests=max_requests,
        window_seconds=window_seconds,
    )
    return app


def _make_mcp_app(*, max_requests: int, window_seconds: float) -> Starlette:
    async def mcp(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
    app.add_middleware(
        McpCallRateLimitMiddleware,
        path="/mcp",
        max_requests=max_requests,
        window_seconds=window_seconds,
    )
    return app


async def _client(app: Starlette) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="https://mcp.example.test")


async def test_requests_within_limit_succeed() -> None:
    app = _make_app(max_requests=3, window_seconds=3600)
    async with await _client(app) as client:
        for _ in range(3):
            response = await client.post("/register")
            assert response.status_code == 200


async def test_request_over_limit_is_rejected() -> None:
    app = _make_app(max_requests=3, window_seconds=3600)
    async with await _client(app) as client:
        for _ in range(3):
            await client.post("/register")
        response = await client.post("/register")
        assert response.status_code == 429


async def test_rejection_increments_metric() -> None:
    from prometheus_client.parser import text_string_to_metric_families

    from studylife_mcp.metrics import render_latest

    def rejections_for_register() -> float:
        body, _content_type = render_latest()
        for family in text_string_to_metric_families(body.decode()):
            for sample in family.samples:
                if sample.name == "studylife_mcp_rate_limit_rejections_total" and (
                    sample.labels == {"path": "/register"}
                ):
                    return sample.value
        return 0

    app = _make_app(max_requests=1, window_seconds=3600)
    before = rejections_for_register()
    async with await _client(app) as client:
        await client.post("/register")
        await client.post("/register")
    after = rejections_for_register()

    assert after == before + 1


async def test_different_ips_have_independent_limits() -> None:
    app = _make_app(max_requests=1, window_seconds=3600)
    async with await _client(app) as client:
        first = await client.post("/register", headers={"x-forwarded-for": "1.1.1.1"})
        second = await client.post("/register", headers={"x-forwarded-for": "2.2.2.2"})
        assert first.status_code == 200
        assert second.status_code == 200


async def test_same_ip_via_x_forwarded_for_is_rate_limited_together() -> None:
    app = _make_app(max_requests=1, window_seconds=3600)
    async with await _client(app) as client:
        first = await client.post("/register", headers={"x-forwarded-for": "3.3.3.3"})
        second = await client.post("/register", headers={"x-forwarded-for": "3.3.3.3"})
        assert first.status_code == 200
        assert second.status_code == 429


async def test_unrelated_paths_are_not_rate_limited() -> None:
    app = _make_app(max_requests=1, window_seconds=3600)
    async with await _client(app) as client:
        for _ in range(5):
            response = await client.post("/other")
            assert response.status_code == 200


async def test_mcp_same_token_is_rate_limited_together() -> None:
    app = _make_mcp_app(max_requests=1, window_seconds=3600)
    async with await _client(app) as client:
        headers = {"authorization": "Bearer token-abc"}
        first = await client.post("/mcp", headers=headers)
        second = await client.post("/mcp", headers=headers)
        assert first.status_code == 200
        assert second.status_code == 429


async def test_mcp_different_tokens_have_independent_limits() -> None:
    app = _make_mcp_app(max_requests=1, window_seconds=3600)
    async with await _client(app) as client:
        first = await client.post("/mcp", headers={"authorization": "Bearer token-a"})
        second = await client.post("/mcp", headers={"authorization": "Bearer token-b"})
        assert first.status_code == 200
        assert second.status_code == 200


async def test_mcp_requests_without_bearer_token_fall_back_to_ip() -> None:
    app = _make_mcp_app(max_requests=1, window_seconds=3600)
    async with await _client(app) as client:
        first = await client.post("/mcp", headers={"x-forwarded-for": "5.5.5.5"})
        second = await client.post("/mcp", headers={"x-forwarded-for": "5.5.5.5"})
        assert first.status_code == 200
        assert second.status_code == 429


def _request_with_bearer_token(token: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    return Request(scope)


def test_bearer_token_key_does_not_contain_the_raw_token() -> None:
    key = bearer_token_key(_request_with_bearer_token("super-secret-access-token"))

    assert "super-secret-access-token" not in key
    assert key.startswith("token:")
    assert key == f"token:{hashlib.sha256(b'super-secret-access-token').hexdigest()}"


def test_bearer_token_key_is_deterministic_and_distinguishes_tokens() -> None:
    key_a1 = bearer_token_key(_request_with_bearer_token("token-a"))
    key_a2 = bearer_token_key(_request_with_bearer_token("token-a"))
    key_b = bearer_token_key(_request_with_bearer_token("token-b"))

    assert key_a1 == key_a2
    assert key_a1 != key_b


async def test_stale_buckets_are_pruned_after_window_and_prune_interval_elapse() -> None:
    """audit A15 item 5: a bucket for a key nobody has hit again just sits in _hits
    forever, since a key's own deque is only trimmed when that same key is hit again.
    Force pruning to run on every call (interval=0) and give the first key's single
    hit time to age out of its own (tiny) window, then confirm a request from a
    different key sweeps it away instead of leaving it to grow unbounded."""
    import studylife_mcp.rate_limit as rate_limit_module

    async def inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
        response = PlainTextResponse("ok")
        await response(scope, receive, send)

    original_interval = rate_limit_module.RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS
    rate_limit_module.RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS = 0
    try:
        middleware = McpCallRateLimitMiddleware(
            inner_app, path="/mcp", max_requests=10, window_seconds=0.05
        )
        transport = httpx.ASGITransport(app=middleware)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as client:
            await client.post("/mcp", headers={"authorization": "Bearer token-a"})
            assert len(middleware._hits) == 1

            await anyio.sleep(0.1)  # let token-a's bucket age out of its own window

            await client.post("/mcp", headers={"authorization": "Bearer token-b"})

        stale_key = bearer_token_key(_request_with_bearer_token("token-a"))
        fresh_key = bearer_token_key(_request_with_bearer_token("token-b"))
        assert stale_key not in middleware._hits
        assert fresh_key in middleware._hits
        assert len(middleware._hits) == 1
    finally:
        rate_limit_module.RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS = original_interval


async def test_pruning_does_not_remove_buckets_still_within_their_window() -> None:
    import studylife_mcp.rate_limit as rate_limit_module

    async def inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
        response = PlainTextResponse("ok")
        await response(scope, receive, send)

    original_interval = rate_limit_module.RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS
    rate_limit_module.RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS = 0
    try:
        middleware = McpCallRateLimitMiddleware(
            inner_app, path="/mcp", max_requests=10, window_seconds=3600
        )
        transport = httpx.ASGITransport(app=middleware)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as client:
            await client.post("/mcp", headers={"authorization": "Bearer token-a"})
            await client.post("/mcp", headers={"authorization": "Bearer token-b"})

        assert len(middleware._hits) == 2
    finally:
        rate_limit_module.RATE_LIMIT_BUCKET_PRUNE_INTERVAL_SECONDS = original_interval
