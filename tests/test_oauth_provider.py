import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from studylife_mcp.oauth_provider import StudyLifeOAuthProvider
from studylife_mcp.oauth_store import OAuthStore
from studylife_mcp.pkce import challenge_for

CLIENT = OAuthClientInformationFull(
    client_id="client-1",
    redirect_uris=[AnyUrl("https://client.example/callback")],
)


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
    return s


@pytest.fixture
def provider(store: OAuthStore) -> StudyLifeOAuthProvider:
    return StudyLifeOAuthProvider(
        store, "https://mcp.example.com/", "https://connect.studylife.example.com/"
    )


async def test_authorize_saves_pending_and_redirects_to_own_consent_page(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    """authorize() never sends the browser straight to StudyLife anymore (audit
    2026-11-16 H2): the first stop is this server's browser-bound consent page."""
    params = AuthorizationParams(
        state="xyz",
        scopes=["studylife"],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://client.example/callback"),
        redirect_uri_provided_explicitly=True,
    )

    url = await provider.authorize(CLIENT, params)

    assert url.startswith("https://mcp.example.com/auth/consent?")
    request_id = parse_qs(urlparse(url).query)["request_id"][0]
    pending = await store.load_pending_authorization(request_id)
    assert pending is not None
    assert pending[0] == "client-1"


async def test_studylife_connect_url_round_trips_request_id_as_state(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    await store.save_pending_authorization("req-1", "client-1", "{}")

    url = await provider.studylife_connect_url("req-1")

    assert url.startswith("https://connect.studylife.example.com/connect/mcp?")
    query = parse_qs(urlparse(url).query)
    assert query["redirect_uri"] == ["https://mcp.example.com/auth/studylife/callback"]
    assert query["state"] == ["req-1"]


async def test_studylife_connect_url_carries_pkce_and_stores_verifier(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    await store.save_pending_authorization("req-1", "client-1", "{}")

    url = await provider.studylife_connect_url("req-1")

    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    verifier = await store.take_code_verifier("req-1")
    assert verifier is not None
    assert query["code_challenge"] == [challenge_for(verifier)]
    # The verifier itself never appears in the URL.
    assert verifier not in url
    # Single use: a second take finds nothing.
    assert await store.take_code_verifier("req-1") is None


async def test_each_connect_attempt_gets_a_fresh_verifier(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    await store.save_pending_authorization("req-1", "client-1", "{}")

    first = parse_qs(urlparse(await provider.studylife_connect_url("req-1")).query)
    second = parse_qs(urlparse(await provider.studylife_connect_url("req-1")).query)

    assert first["code_challenge"] != second["code_challenge"]


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "http://127.0.0.1:33418/callback",
        "http://localhost:6274/oauth/callback",
        "cursor://anysphere.cursor-mcp/oauth/callback",
    ],
)
async def test_register_client_accepts_allowed_redirect_uris(
    provider: StudyLifeOAuthProvider, store: OAuthStore, redirect_uri: str
) -> None:
    client = OAuthClientInformationFull(client_id="ok", redirect_uris=[AnyUrl(redirect_uri)])

    await provider.register_client(client)

    assert await store.get_client("ok") is not None


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://attacker.example/callback",
        "javascript://example.com/%0Aalert(1)",
        "data://text/html,hi",
        "file:///etc/passwd",
    ],
)
async def test_register_client_rejects_disallowed_redirect_uris(
    provider: StudyLifeOAuthProvider, store: OAuthStore, redirect_uri: str
) -> None:
    client = OAuthClientInformationFull(
        client_id="bad",
        redirect_uris=[AnyUrl("https://fine.example/cb"), AnyUrl(redirect_uri)],
    )

    with pytest.raises(RegistrationError) as excinfo:
        await provider.register_client(client)

    assert excinfo.value.error == "invalid_redirect_uri"
    assert await store.get_client("bad") is None


async def _issue_code(store: OAuthStore, subject: str = "user-subject") -> AuthorizationCode:
    code = AuthorizationCode(
        code="test-code",
        scopes=["studylife"],
        expires_at=time.time() + 300,
        client_id="client-1",
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://client.example/callback"),
        redirect_uri_provided_explicitly=True,
        subject=subject,
    )
    await store.save_authorization_code(code)
    return code


async def test_exchange_authorization_code_issues_tokens_bound_to_subject(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    code = await _issue_code(store, subject="user-subject")

    tokens = await provider.exchange_authorization_code(CLIENT, code)

    assert tokens.access_token
    assert tokens.refresh_token
    access = await store.load_access_token(tokens.access_token)
    refresh = await store.load_refresh_token(tokens.refresh_token)
    assert access is not None and access.subject == "user-subject"
    assert refresh is not None and refresh.subject == "user-subject"


async def test_exchange_authorization_code_is_single_use(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    code = await _issue_code(store)

    await provider.exchange_authorization_code(CLIENT, code)

    assert await store.load_authorization_code("test-code") is None


async def test_load_access_token_via_provider(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    token = AccessToken(
        token="access-1", client_id="client-1", scopes=["studylife"], subject="user-subject"
    )
    await store.save_access_token(token)

    loaded = await provider.load_access_token("access-1")

    assert loaded is not None
    assert loaded.subject == "user-subject"


async def test_load_access_token_expired_returns_none(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    expired = AccessToken(
        token="access-expired",
        client_id="client-1",
        scopes=["studylife"],
        expires_at=int(time.time()) - 10,
        subject="user-subject",
    )
    await store.save_access_token(expired)

    assert await provider.load_access_token("access-expired") is None


async def test_exchange_refresh_token_rotates(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    old_refresh = RefreshToken(
        token="refresh-old", client_id="client-1", scopes=["studylife"], subject="user-subject"
    )
    await store.save_refresh_token(old_refresh)

    tokens = await provider.exchange_refresh_token(CLIENT, old_refresh, ["studylife"])

    assert tokens.refresh_token != "refresh-old"
    assert await store.load_refresh_token("refresh-old") is None
    new_refresh = await store.load_refresh_token(tokens.refresh_token or "")
    assert new_refresh is not None and new_refresh.subject == "user-subject"


async def test_revoke_token_deletes_access_token(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    token = AccessToken(token="access-1", client_id="client-1", scopes=["studylife"])
    await store.save_access_token(token)

    await provider.revoke_token(token)

    assert await store.load_access_token("access-1") is None


async def test_revoke_token_deletes_refresh_token(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    token = RefreshToken(token="refresh-1", client_id="client-1", scopes=["studylife"])
    await store.save_refresh_token(token)

    await provider.revoke_token(token)

    assert await store.load_refresh_token("refresh-1") is None
