from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken

from studylife_mcp.client_resolver import StudyLifeClientResolver
from studylife_mcp.config import Settings
from studylife_mcp.oauth_store import OAuthStore


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
    return s


async def test_resolve_without_access_token_returns_default_client(settings: Settings) -> None:
    resolver = StudyLifeClientResolver(settings, oauth_store=None)

    client = await resolver.resolve()

    assert client is resolver._default_client


async def test_resolve_without_oauth_store_returns_default_client_even_if_authenticated(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = AccessToken(token="t", client_id="c", scopes=["studylife"], subject="user-a")
    monkeypatch.setattr("studylife_mcp.client_resolver.get_access_token", lambda: token)

    resolver = StudyLifeClientResolver(settings, oauth_store=None)
    client = await resolver.resolve()

    assert client is resolver._default_client


async def test_resolve_authenticated_returns_per_user_client(
    settings: Settings, store: OAuthStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.save_user_key("user-a", "user-a-studylife-key")
    token = AccessToken(token="t", client_id="c", scopes=["studylife"], subject="user-a")
    monkeypatch.setattr("studylife_mcp.client_resolver.get_access_token", lambda: token)

    resolver = StudyLifeClientResolver(settings, oauth_store=store)
    client = await resolver.resolve()

    assert client is not resolver._default_client
    assert client._client.headers["X-Api-Key"] == "user-a-studylife-key"


async def test_resolve_caches_per_subject(
    settings: Settings, store: OAuthStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.save_user_key("user-a", "user-a-studylife-key")
    token = AccessToken(token="t", client_id="c", scopes=["studylife"], subject="user-a")
    monkeypatch.setattr("studylife_mcp.client_resolver.get_access_token", lambda: token)

    resolver = StudyLifeClientResolver(settings, oauth_store=store)
    first = await resolver.resolve()
    second = await resolver.resolve()

    assert first is second


async def test_resolve_two_users_get_isolated_clients(
    settings: Settings, store: OAuthStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.save_user_key("user-a", "key-a")
    await store.save_user_key("user-b", "key-b")
    resolver = StudyLifeClientResolver(settings, oauth_store=store)

    token_a = AccessToken(token="ta", client_id="c", scopes=["studylife"], subject="user-a")
    monkeypatch.setattr("studylife_mcp.client_resolver.get_access_token", lambda: token_a)
    client_a = await resolver.resolve()

    token_b = AccessToken(token="tb", client_id="c", scopes=["studylife"], subject="user-b")
    monkeypatch.setattr("studylife_mcp.client_resolver.get_access_token", lambda: token_b)
    client_b = await resolver.resolve()

    assert client_a is not client_b
    assert client_a._client.headers["X-Api-Key"] == "key-a"
    assert client_b._client.headers["X-Api-Key"] == "key-b"


async def test_resolve_unknown_subject_fails_closed(
    settings: Settings, store: OAuthStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = AccessToken(token="t", client_id="c", scopes=["studylife"], subject="ghost-user")
    monkeypatch.setattr("studylife_mcp.client_resolver.get_access_token", lambda: token)

    resolver = StudyLifeClientResolver(settings, oauth_store=store)

    with pytest.raises(PermissionError):
        await resolver.resolve()
