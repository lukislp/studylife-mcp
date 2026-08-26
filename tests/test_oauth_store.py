import time
from pathlib import Path

import anyio
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


async def test_count_clients_by_status_empty_store(store: OAuthStore) -> None:
    assert await store.count_clients_by_status() == {"activated": 0, "pending": 0}


async def test_count_clients_by_status_counts_pending_and_activated(store: OAuthStore) -> None:
    await store.register_client(
        OAuthClientInformationFull(
            client_id="pending-client",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )
    await store.register_client(
        OAuthClientInformationFull(
            client_id="activated-client",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )
    await store.save_access_token(
        AccessToken(
            token="access-1",
            client_id="activated-client",
            scopes=["studylife"],
            expires_at=int(time.time()) + 3600,
            subject="user-subject",
        )
    )

    assert await store.count_clients_by_status() == {"activated": 1, "pending": 1}


async def test_unused_client_is_purged_after_ttl_on_next_registration(
    store: OAuthStore,
) -> None:
    import studylife_mcp.oauth_store as oauth_store_module

    stale = OAuthClientInformationFull(
        client_id="stale-client",
        redirect_uris=[AnyUrl("https://client.example/callback")],
    )
    await store.register_client(stale)

    original_ttl = oauth_store_module.UNUSED_CLIENT_TTL_SECONDS
    oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = -1  # already "stale" the instant it's created
    try:
        fresh = OAuthClientInformationFull(
            client_id="fresh-client",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
        await store.register_client(fresh)
    finally:
        oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = original_ttl

    assert await store.get_client("stale-client") is None
    assert await store.get_client("fresh-client") is not None


async def test_activated_client_survives_ttl_cleanup(store: OAuthStore) -> None:
    import studylife_mcp.oauth_store as oauth_store_module

    activated = OAuthClientInformationFull(
        client_id="activated-client",
        redirect_uris=[AnyUrl("https://client.example/callback")],
    )
    await store.register_client(activated)
    await store.save_access_token(
        AccessToken(
            token="access-1",
            client_id="activated-client",
            scopes=["studylife"],
            expires_at=int(time.time()) + 3600,
            subject="user-subject",
        )
    )

    original_ttl = oauth_store_module.UNUSED_CLIENT_TTL_SECONDS
    oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = -1
    try:
        await store.register_client(
            OAuthClientInformationFull(
                client_id="another-client",
                redirect_uris=[AnyUrl("https://client.example/callback")],
            )
        )
    finally:
        oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = original_ttl

    assert await store.get_client("activated-client") is not None


async def test_cleanup_expired_clients_deletes_stale_and_returns_count(
    store: OAuthStore,
) -> None:
    import studylife_mcp.oauth_store as oauth_store_module

    await store.register_client(
        OAuthClientInformationFull(
            client_id="will-expire",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )

    original_ttl = oauth_store_module.UNUSED_CLIENT_TTL_SECONDS
    oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = -1
    try:
        deleted = await store.cleanup_expired_clients()
    finally:
        oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = original_ttl

    assert deleted == 1
    assert await store.get_client("will-expire") is None


async def test_cleanup_expired_clients_returns_zero_when_nothing_expired(
    store: OAuthStore,
) -> None:
    await store.register_client(
        OAuthClientInformationFull(
            client_id="fresh-client",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )

    assert await store.cleanup_expired_clients() == 0


async def test_run_periodic_cleanup_sweeps_without_a_new_registration(
    store: OAuthStore,
) -> None:
    """The actual point of the periodic sweep (vs. the pre-existing opportunistic-only
    cleanup in register_client()): an expired client gets removed even though nothing
    ever registers again to trigger that path - found live as a stale dashboard count
    that only self-corrected on the next unrelated registration, see docs/decisions.md."""
    import studylife_mcp.oauth_store as oauth_store_module

    await store.register_client(
        OAuthClientInformationFull(
            client_id="stale-client",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )

    original_ttl = oauth_store_module.UNUSED_CLIENT_TTL_SECONDS
    original_interval = oauth_store_module.CLEANUP_INTERVAL_SECONDS
    oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = -1
    oauth_store_module.CLEANUP_INTERVAL_SECONDS = 0
    try:
        with anyio.move_on_after(1):
            await store.run_periodic_cleanup()
    finally:
        oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = original_ttl
        oauth_store_module.CLEANUP_INTERVAL_SECONDS = original_interval

    assert await store.get_client("stale-client") is None


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


async def test_load_refresh_token_expired_returns_none(store: OAuthStore) -> None:
    expired = RefreshToken(
        token="refresh-expired",
        client_id="client-1",
        scopes=["studylife"],
        expires_at=int(time.time()) - 10,
        subject="user-subject",
    )
    await store.save_refresh_token(expired)

    assert await store.load_refresh_token("refresh-expired") is None


async def test_load_refresh_token_without_expiry_never_expires(store: OAuthStore) -> None:
    token = RefreshToken(
        token="refresh-no-expiry",
        client_id="client-1",
        scopes=["studylife"],
        subject="user-subject",
    )
    await store.save_refresh_token(token)

    loaded = await store.load_refresh_token("refresh-no-expiry")
    assert loaded is not None
    assert loaded.expires_at is None


async def test_load_authorization_code_expired_returns_none(store: OAuthStore) -> None:
    expired = AuthorizationCode(
        code="code-expired",
        scopes=["studylife"],
        expires_at=time.time() - 10,
        client_id="client-1",
        code_challenge="challenge",
        redirect_uri="https://client.example/callback",  # type: ignore[arg-type]
        redirect_uri_provided_explicitly=True,
        subject="user-subject",
    )
    await store.save_authorization_code(expired)

    assert await store.load_authorization_code("code-expired") is None


async def _save_expired_and_live_tokens(store: OAuthStore) -> None:
    now = int(time.time())
    await store.save_access_token(
        AccessToken(
            token="access-long-expired",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=now - 60 * 60 * 24 * 2,  # 2 days past expiry - beyond the grace period
            subject="user-subject",
        )
    )
    await store.save_access_token(
        AccessToken(
            token="access-live",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=now + 3600,
            subject="user-subject",
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-long-expired",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=now - 60 * 60 * 24 * 2,
            subject="user-subject",
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-live",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=now + 3600,
            subject="user-subject",
        )
    )
    # No expiry set at all - must never be purged regardless of grace period.
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-no-expiry",
            client_id="client-1",
            scopes=["studylife"],
            subject="user-subject",
        )
    )


async def test_cleanup_expired_tokens_deletes_only_rows_past_the_grace_period(
    store: OAuthStore,
) -> None:
    await _save_expired_and_live_tokens(store)

    deleted = await store.cleanup_expired_tokens()

    assert deleted == 2
    assert await store.load_access_token("access-long-expired") is None
    assert await store.load_access_token("access-live") is not None
    assert await store.load_refresh_token("refresh-live") is not None
    assert await store.load_refresh_token("refresh-no-expiry") is not None


async def test_cleanup_expired_tokens_respects_grace_period(store: OAuthStore) -> None:
    """A token that JUST expired (well within TOKEN_PURGE_GRACE_SECONDS) survives the
    sweep - only load_refresh_token's own expiry check (tested above) makes it
    unusable, the purge itself waits out the grace period."""
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-just-expired",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=int(time.time()) - 10,
            subject="user-subject",
        )
    )

    deleted = await store.cleanup_expired_tokens()

    assert deleted == 0


async def test_cleanup_expired_tokens_returns_zero_when_nothing_expired(store: OAuthStore) -> None:
    await store.save_access_token(
        AccessToken(
            token="access-1",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=int(time.time()) + 3600,
            subject="user-subject",
        )
    )

    assert await store.cleanup_expired_tokens() == 0


def _make_authorization_code(*, code: str, expires_at: float) -> AuthorizationCode:
    return AuthorizationCode(
        code=code,
        scopes=["studylife"],
        expires_at=expires_at,
        client_id="client-1",
        code_challenge="challenge",
        redirect_uri="https://client.example/callback",  # type: ignore[arg-type]
        redirect_uri_provided_explicitly=True,
        subject="user-subject",
    )


async def test_cleanup_expired_tokens_also_purges_expired_auth_codes(store: OAuthStore) -> None:
    """Abandoned auth codes (nobody ever completed the token exchange) accumulate just
    like abandoned tokens - AUTHORIZATION_CODE_TTL_SECONDS is only 5 minutes, so by the
    time TOKEN_PURGE_GRACE_SECONDS (1 day) has also passed, this is purely a storage
    sweep: the code has already been unusable for nearly 24 hours via the SDK's own
    /token handler and load_authorization_code's local expiry check."""
    now = time.time()
    await store.save_authorization_code(
        _make_authorization_code(code="code-long-abandoned", expires_at=now - 60 * 60 * 24 * 2)
    )
    await store.save_authorization_code(
        _make_authorization_code(code="code-live", expires_at=now + 300)
    )

    deleted = await store.cleanup_expired_tokens()

    assert deleted == 1
    assert await store.load_authorization_code("code-long-abandoned") is None
    assert await store.load_authorization_code("code-live") is not None


async def test_cleanup_expired_tokens_respects_grace_period_for_auth_codes(
    store: OAuthStore,
) -> None:
    """A code that just expired (well within the grace period) survives the sweep -
    same grace-period semantics as access/refresh tokens above. load_authorization_code
    already treats it as gone regardless (tested separately) - the purge is on its own
    slower clock."""
    await store.save_authorization_code(
        _make_authorization_code(code="code-just-expired", expires_at=time.time() - 10)
    )

    deleted = await store.cleanup_expired_tokens()

    assert deleted == 0


async def test_run_periodic_cleanup_also_sweeps_expired_tokens(store: OAuthStore) -> None:
    import studylife_mcp.oauth_store as oauth_store_module

    await store.save_access_token(
        AccessToken(
            token="access-long-expired",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=int(time.time()) - 60 * 60 * 24 * 2,
            subject="user-subject",
        )
    )

    original_ttl = oauth_store_module.UNUSED_CLIENT_TTL_SECONDS
    original_interval = oauth_store_module.CLEANUP_INTERVAL_SECONDS
    original_grace = oauth_store_module.TOKEN_PURGE_GRACE_SECONDS
    oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = -1
    oauth_store_module.CLEANUP_INTERVAL_SECONDS = 0
    oauth_store_module.TOKEN_PURGE_GRACE_SECONDS = -1
    try:
        with anyio.move_on_after(1):
            await store.run_periodic_cleanup()
    finally:
        oauth_store_module.UNUSED_CLIENT_TTL_SECONDS = original_ttl
        oauth_store_module.CLEANUP_INTERVAL_SECONDS = original_interval
        oauth_store_module.TOKEN_PURGE_GRACE_SECONDS = original_grace

    assert await store.load_access_token("access-long-expired") is None


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


async def test_management_session_round_trip(store: OAuthStore) -> None:
    session_id, csrf_token = await store.create_management_session("subject-1")
    assert await store.load_management_session(session_id) == "subject-1"
    assert await store.load_management_session_csrf_token(session_id) == csrf_token


async def test_management_session_unknown_id_returns_none(store: OAuthStore) -> None:
    assert await store.load_management_session("no-such-session") is None


async def test_management_session_csrf_token_unknown_id_returns_none(store: OAuthStore) -> None:
    assert await store.load_management_session_csrf_token("no-such-session") is None


async def test_management_session_csrf_tokens_are_distinct_per_session(store: OAuthStore) -> None:
    _session_a, csrf_a = await store.create_management_session("subject-1")
    _session_b, csrf_b = await store.create_management_session("subject-1")
    assert csrf_a != csrf_b


async def test_management_session_expires(store: OAuthStore) -> None:
    import studylife_mcp.oauth_store as oauth_store_module

    session_id, _csrf_token = await store.create_management_session("subject-1")

    original_ttl = oauth_store_module.MANAGEMENT_SESSION_TTL_SECONDS
    oauth_store_module.MANAGEMENT_SESSION_TTL_SECONDS = -1
    try:
        assert await store.load_management_session(session_id) is None
        assert await store.load_management_session_csrf_token(session_id) is None
    finally:
        oauth_store_module.MANAGEMENT_SESSION_TTL_SECONDS = original_ttl


async def _register_and_authorize(
    store: OAuthStore, *, client_id: str, client_name: str, subject: str
) -> None:
    await store.register_client(
        OAuthClientInformationFull(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )
    await store.save_access_token(
        AccessToken(
            token=f"access-{client_id}-{subject}",
            client_id=client_id,
            scopes=["studylife"],
            expires_at=int(time.time()) + 3600,
            subject=subject,
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token=f"refresh-{client_id}-{subject}",
            client_id=client_id,
            scopes=["studylife"],
            expires_at=int(time.time()) + 3600,
            subject=subject,
        )
    )


async def test_list_connected_clients_only_returns_matching_subject(store: OAuthStore) -> None:
    await _register_and_authorize(store, client_id="claude", client_name="Claude", subject="alice")
    await _register_and_authorize(
        store, client_id="other-app", client_name="Other App", subject="bob"
    )

    alice_apps = await store.list_connected_clients("alice")

    assert [c.client_id for c in alice_apps] == ["claude"]
    assert alice_apps[0].client_name == "Claude"


async def test_list_connected_clients_empty_for_unknown_subject(store: OAuthStore) -> None:
    assert await store.list_connected_clients("nobody") == []


async def test_list_connected_clients_excludes_client_with_only_expired_refresh_tokens(
    store: OAuthStore,
) -> None:
    await store.register_client(
        OAuthClientInformationFull(
            client_id="stale-app",
            client_name="Stale App",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-stale-app",
            client_id="stale-app",
            scopes=["studylife"],
            expires_at=int(time.time()) - 3600,  # "active until last year" - audit A15 item 2
            subject="alice",
        )
    )

    assert await store.list_connected_clients("alice") == []


async def test_list_connected_clients_includes_client_with_one_live_and_one_expired_token(
    store: OAuthStore,
) -> None:
    """A client can hold more than one refresh_tokens row for the same subject (e.g. it
    reconnected via a fresh /authorize round trip rather than a refresh-token exchange).
    It must still show up - and with the live row's expiry, not the expired one's -
    as long as at least one row is still live."""
    await store.register_client(
        OAuthClientInformationFull(
            client_id="claude",
            client_name="Claude",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )
    live_expiry = int(time.time()) + 3600
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-claude-old",
            client_id="claude",
            scopes=["studylife"],
            expires_at=int(time.time()) - 3600,
            subject="alice",
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-claude-new",
            client_id="claude",
            scopes=["studylife"],
            expires_at=live_expiry,
            subject="alice",
        )
    )

    apps = await store.list_connected_clients("alice")

    assert [c.client_id for c in apps] == ["claude"]
    assert apps[0].expires_at == live_expiry


async def test_list_connected_clients_no_expiry_token_outranks_expiring_one(
    store: OAuthStore,
) -> None:
    await store.register_client(
        OAuthClientInformationFull(
            client_id="claude",
            client_name="Claude",
            redirect_uris=[AnyUrl("https://client.example/callback")],
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-claude-expiring",
            client_id="claude",
            scopes=["studylife"],
            expires_at=int(time.time()) + 3600,
            subject="alice",
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token="refresh-claude-no-expiry",
            client_id="claude",
            scopes=["studylife"],
            subject="alice",
        )
    )

    apps = await store.list_connected_clients("alice")

    assert [c.client_id for c in apps] == ["claude"]
    assert apps[0].expires_at is None


async def test_revoke_client_access_deletes_only_that_subjects_tokens(store: OAuthStore) -> None:
    await _register_and_authorize(
        store, client_id="shared-client", client_name="Shared", subject="alice"
    )
    await _register_and_authorize(
        store, client_id="shared-client", client_name="Shared", subject="bob"
    )

    await store.revoke_client_access("alice", "shared-client")

    assert await store.load_access_token("access-shared-client-alice") is None
    assert await store.load_refresh_token("refresh-shared-client-alice") is None
    assert await store.load_access_token("access-shared-client-bob") is not None
    assert await store.load_refresh_token("refresh-shared-client-bob") is not None
