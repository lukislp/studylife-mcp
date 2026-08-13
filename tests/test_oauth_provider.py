import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from studylife_mcp.oauth_provider import StudyLifeOAuthProvider
from studylife_mcp.oauth_store import OAuthStore

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
    return StudyLifeOAuthProvider(store, "https://mcp.example.com/")


async def test_authorize_saves_pending_and_returns_own_login_url(
    provider: StudyLifeOAuthProvider, store: OAuthStore
) -> None:
    params = AuthorizationParams(
        state="xyz",
        scopes=["studylife"],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://client.example/callback"),
        redirect_uri_provided_explicitly=True,
    )

    url = await provider.authorize(CLIENT, params)

    assert url.startswith("https://mcp.example.com/login?request_id=")
    request_id = url.split("request_id=")[1]
    pending = await store.load_pending_authorization(request_id)
    assert pending is not None
    assert pending[0] == "client-1"


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
