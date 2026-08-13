import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull


@dataclass(frozen=True)
class ConnectedClient:
    """One row of the /connected-apps view - a client that currently holds a live
    refresh token for some subject, i.e. an app the resource owner has actually
    authorized (not just registered - see UNUSED_CLIENT_TTL_SECONDS)."""

    client_id: str
    client_name: str
    expires_at: float | None


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
CREATE TABLE IF NOT EXISTS management_sessions (
    session_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
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

# How long a "prove you own this StudyLife key" verification on /connected-apps stays
# usable for follow-up actions (viewing the list again, revoking an entry) before the
# StudyLife key has to be re-entered. Short-lived on purpose - this page reveals which
# clients hold live access to someone's account, so the proof of ownership shouldn't
# outlive a single sitting at the page.
MANAGEMENT_SESSION_TTL_SECONDS = 600


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

    # --- connected-apps self-service (/connected-apps): view/revoke per-subject grants ---

    async def create_management_session(self, subject: str) -> str:
        """Re-validating the StudyLife key once (same check as /login) proves the
        caller owns `subject`; this opaque session id stands in for the raw key on
        every follow-up request on the page (list refresh, revoke click) so the key
        itself is never echoed back into rendered HTML."""
        session_id = secrets.token_urlsafe(24)
        async with self._connection() as conn:
            await conn.execute(
                "INSERT INTO management_sessions (session_id, subject, created_at) "
                "VALUES (?, ?, ?)",
                (session_id, subject, time.time()),
            )
            await conn.commit()
        return session_id

    async def load_management_session(self, session_id: str) -> str | None:
        """Returns the verified subject, or None if the session id is unknown or its
        TTL has passed. Non-destructive, like load_pending_authorization - reused for
        every action taken on the page during one sitting."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT subject, created_at FROM management_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        subject, created_at = row
        if created_at < time.time() - MANAGEMENT_SESSION_TTL_SECONDS:
            return None
        return str(subject)

    async def list_connected_clients(self, subject: str) -> list[ConnectedClient]:
        """Apps with a *live* refresh token for this subject - registering (DCR) alone
        doesn't grant access, completing the login flow at least once does, and that's
        exactly what issuing a refresh token represents. Filtered in Python rather than
        by a SQL column: subject lives inside refresh_tokens.token_json (the SDK's own
        RefreshToken shape), and this table is tiny at this server's traffic scale."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT client_id, token_json, expires_at FROM refresh_tokens"
            )
            rows = await cursor.fetchall()
            results = []
            for client_id, token_json, expires_at in rows:
                token = RefreshToken.model_validate_json(token_json)
                if token.subject != subject:
                    continue
                client_cursor = await conn.execute(
                    "SELECT info_json FROM clients WHERE client_id = ?", (client_id,)
                )
                client_row = await client_cursor.fetchone()
                client_name = client_id
                if client_row is not None:
                    client_info = OAuthClientInformationFull.model_validate_json(client_row[0])
                    client_name = client_info.client_name or client_id
                results.append(ConnectedClient(client_id, client_name, expires_at))
        return results

    async def revoke_client_access(self, subject: str, client_id: str) -> None:
        """Deletes every access/refresh token this subject has for this client - the
        client keeps its DCR registration (client_id/secret), but has to go through a
        fresh /authorize -> /login round trip (a real StudyLife-key re-entry) to get
        anything working again, exactly like a first-time connection."""
        async with self._connection() as conn:
            for table in ("access_tokens", "refresh_tokens"):
                cursor = await conn.execute(
                    f"SELECT token, token_json FROM {table} WHERE client_id = ?",
                    (client_id,),
                )
                rows = await cursor.fetchall()
                model = AccessToken if table == "access_tokens" else RefreshToken
                stale_tokens = [
                    token
                    for token, token_json in rows
                    if model.model_validate_json(token_json).subject == subject
                ]
                if stale_tokens:
                    placeholders = ",".join("?" for _ in stale_tokens)
                    await conn.execute(
                        f"DELETE FROM {table} WHERE token IN ({placeholders})",
                        stale_tokens,
                    )
            await conn.commit()
