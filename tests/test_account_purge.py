import hashlib
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from studylife_mcp.account_purge import MAX_KEY_HASHES, SHARED_SECRET_HEADER, register_purge_route
from studylife_mcp.config import Settings
from studylife_mcp.oauth_store import OAuthStore

SECRET = "purge-secret-for-tests"
USER_ID = 42
LEGACY_KEY = "legacy-studylife-key"
LEGACY_HASH = hashlib.sha256(LEGACY_KEY.encode()).hexdigest()
OTHER_USER_ID = 43
OTHER_KEY = "someone-elses-key"
OTHER_HASH = hashlib.sha256(OTHER_KEY.encode()).hexdigest()


def _settings(secret: str | None) -> Settings:
    return Settings(
        studylife_base_url="https://studylife.example.test/",
        studylife_purge_secret=secret,
    )


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
    return s


async def _client_for(store: OAuthStore, secret: str | None) -> httpx.AsyncClient:
    mcp = MCPServer("studylife-mcp-test")
    register_purge_route(mcp, store, _settings(secret))
    transport = httpx.ASGITransport(app=mcp.streamable_http_app())
    return httpx.AsyncClient(transport=transport, base_url="https://mcp.example.test")


@pytest.fixture
async def api(store: OAuthStore) -> AsyncIterator[httpx.AsyncClient]:
    async with await _client_for(store, SECRET) as client:
        yield client


def _headers(secret: str = SECRET) -> dict[str, str]:
    return {SHARED_SECRET_HEADER: secret}


async def _seed_subject(store: OAuthStore, subject: str, *, suffix: str) -> None:
    """One of everything the purge has to find for `subject`."""
    now = time.time()
    await store.save_user_key(subject, f"key-{suffix}")
    await store.save_access_token(
        AccessToken(
            token=f"access-{suffix}",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=int(now + 3600),
            subject=subject,
        )
    )
    await store.save_refresh_token(
        RefreshToken(
            token=f"refresh-{suffix}",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=int(now + 3600),
            subject=subject,
        )
    )
    await store.save_authorization_code(
        AuthorizationCode(
            code=f"code-{suffix}",
            client_id="client-1",
            scopes=["studylife"],
            expires_at=now + 300,
            redirect_uri=AnyUrl("https://client.example/callback"),
            redirect_uri_provided_explicitly=True,
            code_challenge="challenge",
            subject=subject,
        )
    )
    await store.create_management_session(subject)


async def _has_data(store: OAuthStore, subject: str, *, suffix: str) -> bool:
    found = [
        await store.load_user_key(subject),
        await store.load_access_token(f"access-{suffix}"),
        await store.load_refresh_token(f"refresh-{suffix}"),
        await store.load_authorization_code(f"code-{suffix}"),
    ]
    return any(item is not None for item in found)


async def _management_sessions(store: OAuthStore, subject: str) -> int:
    async with store._connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM management_sessions WHERE subject = ?", (subject,)
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _seed_client(store: OAuthStore) -> None:
    await store.register_client(
        OAuthClientInformationFull(
            client_id="client-1", redirect_uris=[AnyUrl("https://client.example/callback")]
        )
    )


async def test_unset_secret_answers_503_and_purges_nothing(store: OAuthStore) -> None:
    await _seed_subject(store, str(USER_ID), suffix="a")
    async with await _client_for(store, None) as client:
        response = await client.post(
            f"/internal/users/{USER_ID}/purge", headers=_headers(), json={}
        )
        empty = await client.post(f"/internal/users/{USER_ID}/purge", headers=_headers(""), json={})

    assert response.status_code == 503
    assert empty.status_code == 503
    assert await _has_data(store, str(USER_ID), suffix="a")


async def test_empty_secret_setting_counts_as_unset(store: OAuthStore) -> None:
    async with await _client_for(store, "") as client:
        response = await client.post(f"/internal/users/{USER_ID}/purge", headers=_headers(""))

    assert response.status_code == 503


@pytest.mark.parametrize("headers", [{}, {SHARED_SECRET_HEADER: "wrong"}])
async def test_missing_or_wrong_secret_answers_401(
    api: httpx.AsyncClient, store: OAuthStore, headers: dict[str, str]
) -> None:
    await _seed_subject(store, str(USER_ID), suffix="a")

    response = await api.post(f"/internal/users/{USER_ID}/purge", headers=headers, json={})

    assert response.status_code == 401
    assert await _has_data(store, str(USER_ID), suffix="a")


async def test_too_many_key_hashes_answers_422(api: httpx.AsyncClient) -> None:
    hashes = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(MAX_KEY_HASHES + 1)]

    response = await api.post(
        f"/internal/users/{USER_ID}/purge", headers=_headers(), json={"key_hashes": hashes}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad_hash", ["abc", "G" * 64, LEGACY_HASH.upper(), LEGACY_HASH + "0", 5, None]
)
async def test_malformed_key_hash_answers_422(api: httpx.AsyncClient, bad_hash: object) -> None:
    response = await api.post(
        f"/internal/users/{USER_ID}/purge", headers=_headers(), json={"key_hashes": [bad_hash]}
    )

    assert response.status_code == 422


