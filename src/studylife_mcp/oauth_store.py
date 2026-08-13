import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    info_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    activated_at REAL
);
CREATE TABLE IF NOT EXISTS pending_auth (
    request_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    code_json TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS access_tokens (
    token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    token_json TEXT NOT NULL,
    expires_at REAL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    token_json TEXT NOT NULL,
    expires_at REAL
);
CREATE TABLE IF NOT EXISTS user_keys (
    subject TEXT PRIMARY KEY,
    encrypted_key BLOB NOT NULL,
    created_at REAL NOT NULL
);
"""

# How long a /authorize -> our login form round trip has to complete before the
# pending request is discarded. Generous for a human filling in a form once.
PENDING_AUTH_TTL_SECONDS = 600

# RFC 7591 dynamic client registration is unauthenticated by design (any MCP client
# self-registers). A client that never completes the OAuth flow (no access token ever
# issued) within this window is considered abandoned/abuse and gets purged the next
# time someone registers - see register_client() below. Real clients activate within
# seconds of registering, so a day is generous headroom, not a tight deadline.
UNUSED_CLIENT_TTL_SECONDS = 60 * 60 * 24


class OAuthStore:
    """SQLite-backed persistence for the OAuth authorization server (oauth_provider.py):
    registered clients, in-flight authorizations, issued codes/tokens, and each
    resource owner's StudyLife API key (encrypted at rest with `encryption_key` -
    unlike StudyLife's own hash-only key storage, this server needs the plaintext
    back to call StudyLife on the user's behalf).

    Opens a fresh connection per call rather than holding one open across the
    server's lifetime - simpler to reason about and test, and fine at the traffic
    scale this personal-use server actually sees; revisit only if that changes.
    """

    def __init__(self, db_path: str, encryption_key: str) -> None:
        self._db_path = db_path
        self._fernet = Fernet(encryption_key.encode())

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        # aiosqlite.connect() returns an object that is itself awaitable *and* an
        # async context manager - awaiting it starts its background thread, and
        # entering `async with` on the SAME object awaits it again, trying to start
        # that thread a second time ("threads can only be started once"). Awaiting
        # once here and wrapping the result in a plain try/finally sidesteps that.
        conn = await aiosqlite.connect(self._db_path)
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            await conn.close()

    async def initialize(self) -> None:
        async with self._connection() as conn:
            await conn.executescript(_SCHEMA)
            # SQLite's CREATE TABLE IF NOT EXISTS doesn't retrofit new columns onto an
            # already-existing table (e.g. a DB created before activated_at existed) -
            # ALTER TABLE, ignoring the "already there" error, makes this idempotent.
            try:
                await conn.execute("ALTER TABLE clients ADD COLUMN activated_at REAL")
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
            await conn.commit()

    # --- clients (RFC 7591 dynamic client registration) ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT info_json FROM clients WHERE client_id = ?", (client_id,)
            )
            row = await cursor.fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self._connection() as conn:
            # Opportunistic cleanup: bounds table growth from registration spam without
            # a separate background task - runs exactly when new registrations happen,
            # i.e. exactly when growth would otherwise be unbounded.
            await conn.execute(
                "DELETE FROM clients WHERE activated_at IS NULL AND created_at < ?",
                (time.time() - UNUSED_CLIENT_TTL_SECONDS,),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO clients (client_id, info_json, created_at) "
                "VALUES (?, ?, ?)",
                (client_info.client_id, client_info.model_dump_json(), time.time()),
            )
            await conn.commit()

    # --- pending authorization: bridges /authorize -> our login form -> final redirect ---

    async def save_pending_authorization(
        self, request_id: str, client_id: str, params_json: str
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO pending_auth "
                "(request_id, client_id, params_json, created_at) VALUES (?, ?, ?, ?)",
                (request_id, client_id, params_json, time.time()),
            )
            await conn.commit()

    async def load_pending_authorization(self, request_id: str) -> tuple[str, str] | None:
        """Returns (client_id, params_json), or None if the id is unknown or the TTL
        has passed. Non-destructive - the login form may be (re-)rendered (GET) or
        submitted with a wrong key (POST, re-shows the form) any number of times
        before `delete_pending_authorization` explicitly consumes it."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT client_id, params_json, created_at FROM pending_auth WHERE request_id = ?",
                (request_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        client_id, params_json, created_at = row
        if created_at < time.time() - PENDING_AUTH_TTL_SECONDS:
            return None
        return client_id, params_json

    async def delete_pending_authorization(self, request_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM pending_auth WHERE request_id = ?", (request_id,))
            await conn.commit()

    # --- authorization codes ---

    async def save_authorization_code(self, code: AuthorizationCode) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO auth_codes "
                "(code, client_id, code_json, expires_at) VALUES (?, ?, ?, ?)",
                (code.code, code.client_id, code.model_dump_json(), code.expires_at),
            )
            await conn.commit()

    async def load_authorization_code(self, code: str) -> AuthorizationCode | None:
        async with self._connection() as conn:
            cursor = await conn.execute("SELECT code_json FROM auth_codes WHERE code = ?", (code,))
            row = await cursor.fetchone()
        return AuthorizationCode.model_validate_json(row[0]) if row else None

    async def delete_authorization_code(self, code: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM auth_codes WHERE code = ?", (code,))
            await conn.commit()

    # --- access / refresh tokens ---

    async def save_access_token(self, token: AccessToken) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO access_tokens (token, client_id, token_json, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token.token, token.client_id, token.model_dump_json(), token.expires_at),
            )
            # First real token issued for this client = it completed the OAuth flow at
            # least once, so the TTL cleanup in register_client() must never sweep it.
            await conn.execute(
                "UPDATE clients SET activated_at = COALESCE(activated_at, ?) WHERE client_id = ?",
                (time.time(), token.client_id),
            )
            await conn.commit()

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT token_json FROM access_tokens WHERE token = ?", (token,)
            )
            row = await cursor.fetchone()
        return AccessToken.model_validate_json(row[0]) if row else None

    async def delete_access_token(self, token: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM access_tokens WHERE token = ?", (token,))
            await conn.commit()

    async def save_refresh_token(self, token: RefreshToken) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO refresh_tokens (token, client_id, token_json, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token.token, token.client_id, token.model_dump_json(), token.expires_at),
            )
            await conn.commit()

    async def load_refresh_token(self, token: str) -> RefreshToken | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT token_json FROM refresh_tokens WHERE token = ?", (token,)
            )
            row = await cursor.fetchone()
        return RefreshToken.model_validate_json(row[0]) if row else None

    async def delete_refresh_token(self, token: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
            await conn.commit()

    # --- per-user StudyLife API key, encrypted at rest ---

    @staticmethod
    def subject_for_key(studylife_api_key: str) -> str:
        """Stable, non-secret identifier for a StudyLife API key, used as the OAuth
        `subject` - lets tokens reference which user they belong to without the
        subject value itself being sensitive."""
        return hashlib.sha256(studylife_api_key.encode()).hexdigest()

    async def save_user_key(self, subject: str, studylife_api_key: str) -> None:
        encrypted = self._fernet.encrypt(studylife_api_key.encode())
        async with self._connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO user_keys "
                "(subject, encrypted_key, created_at) VALUES (?, ?, ?)",
                (subject, encrypted, time.time()),
            )
            await conn.commit()

    async def load_user_key(self, subject: str) -> str | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT encrypted_key FROM user_keys WHERE subject = ?", (subject,)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._fernet.decrypt(row[0]).decode()
