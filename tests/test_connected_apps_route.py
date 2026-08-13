from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from studylife_mcp.config import Settings
from studylife_mcp.oauth_provider import SCOPE, StudyLifeOAuthProvider, register_oauth_routes
from studylife_mcp.oauth_store import OAuthStore

API_KEY = "a-real-studylife-key"


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
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


async def _connect_client(store: OAuthStore, *, client_id: str, client_name: str) -> str:
    """Directly seeds a registered+authorized client for API_KEY's subject - the login
    flow itself is covered by test_oauth_login_route.py, this file is about
    /connected-apps behavior once a connection already exists."""
    subject = store.subject_for_key(API_KEY)
    await store.save_user_key(subject, API_KEY)
    await store.register_client(
        OAuthClientInformationFull(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )
    await store.save_access_token(
        AccessToken(
            token=f"access-{client_id}",
            client_id=client_id,
            scopes=[SCOPE],
            expires_at=9999999999,
            subject=subject,
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token=f"refresh-{client_id}",
            client_id=client_id,
            scopes=[SCOPE],
            expires_at=9999999999,
            subject=subject,
        )
    )
    return subject


async def test_get_without_session_shows_key_form(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/connected-apps")

    assert response.status_code == 200
    assert "api_key" in response.text


async def test_get_with_unknown_session_shows_key_form(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/connected-apps", params={"session_id": "does-not-exist"})

    assert response.status_code == 200
    assert "api_key" in response.text


async def test_post_missing_key_reshows_form(app_client: httpx.AsyncClient) -> None:
    response = await app_client.post("/connected-apps", data={"api_key": ""})

    assert response.status_code == 400
    assert "api_key" in response.text


@respx.mock
async def test_post_rejected_key_reshows_form_with_error(
    app_client: httpx.AsyncClient, settings: Settings
) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(return_value=httpx.Response(401))

    response = await app_client.post("/connected-apps", data={"api_key": "a-bad-key"})

    assert response.status_code == 401
    assert "rejected" in response.text.lower()


@respx.mock
async def test_post_valid_key_redirects_to_list_with_session(
    app_client: httpx.AsyncClient, settings: Settings
) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(
        return_value=httpx.Response(200, json=[])
    )

    response = await app_client.post(
        "/connected-apps", data={"api_key": API_KEY}, follow_redirects=False
    )

    assert response.status_code == 303
    redirect = urlparse(response.headers["location"])
    assert redirect.path == "/connected-apps"
    assert "session_id" in parse_qs(redirect.query)


@respx.mock
async def test_list_shows_connected_client_and_no_others(
    app_client: httpx.AsyncClient, store: OAuthStore, settings: Settings
) -> None:
    await _connect_client(store, client_id="claude", client_name="Claude")
    respx.get("https://studylife.example.test/api/courses").mock(
        return_value=httpx.Response(200, json=[])
    )
    login = await app_client.post(
        "/connected-apps", data={"api_key": API_KEY}, follow_redirects=False
    )
    session_id = parse_qs(urlparse(login.headers["location"]).query)["session_id"][0]

    response = await app_client.get("/connected-apps", params={"session_id": session_id})

    assert response.status_code == 200
    assert "Claude" in response.text


async def test_list_empty_state_when_nothing_connected(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    subject = store.subject_for_key(API_KEY)
    session_id = await store.create_management_session(subject)

    response = await app_client.get("/connected-apps", params={"session_id": session_id})

    assert response.status_code == 200
    assert "no apps" in response.text.lower()


async def test_revoke_without_valid_session_shows_key_form(
    app_client: httpx.AsyncClient,
) -> None:
    response = await app_client.post(
        "/connected-apps/revoke",
        data={"session_id": "does-not-exist", "client_id": "claude"},
    )

    assert response.status_code == 400
    assert "api_key" in response.text


async def test_revoke_deletes_tokens_and_removes_from_list(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    subject = await _connect_client(store, client_id="claude", client_name="Claude")
    session_id = await store.create_management_session(subject)

    response = await app_client.post(
        "/connected-apps/revoke",
        data={"session_id": session_id, "client_id": "claude"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert await store.load_access_token("access-claude") is None
    assert await store.load_refresh_token("refresh-claude") is None

    follow_up = await app_client.get("/connected-apps", params={"session_id": session_id})
    assert "Claude" not in follow_up.text


async def test_revoke_does_not_affect_other_subjects(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    subject = await _connect_client(store, client_id="shared-client", client_name="Shared")

    other_subject = "someone-else"
    await store.save_access_token(
        AccessToken(
            token="access-other",
            client_id="shared-client",
            scopes=[SCOPE],
            expires_at=9999999999,
            subject=other_subject,
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-other",
            client_id="shared-client",
            scopes=[SCOPE],
            expires_at=9999999999,
            subject=other_subject,
        )
    )

    session_id = await store.create_management_session(subject)
    await app_client.post(
        "/connected-apps/revoke",
        data={"session_id": session_id, "client_id": "shared-client"},
    )

    assert await store.load_access_token("access-other") is not None
    assert await store.load_refresh_token("refresh-other") is not None