async def test_non_json_body_answers_422(api: httpx.AsyncClient) -> None:
    response = await api.post(
        f"/internal/users/{USER_ID}/purge", headers=_headers(), content=b"not json"
    )

    assert response.status_code == 422


@pytest.mark.parametrize("user_id", ["0", "-5", "abc", "4.2"])
async def test_invalid_user_id_answers_422(api: httpx.AsyncClient, user_id: str) -> None:
    response = await api.post(f"/internal/users/{user_id}/purge", headers=_headers(), json={})

    assert response.status_code == 422


async def test_purge_removes_everything_for_the_user_id_subject(
    api: httpx.AsyncClient, store: OAuthStore
) -> None:
    await _seed_client(store)
    await _seed_subject(store, str(USER_ID), suffix="a")

    response = await api.post(f"/internal/users/{USER_ID}/purge", headers=_headers(), json={})

    assert response.status_code == 204
    assert response.content == b""
    assert not await _has_data(store, str(USER_ID), suffix="a")
    assert await _management_sessions(store, str(USER_ID)) == 0
    # OAuth client registrations are not per-user data.
    assert await store.get_client("client-1") is not None


async def test_purge_without_body_still_removes_the_user_id_subject(
    api: httpx.AsyncClient, store: OAuthStore
) -> None:
    await _seed_subject(store, str(USER_ID), suffix="a")

    response = await api.post(f"/internal/users/{USER_ID}/purge", headers=_headers())

    assert response.status_code == 204
    assert not await _has_data(store, str(USER_ID), suffix="a")


async def test_purge_removes_legacy_grants_bound_to_a_key_hash(
    api: httpx.AsyncClient, store: OAuthStore
) -> None:
    assert OAuthStore.subject_for_key(LEGACY_KEY) == LEGACY_HASH
    await _seed_subject(store, LEGACY_HASH, suffix="legacy")

    response = await api.post(
        f"/internal/users/{USER_ID}/purge",
        headers=_headers(),
        json={"key_hashes": [LEGACY_HASH]},
    )

    assert response.status_code == 204
    assert not await _has_data(store, LEGACY_HASH, suffix="legacy")
    assert await _management_sessions(store, LEGACY_HASH) == 0


async def test_purge_leaves_other_users_rows_alone(
    api: httpx.AsyncClient, store: OAuthStore
) -> None:
    await _seed_subject(store, str(USER_ID), suffix="a")
    await _seed_subject(store, LEGACY_HASH, suffix="legacy")
    await _seed_subject(store, str(OTHER_USER_ID), suffix="other")
    await _seed_subject(store, OTHER_HASH, suffix="other-legacy")

    response = await api.post(
        f"/internal/users/{USER_ID}/purge",
        headers=_headers(),
        json={"key_hashes": [LEGACY_HASH]},
    )

    assert response.status_code == 204
    assert not await _has_data(store, str(USER_ID), suffix="a")
    assert not await _has_data(store, LEGACY_HASH, suffix="legacy")
    assert await _has_data(store, str(OTHER_USER_ID), suffix="other")
    assert await _has_data(store, OTHER_HASH, suffix="other-legacy")
    assert await _management_sessions(store, str(OTHER_USER_ID)) == 1
    assert await _management_sessions(store, OTHER_HASH) == 1


async def test_purge_is_idempotent(api: httpx.AsyncClient, store: OAuthStore) -> None:
    await _seed_subject(store, str(USER_ID), suffix="a")
    await _seed_subject(store, str(OTHER_USER_ID), suffix="other")
    url = f"/internal/users/{USER_ID}/purge"

    first = await api.post(url, headers=_headers(), json={})
    second = await api.post(url, headers=_headers(), json={})
    unknown = await api.post("/internal/users/999/purge", headers=_headers(), json={})

    assert (first.status_code, second.status_code, unknown.status_code) == (204, 204, 204)
    assert await _has_data(store, str(OTHER_USER_ID), suffix="other")


async def test_purge_subjects_reports_counts(store: OAuthStore) -> None:
    await _seed_subject(store, str(USER_ID), suffix="a")
    await _seed_subject(store, LEGACY_HASH, suffix="legacy")

    counts = await store.purge_subjects({str(USER_ID), LEGACY_HASH})

    assert (counts.user_keys, counts.access_tokens, counts.refresh_tokens) == (2, 2, 2)
    assert (counts.auth_codes, counts.management_sessions) == (2, 2)
    assert counts.total == 10
    assert (await store.purge_subjects({str(USER_ID), LEGACY_HASH})).total == 0
    assert (await store.purge_subjects(set())).total == 0
