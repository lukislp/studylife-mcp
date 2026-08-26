from pathlib import Path

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
from studylife_mcp.oauth_provider import (
    MANAGEMENT_SESSION_COOKIE,
    SCOPE,
    StudyLifeOAuthProvider,
    register_oauth_routes,
)
from studylife_mcp.oauth_store import OAuthStore

API_KEY = "a-real-studylife-key"


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
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


async def _connect_client(store: OAuthStore, *, client_id: str, client_name: str) -> str:
    """Directly seeds a registered+authorized client for API_KEY's subject - the connect
    flow itself is covered by test_oauth_studylife_callback_route.py, this file is about
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
    # No cookie set, but an (invalid) session_id arrives via the legacy query param - the
    # transition path (audit A15 item 3) always accepts it and redirects once before
    # validating, so follow that redirect to see the actual outcome.
    response = await app_client.get(
        "/connected-apps", params={"session_id": "does-not-exist"}, follow_redirects=True
    )

    assert response.status_code == 200
    assert "api_key" in response.text


async def test_get_with_query_session_id_sets_cookie_and_redirects_to_clean_url(
    app_client: httpx.AsyncClient,
) -> None:
    """audit A15 item 3: a bookmarked/old-style link carrying ?session_id=... in the URL
    must not keep leaking it - the first hit moves it into a cookie and lands on a URL
    with no query string at all."""
    response = await app_client.get(
        "/connected-apps", params={"session_id": "some-session-id"}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/connected-apps"
    set_cookie = response.headers.get("set-cookie", "")
    assert f"{MANAGEMENT_SESSION_COOKIE}=some-session-id" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "secure" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "path=/connected-apps" in set_cookie.lower()


async def test_get_with_valid_cookie_shows_list_without_query_param(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    subject = store.subject_for_key(API_KEY)
    session_id, _csrf_token = await store.create_management_session(subject)
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, session_id, path="/connected-apps")

    response = await app_client.get("/connected-apps")

    assert response.status_code == 200
    assert "no apps" in response.text.lower()


async def test_get_with_invalid_cookie_clears_it_and_shows_key_form(
    app_client: httpx.AsyncClient,
) -> None:
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, "does-not-exist", path="/connected-apps")

    response = await app_client.get("/connected-apps")

    assert response.status_code == 200
    assert "api_key" in response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert f'{MANAGEMENT_SESSION_COOKIE}=""' in set_cookie or f"{MANAGEMENT_SESSION_COOKIE}=;" in (
        set_cookie.replace(" ", "")
    )


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
async def test_post_valid_key_redirects_to_clean_url_and_sets_session_cookie(
    app_client: httpx.AsyncClient, settings: Settings
) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(
        return_value=httpx.Response(200, json=[])
    )

    response = await app_client.post(
        "/connected-apps", data={"api_key": API_KEY}, follow_redirects=False
    )

    assert response.status_code == 303
    # No session_id in the redirect target (audit A15 item 3) - it travels via cookie now.
    assert response.headers["location"] == "/connected-apps"
    set_cookie = response.headers.get("set-cookie", "")
    assert f"{MANAGEMENT_SESSION_COOKIE}=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "secure" in set_cookie.lower()


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
    assert login.status_code == 303

    # Same client instance - the cookie set by the login response above is sent
    # automatically, no session_id param needed.
    response = await app_client.get("/connected-apps")

    assert response.status_code == 200
    assert "Claude" in response.text


async def test_list_empty_state_when_nothing_connected(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    subject = store.subject_for_key(API_KEY)
    session_id, _csrf_token = await store.create_management_session(subject)
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, session_id, path="/connected-apps")

    response = await app_client.get("/connected-apps")

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
    session_id, csrf_token = await store.create_management_session(subject)
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, session_id, path="/connected-apps")

    response = await app_client.post(
        "/connected-apps/revoke",
        data={"client_id": "claude", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert await store.load_access_token("access-claude") is None
    assert await store.load_refresh_token("refresh-claude") is None

    follow_up = await app_client.get("/connected-apps")
    assert "Claude" not in follow_up.text


async def test_revoke_without_csrf_token_is_rejected(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    subject = await _connect_client(store, client_id="claude", client_name="Claude")
    session_id, _csrf_token = await store.create_management_session(subject)
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, session_id, path="/connected-apps")

    response = await app_client.post(
        "/connected-apps/revoke",
        data={"client_id": "claude"},  # no csrf_token field at all
        follow_redirects=False,
    )

    assert response.status_code == 403
    # Token must not have been revoked.
    assert await store.load_access_token("access-claude") is not None


async def test_revoke_with_wrong_csrf_token_is_rejected(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    subject = await _connect_client(store, client_id="claude", client_name="Claude")
    session_id, _csrf_token = await store.create_management_session(subject)
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, session_id, path="/connected-apps")

    response = await app_client.post(
        "/connected-apps/revoke",
        data={"client_id": "claude", "csrf_token": "not-the-right-token"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert await store.load_access_token("access-claude") is not None


async def test_revoke_rendered_form_carries_the_real_csrf_token(
    app_client: httpx.AsyncClient, store: OAuthStore
) -> None:
    """End-to-end: the token embedded in the server-rendered list page's revoke form is
    exactly the one that passes validation - not a placeholder that happens to be
    accepted."""
    subject = await _connect_client(store, client_id="claude", client_name="Claude")
    session_id, csrf_token = await store.create_management_session(subject)
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, session_id, path="/connected-apps")

    page = await app_client.get("/connected-apps")
    assert csrf_token in page.text

    response = await app_client.post(
        "/connected-apps/revoke",
        data={"client_id": "claude", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303


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

    session_id, csrf_token = await store.create_management_session(subject)
    app_client.cookies.set(MANAGEMENT_SESSION_COOKIE, session_id, path="/connected-apps")
    await app_client.post(
        "/connected-apps/revoke",
        data={"client_id": "shared-client", "csrf_token": csrf_token},
    )

    assert await store.load_access_token("access-other") is not None
    assert await store.load_refresh_token("refresh-other") is not None
