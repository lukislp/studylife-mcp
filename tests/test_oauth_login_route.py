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


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
    await s.register_client(CLIENT)
    return s


@pytest.fixture
async def app_client(store: OAuthStore, settings: Settings):
    provider = StudyLifeOAuthProvider(store, "https://mcp.example.test")
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
    provider = StudyLifeOAuthProvider(provider_store, base_url)
    params = AuthorizationParams(
        state="xyz",
        scopes=[SCOPE],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://client.example/callback"),
        redirect_uri_provided_explicitly=True,
    )
    url = await provider.authorize(CLIENT, params)
    return url.split("request_id=")[1]


async def test_get_login_unknown_request_id_returns_400(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/login", params={"request_id": "does-not-exist"})
    assert response.status_code == 400


async def test_get_login_renders_form_for_valid_request(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    request_id = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.get("/login", params={"request_id": request_id})

    assert response.status_code == 200
    assert request_id in response.text
    assert "api_key" in response.text


@respx.mock
async def test_post_login_valid_key_redirects_with_code(
    app_client: httpx.AsyncClient, store: OAuthStore, settings: Settings
) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(
        return_value=httpx.Response(200, json=[])
    )
    request_id = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.post(
        "/login",
        data={"request_id": request_id, "api_key": "a-real-studylife-key"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    redirect = urlparse(response.headers["location"])
    assert redirect.scheme == "https"
    assert redirect.netloc == "client.example"
    query = parse_qs(redirect.query)
    assert "code" in query
    assert query["state"] == ["xyz"]

    # The pending authorization is consumed; re-submitting the same request_id fails.
    assert await store.load_pending_authorization(request_id) is None

    subject = store.subject_for_key("a-real-studylife-key")
    assert await store.load_user_key(subject) == "a-real-studylife-key"


@respx.mock
async def test_post_login_rejected_key_reshows_form_with_error(
    app_client: httpx.AsyncClient, store: OAuthStore, settings: Settings
) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(return_value=httpx.Response(401))
    request_id = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.post(
        "/login",
        data={"request_id": request_id, "api_key": "a-bad-key"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "rejected" in response.text.lower()
    # Not consumed - the same link can be retried with a correct key.
    assert await store.load_pending_authorization(request_id) is not None


async def test_post_login_missing_key_reshows_form(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    request_id = await _start_authorization(store, "https://mcp.example.test")

    response = await app_client.post(
        "/login", data={"request_id": request_id, "api_key": ""}, follow_redirects=False
    )

    assert response.status_code == 400
    assert await store.load_pending_authorization(request_id) is not None
