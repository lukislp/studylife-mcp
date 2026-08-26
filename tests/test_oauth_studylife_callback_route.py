from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from studylife_mcp.config import Settings
from studylife_mcp.oauth_provider import SCOPE, StudyLifeOAuthProvider, register_oauth_routes
from studylife_mcp.oauth_store import OAuthStore

CLIENT = OAuthClientInformationFull(
    client_id="client-1",
    redirect_uris=[AnyUrl("https://client.example/callback")],
)

EXCHANGE_URL = "https://studylife.example.test/api/auth/mcp-assertion-exchange"


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
    await s.register_client(CLIENT)
    return s


@pytest.fixture
async def app_client(store: OAuthStore, settings: Settings):
    provider = StudyLifeOAuthProvider(
        store, "https://mcp.example.test", "https://connect.studylife.example.test"
    )
    mcp = MCPServer(
        "studylife-mcp-test",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyUrl("https://mcp.example.test"),
            resource_server_url=AnyUrl("https://mcp.example.test"),
            client_registration_options=ClientRegistrationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )
    register_oauth_routes(mcp, store, settings)
    app = mcp.streamable_http_app()
    transport = httpx.ASGITransport(app=app)
    base_url = "https://mcp.example.test"
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        yield client


async def _start_authorization(provider_store: OAuthStore, base_url: str) -> str:
    """Mirrors what authorize() does - returns the state (our own pending-authorization
    request_id) a real caller would get back from the redirect to StudyLife's
    /connect/mcp, then would see echoed on our /auth/studylife/callback."""
    provider = StudyLifeOAuthProvider(
        provider_store, base_url, "https://connect.studylife.example.test"
    )
    params = AuthorizationParams(
        state="xyz",
        scopes=[SCOPE],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://client.example/callback"),
        redirect_uri_provided_explicitly=True,
    )
    url = await provider.authorize(CLIENT, params)
    return parse_qs(urlparse(url).query)["state"][0]


async def test_get_callback_unknown_state_returns_400(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get(
        "/auth/studylife/callback", params={"assertion": "a", "state": "does-not-exist"}
    )
    assert response.status_code == 400


async def test_get_callback_missing_assertion_returns_400(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    state = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.get("/auth/studylife/callback", params={"state": state})

    assert response.status_code == 400
    # No pending authorization is consumed - only a successful exchange consumes it.
    assert await store.load_pending_authorization(state) is not None


@respx.mock
async def test_callback_happy_path_exchanges_assertion_and_redirects_with_code(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    respx.post(EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"userId": 42, "mcpApiKey": "rotated-mcp-key"})
    )
    state = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.get(
        "/auth/studylife/callback",
        params={"assertion": "single-use-assertion", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 302
    redirect = urlparse(response.headers["location"])
    assert redirect.scheme == "https"
    assert redirect.netloc == "client.example"
    query = parse_qs(redirect.query)
    assert "code" in query
    assert query["state"] == ["xyz"]  # the ORIGINAL client's OAuth state, not our own

    # The pending authorization is consumed; re-submitting the same state fails.
    assert await store.load_pending_authorization(state) is None

    # Subject is the real StudyLife user id, not a hash of the key (audit A1).
    assert await store.load_user_key("42") == "rotated-mcp-key"


@respx.mock
async def test_callback_reconnect_upserts_key_for_same_subject(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    """A second connect for the same StudyLife user replaces the stored key rather
    than erroring or creating a second identity - required so rotation never orphans
    an existing grant (identity-contract-v1)."""
    respx.post(EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"userId": 42, "mcpApiKey": "first-key"})
    )
    first_state = await _start_authorization(store, "https://mcp.example.test")
    await app_client.get(
        "/auth/studylife/callback",
        params={"assertion": "first-assertion", "state": first_state},
        follow_redirects=False,
    )

    respx.post(EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"userId": 42, "mcpApiKey": "second-key"})
    )
    second_state = await _start_authorization(store, "https://mcp.example.test")
    response = await app_client.get(
        "/auth/studylife/callback",
        params={"assertion": "second-assertion", "state": second_state},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert await store.load_user_key("42") == "second-key"


@respx.mock
async def test_callback_exchange_rejected_shows_generic_error_without_key_material(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    respx.post(EXCHANGE_URL).mock(return_value=httpx.Response(401))
    state = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.get(
        "/auth/studylife/callback",
        params={"assertion": "expired-or-unknown-assertion", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "expired-or-unknown-assertion" not in response.text
    # Not consumed - a fresh attempt from the MCP client can still reuse this state.
    assert await store.load_pending_authorization(state) is not None
    assert await store.load_user_key("42") is None


@respx.mock
async def test_callback_exchange_network_error_shows_generic_error(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    respx.post(EXCHANGE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    state = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.get(
        "/auth/studylife/callback",
        params={"assertion": "some-assertion", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert await store.load_pending_authorization(state) is not None
