import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from studylife_mcp.oauth_store import OAuthStore


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    db_path = str(tmp_path / "oauth.db")
    s = OAuthStore(db_path, Fernet.generate_key().decode())
    await s.initialize()
    return s


async def test_initialize_is_idempotent(store: OAuthStore) -> None:
    await store.initialize()
    await store.initialize()


async def test_register_and_get_client_round_trip(store: OAuthStore) -> None:
    client = OAuthClientInformationFull(
        client_id="client-1",
        redirect_uris=[AnyUrl("https://client.example/callback")],
    )

    await store.register_client(client)
    loaded = await store.get_client("client-1")

    assert loaded is not None
    assert loaded.client_id == "client-1"
    assert [str(u) for u in (loaded.redirect_uris or [])] == ["https://client.example/callback"]


async def test_get_unknown_client_returns_none(store: OAuthStore) -> None:
    assert await store.get_client("nope") is None


async def test_pending_authorization_round_trip_and_delete(store: OAuthStore) -> None:
    await store.save_pending_authorization("req-1", "client-1", '{"state": "xyz"}')

    loaded = await store.load_pending_authorization("req-1")
    assert loaded == ("client-1", '{"state": "xyz"}')

    # Non-destructive: loading again still finds it (GET re-renders, POST re-validates).
    assert await store.load_pending_authorization("req-1") == ("client-1", '{"state": "xyz"}')

    await store.delete_pending_authorization("req-1")
    assert await store.load_pending_authorization("req-1") is None


async def test_pending_authorization_expires(store: OAuthStore) -> None:
    import studylife_mcp.oauth_store as oauth_store_module

    await store.save_pending_authorization("req-1", "client-1", "{}")

    original_ttl = oauth_store_module.PENDING_AUTH_TTL_SECONDS
    oauth_store_module.PENDING_AUTH_TTL_SECONDS = -1  # already "expired" the instant it's created
    try:
        assert await store.load_pending_authorization("req-1") is None
    finally:
        oauth_store_module.PENDING_AUTH_TTL_SECONDS = original_ttl


async def test_authorization_code_round_trip_and_delete(store: OAuthStore) -> None:
    code = AuthorizationCode(
        code="code-1",
        scopes=["studylife"],
        expires_at=time.time() + 300,
        client_id="client-1",
        code_challenge="challenge",
        redirect_uri="https://client.example/callback",  # type: ignore[arg-type]
        redirect_uri_provided_explicitly=True,
        subject="user-subject",
    )

    await store.save_authorization_code(code)
    loaded = await store.load_authorization_code("code-1")

    assert loaded is not None
    assert loaded.subject == "user-subject"
    assert loaded.code_challenge == "challenge"

    await store.delete_authorization_code("code-1")
    assert await store.load_authorization_code("code-1") is None


async def test_access_token_round_trip_and_delete(store: OAuthStore) -> None:
    token = AccessToken(
        token="access-1",
        client_id="client-1",
        scopes=["studylife"],
        expires_at=int(time.time()) + 3600,
        subject="user-subject",
    )

    await store.save_access_token(token)
    loaded = await store.load_access_token("access-1")

    assert loaded is not None
    assert loaded.subject == "user-subject"

    await store.delete_access_token("access-1")
    assert await store.load_access_token("access-1") is None


async def test_refresh_token_round_trip_and_delete(store: OAuthStore) -> None:
    token = RefreshToken(
        token="refresh-1",
        client_id="client-1",
        scopes=["studylife"],
        subject="user-subject",
    )

    await store.save_refresh_token(token)
    loaded = await store.load_refresh_token("refresh-1")

    assert loaded is not None
    assert loaded.subject == "user-subject"

    await store.delete_refresh_token("refresh-1")
    assert await store.load_refresh_token("refresh-1") is None


async def test_user_key_is_encrypted_at_rest_and_decrypts_correctly(
    store: OAuthStore, tmp_path: Path
) -> None:
    subject = store.subject_for_key("plaintext-studylife-key")
    await store.save_user_key(subject, "plaintext-studylife-key")

    decrypted = await store.load_user_key(subject)
    assert decrypted == "plaintext-studylife-key"

    # The plaintext key must not appear anywhere in the raw database file.
    raw = (tmp_path / "oauth.db").read_bytes()
    assert b"plaintext-studylife-key" not in raw


async def test_subject_for_key_is_deterministic_and_distinguishes_keys(store: OAuthStore) -> None:
    assert store.subject_for_key("key-a") == store.subject_for_key("key-a")
    assert store.subject_for_key("key-a") != store.subject_for_key("key-b")


async def test_load_user_key_unknown_subject_returns_none(store: OAuthStore) -> None:
    assert await store.load_user_key("no-such-subject") is None
