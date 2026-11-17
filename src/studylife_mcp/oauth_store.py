import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite
import anyio
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from studylife_mcp.token_hash import hash_token, is_hashed

logger = logging.getLogger("studylife_mcp.oauth_store")


@dataclass(frozen=True)
class ConnectedClient:
    """One row of the /connected-apps view - a client that currently holds a live
    refresh token for some subject, i.e. an app the resource owner has actually
    authorized (not just registered - see UNUSED_CLIENT_TTL_SECONDS)."""

    client_id: str
    client_name: str
    expires_at: float | None


@dataclass(frozen=True)
class _ConsentRow:
    """A live pending authorization that a browser has been bound to (consent.py)."""

    client_id: str
    params_json: str
    binding_hash: str
    csrf_token: str
    approved_at: float | None


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
# management_sessions.csrf_token (audit A15 item 4) is added via ALTER TABLE in
# initialize() below, like clients.activated_at - it postdates the table above and
# SQLite's CREATE TABLE IF NOT EXISTS won't retrofit a column onto an already-existing
# table. Nullable: a row from a not-yet-upgraded server has no csrf_token, and
# load_management_session_csrf_token() treats that as "no valid token" (safe-fail)
# rather than crashing - these rows all expire within MANAGEMENT_SESSION_TTL_SECONDS
# (10 minutes) regardless, so the gap is short-lived.

# How long a /authorize -> our login form round trip has to complete before the
# pending request is discarded. Generous for a human filling in a form once.
PENDING_AUTH_TTL_SECONDS = 600

# RFC 7591 dynamic client registration is unauthenticated by design (any MCP client
# self-registers). A client that never completes the OAuth flow (no access token ever
# issued) within this window is considered abandoned/abuse and gets purged - both
# opportunistically on the next registration (register_client() below) and periodically
# (run_periodic_cleanup() below). Real clients activate within seconds of registering, so
# a day is generous headroom, not a tight deadline.
UNUSED_CLIENT_TTL_SECONDS = 60 * 60 * 24

# How often run_periodic_cleanup() sweeps expired unactivated clients. Found live
# (2026-08-14) that the opportunistic-only cleanup left 6 stale "pending" entries sitting
# on the Grafana dashboard for over a day, past their TTL, simply because nobody had
# registered a new client to trigger the sweep - correct on the next registration, but a
# misleading dashboard signal in the meantime for a low-traffic personal server. An hour
# is frequent enough that expired entries don't linger for most of the TTL window itself,
# without adding a real background-job scheduler for something this simple.
CLEANUP_INTERVAL_SECONDS = 60 * 60

# Expired access/refresh tokens and authorization codes (audit A15 item 1) aren't
# purged by anything until now - they're already unusable everywhere they're checked
# (load_access_token in oauth_provider.py, and the SDK's own /token handler checks
# refresh-token/auth-code expiry itself, see load_refresh_token/load_authorization_code
# below), so this is a storage-hygiene sweep, not a correctness fix. The grace period
# beyond raw expiry isn't needed for correctness either - it just gives a row that
# *just* lapsed a beat to still show up in ad-hoc debugging ("did this ever get
# issued") instead of vanishing the instant expires_at ticks over, mirroring
# UNUSED_CLIENT_TTL_SECONDS' own headroom.
TOKEN_PURGE_GRACE_SECONDS = 60 * 60 * 24

# How long a "prove you own this StudyLife key" verification on /connected-apps stays
# usable for follow-up actions (viewing the list again, revoking an entry) before the
# StudyLife key has to be re-entered. Short-lived on purpose - this page reveals which
# clients hold live access to someone's account, so the proof of ownership shouldn't
# outlive a single sitting at the page.
MANAGEMENT_SESSION_TTL_SECONDS = 600


# (table, model) pairs whose `token` column is stored hashed - see token_hash.py.
_TOKEN_TABLES: tuple[tuple[str, type[AccessToken] | type[RefreshToken]], ...] = (
    ("access_tokens", AccessToken),
    ("refresh_tokens", RefreshToken),
)


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
            try:
                await conn.execute("ALTER TABLE management_sessions ADD COLUMN csrf_token TEXT")
            except aiosqlite.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
            # Consent + browser binding for pending authorizations (audit 2026-11-16 H2,
            # see consent.py). A row from before this change has NULLs here and can
            # therefore never be approved - it simply expires.
            for column in (
                "binding_hash TEXT",
                "csrf_token TEXT",
                "approved_at REAL",
                # PKCE verifier for the StudyAtrium connect round trip (audit M5).
                "code_verifier TEXT",
            ):
                try:
                    await conn.execute(f"ALTER TABLE pending_auth ADD COLUMN {column}")
                except aiosqlite.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise
            for table, model in _TOKEN_TABLES:
                await self._hash_legacy_tokens(conn, table, model)
            await conn.commit()

    @staticmethod
    async def _hash_legacy_tokens(
        conn: aiosqlite.Connection, table: str, model: type[AccessToken] | type[RefreshToken]
    ) -> None:
        """Audit 2026-11-16 L22: rows written before token hashing hold the bearer
        token in plaintext (both the `token` column and inside token_json). Rewrite
        them in place - hashed key, redacted JSON - so every token issued earlier
        keeps validating (the presented token is hashed at lookup) and nobody is
        logged out. Idempotent: already-hashed rows are skipped, so it is safe on
        every start and a no-op once the table is clean."""
        cursor = await conn.execute(f"SELECT token, token_json FROM {table}")
        for stored, token_json in await cursor.fetchall():
            if is_hashed(stored):
                continue
            digest = hash_token(stored)
            redacted = model.model_validate_json(token_json).model_copy(update={"token": digest})
            await conn.execute(
                f"UPDATE {table} SET token = ?, token_json = ? WHERE token = ?",
                (digest, redacted.model_dump_json(), stored),
            )

    # --- clients (RFC 7591 dynamic client registration) ---

    async def count_clients_by_status(self) -> dict[str, int]:
        """Counts for the studylife_mcp_registered_clients Gauge (metrics.py) - queried
        fresh on every /metrics scrape rather than tracked incrementally, so it always
        reflects the real table, including whatever TTL cleanup just removed."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT activated_at IS NOT NULL, COUNT(*) FROM clients GROUP BY 1"
            )
            rows = await cursor.fetchall()
        counts = {"activated": 0, "pending": 0}
        for activated, count in rows:
            counts["activated" if activated else "pending"] = count
        return counts

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT info_json FROM clients WHERE client_id = ?", (client_id,)
            )
            row = await cursor.fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def cleanup_expired_clients(self) -> int:
        """Deletes never-activated client registrations older than
        UNUSED_CLIENT_TTL_SECONDS. Returns the number of rows deleted. Called both
        opportunistically (register_client() below, on every new registration) and
        periodically (run_periodic_cleanup() below, on a fixed interval regardless of
        whether anyone registers) - same query either way."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "DELETE FROM clients WHERE activated_at IS NULL AND created_at < ?",
                (time.time() - UNUSED_CLIENT_TTL_SECONDS,),
            )
            await conn.commit()
            return cursor.rowcount

    async def cleanup_expired_tokens(self) -> int:
        """Deletes access_tokens/refresh_tokens/auth_codes rows whose expires_at is more
        than TOKEN_PURGE_GRACE_SECONDS in the past. Returns the number of rows deleted
        (all three tables combined). Rows with expires_at IS NULL (no TTL - only
        possible for access_tokens/refresh_tokens, auth_codes.expires_at is NOT NULL)
        are never touched. auth_codes rows are usually gone already - exchange_
        authorization_code() deletes a code the moment it's used (single-use, RFC 6749
        §10.5) - this only catches ones nobody ever exchanged (abandoned flow, code
        never redeemed). Given AUTHORIZATION_CODE_TTL_SECONDS is 5 minutes vs. refresh
        tokens' 90 days, the same day-long grace period here is purely a hygiene sweep,
        never a behavior change - by the time a code is old enough to be swept, it's
        been rejected as expired by the SDK's own /token handler (and by
        load_authorization_code below) for nearly 24 hours already. Called only from
        run_periodic_cleanup() below - unlike cleanup_expired_clients(), there's no
        natural per-request event to hang an opportunistic call off of, so the periodic
        sweep is the only trigger."""
        cutoff = time.time() - TOKEN_PURGE_GRACE_SECONDS
        async with self._connection() as conn:
            deleted = 0
            for table in ("access_tokens", "refresh_tokens", "auth_codes"):
                cursor = await conn.execute(
                    f"DELETE FROM {table} WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (cutoff,),
                )
                deleted += cursor.rowcount
            await conn.commit()
            return deleted

    async def run_periodic_cleanup(self) -> None:
        """Background loop (started alongside the HTTP server in server.py's
        main_http()): sweeps expired unactivated clients and expired access/refresh
        tokens + authorization codes on CLEANUP_INTERVAL_SECONDS, independent of
        whether any new client registers. Without the client half of this, an idle
        period longer than UNUSED_CLIENT_TTL_SECONDS leaves expired entries sitting on
        the studylife_mcp_registered_clients dashboard gauge indefinitely - correct on
        the next registration either way, but misleading until then. The token half
        (audit A15 item 1) has no such opportunistic fallback at all, so this periodic
        sweep is the only thing bounding access_tokens/refresh_tokens/auth_codes
        growth. Runs forever; the caller cancels it on shutdown."""
        while True:
            await anyio.sleep(CLEANUP_INTERVAL_SECONDS)
            deleted_clients = await self.cleanup_expired_clients()
            if deleted_clients:
                logger.info("periodic_cleanup deleted_clients=%s", deleted_clients)
            deleted_tokens = await self.cleanup_expired_tokens()
            if deleted_tokens:
                logger.info("periodic_cleanup deleted_tokens=%s", deleted_tokens)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self._connection() as conn:
            # Opportunistic cleanup: bounds table growth from registration spam even
            # between periodic sweeps - runs exactly when new registrations happen, i.e.
            # exactly when growth would otherwise be unbounded. Inlined (not a call to
            # cleanup_expired_clients()) so it shares this same connection/transaction as
            # the INSERT below instead of opening a second one.
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

    # --- PKCE verifier for the StudyAtrium connect round trip (audit M5) ---

    async def save_code_verifier(self, request_id: str, code_verifier: str) -> None:
        """Stores the PKCE verifier with the pending authorization, keyed by the same
        request_id that travels as `state`. Server-side only: never in a URL or cookie.
        Overwrites a previous one, so each connect attempt has a fresh verifier."""
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE pending_auth SET code_verifier = ? WHERE request_id = ?",
                (code_verifier, request_id),
            )
            await conn.commit()

    async def take_code_verifier(self, request_id: str) -> str | None:
        """Returns the stored verifier and clears it in one step, so it can be used once.
        None if there is none (unknown request, already taken, or a request that began
        before PKCE existed)."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT code_verifier FROM pending_auth WHERE request_id = ?", (request_id,)
            )
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return None
            cleared = await conn.execute(
                "UPDATE pending_auth SET code_verifier = NULL "
                "WHERE request_id = ? AND code_verifier = ?",
                (request_id, row[0]),
            )
            await conn.commit()
            return str(row[0]) if cleared.rowcount == 1 else None

    # --- consent + browser binding for pending authorizations (consent.py, audit H2) ---

    async def bind_pending_authorization(
        self, request_id: str, binding_hash: str, csrf_token: str
    ) -> str | None:
        """Binds a pending authorization to one browser. The first browser to open the
        consent page wins: the binding is only written while the row has none, so a
        link already opened elsewhere can't be taken over by whoever opens it next.
        Returns the consent form's CSRF token when the row is (now) bound to
        `binding_hash`, or None if the request is unknown, expired, or bound to another
        browser. `csrf_token` is only stored on the first binding; a re-render from the
        same browser gets the stored one back."""
        cutoff = time.time() - PENDING_AUTH_TTL_SECONDS
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE pending_auth SET binding_hash = ?, csrf_token = ? "
                "WHERE request_id = ? AND binding_hash IS NULL AND created_at >= ?",
                (binding_hash, csrf_token, request_id, cutoff),
            )
            await conn.commit()
        row = await self._load_consent_row(request_id)
        if row is None or not hmac.compare_digest(row.binding_hash, binding_hash):
            return None
        return row.csrf_token

    async def approve_pending_authorization(
        self, request_id: str, binding_hash: str, csrf_token: str
    ) -> bool:
        """Records consent. Only succeeds from the bound browser with the CSRF token
        that was rendered into that browser's consent form, while the request is live."""
        if not await self._consent_proof_matches(request_id, binding_hash, csrf_token):
            return False
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE pending_auth SET approved_at = ? WHERE request_id = ?",
                (time.time(), request_id),
            )
            await conn.commit()
        return True

    async def deny_pending_authorization(
        self, request_id: str, binding_hash: str, csrf_token: str
    ) -> tuple[str, str] | None:
        """Consumes the pending authorization on an explicit "deny" from the bound
        browser. Returns (client_id, params_json) so the caller can send the client its
        access_denied error, or None if the request can't be acted on from here."""
        if not await self._consent_proof_matches(request_id, binding_hash, csrf_token):
            return None
        pending = await self.load_pending_authorization(request_id)
        await self.delete_pending_authorization(request_id)
        return pending

    async def load_approved_pending_authorization(
        self, request_id: str, binding_hash: str
    ) -> tuple[str, str] | None:
        """Like load_pending_authorization, but only for a request that this same
        browser approved on the consent page. The StudyLife callback uses this, so a
        `state` that was never approved here, or was approved in another browser, never
        yields an authorization code."""
        row = await self._load_consent_row(request_id)
        if row is None or row.approved_at is None:
            return None
        if not hmac.compare_digest(row.binding_hash, binding_hash):
            return None
        return row.client_id, row.params_json

    async def _consent_proof_matches(
        self, request_id: str, binding_hash: str, csrf_token: str
    ) -> bool:
        row = await self._load_consent_row(request_id)
        if row is None:
            return False
        # Both compared in constant time, and both always evaluated.
        hash_ok = hmac.compare_digest(row.binding_hash, binding_hash)
        csrf_ok = hmac.compare_digest(row.csrf_token, csrf_token)
        return hash_ok and csrf_ok

    async def _load_consent_row(self, request_id: str) -> _ConsentRow | None:
        """The live, browser-bound pending authorization, or None if it is unknown,
        expired, or not bound to any browser yet."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT client_id, params_json, created_at, binding_hash, csrf_token, "
                "approved_at FROM pending_auth WHERE request_id = ?",
                (request_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        client_id, params_json, created_at, binding_hash, csrf_token, approved_at = row
        if created_at < time.time() - PENDING_AUTH_TTL_SECONDS:
            return None
        if binding_hash is None or csrf_token is None:
            return None
        return _ConsentRow(
            client_id=str(client_id),
            params_json=str(params_json),
            binding_hash=str(binding_hash),
            csrf_token=str(csrf_token),
            approved_at=approved_at,
        )

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
        """Returns None for an unknown code AND for one that's expired (audit A15 item
        1, defense in depth) - the MCP SDK's own /token handler checks
        AuthorizationCode.expires_at itself before calling exchange_authorization_code
        (mcp.server.auth.handlers.token), so this duplicates that check rather than
        relying on it: any future/direct caller of the store gets the same "expired
        means gone" guarantee instead of needing to remember to re-check expires_at
        itself. Both an unknown code and an expired one already map to the same
        'invalid_grant' family of error on the SDK path, so returning None either way
        doesn't change externally observable behavior there - only the error_description
        text differs ('does not exist' vs 'has expired')."""
        async with self._connection() as conn:
            cursor = await conn.execute("SELECT code_json FROM auth_codes WHERE code = ?", (code,))
            row = await cursor.fetchone()
        if row is None:
            return None
        loaded = AuthorizationCode.model_validate_json(row[0])
        if loaded.expires_at < time.time():
            return None
        return loaded

    async def delete_authorization_code(self, code: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM auth_codes WHERE code = ?", (code,))
            await conn.commit()

    # --- access / refresh tokens ---

    async def save_access_token(self, token: AccessToken) -> None:
        digest = hash_token(token.token)
        stored = token.model_copy(update={"token": digest})
        async with self._connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO access_tokens (token, client_id, token_json, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (digest, token.client_id, stored.model_dump_json(), token.expires_at),
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
                "SELECT token_json FROM access_tokens WHERE token = ?", (hash_token(token),)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        # The stored JSON carries the digest, not the token - hand back the real one.
        return AccessToken.model_validate_json(row[0]).model_copy(update={"token": token})

    async def delete_access_token(self, token: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM access_tokens WHERE token = ?", (hash_token(token),))
            await conn.commit()

    async def save_refresh_token(self, token: RefreshToken) -> None:
        digest = hash_token(token.token)
        stored = token.model_copy(update={"token": digest})
        async with self._connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO refresh_tokens (token, client_id, token_json, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (digest, token.client_id, stored.model_dump_json(), token.expires_at),
            )
            await conn.commit()

    async def load_refresh_token(self, token: str) -> RefreshToken | None:
        """Returns None for an unknown token AND for an expired one - see
        load_authorization_code's docstring above for why (same defense-in-depth
        reasoning, same "unknown and expired both already error out the same way on
        the SDK path" argument). A token with expires_at IS NULL never expires."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT token_json FROM refresh_tokens WHERE token = ?", (hash_token(token),)
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        loaded = RefreshToken.model_validate_json(row[0]).model_copy(update={"token": token})
        if loaded.expires_at is not None and loaded.expires_at < time.time():
            return None
        return loaded

    async def delete_refresh_token(self, token: str) -> None:
        async with self._connection() as conn:
            await conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (hash_token(token),))
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

    async def create_management_session(self, subject: str) -> tuple[str, str]:
        """Re-validating the StudyLife key once (same check as /login) proves the
        caller owns `subject`; this opaque session id stands in for the raw key on
        every follow-up request on the page (list refresh, revoke click) so the key
        itself is never echoed back into rendered HTML. Returns (session_id,
        csrf_token): session_id is carried via a cookie (oauth_provider.py sets it,
        HttpOnly/Secure/SameSite=Lax, scoped to /connected-apps - audit A15 item 3), and
        csrf_token is rendered into the revoke form's hidden field and checked against
        this same stored value on submit (audit A15 item 4). Two independent secrets on
        purpose: a cookie alone doesn't stop CSRF (browsers attach it automatically to a
        cross-site form post too), and the whole point of the token is to prove the
        request came from a page this server itself rendered, not just that the browser
        holds a valid cookie."""
        session_id = secrets.token_urlsafe(24)
        csrf_token = secrets.token_urlsafe(24)
        async with self._connection() as conn:
            await conn.execute(
                "INSERT INTO management_sessions (session_id, subject, created_at, csrf_token) "
                "VALUES (?, ?, ?, ?)",
                (session_id, subject, time.time(), csrf_token),
            )
            await conn.commit()
        return session_id, csrf_token

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

    async def load_management_session_csrf_token(self, session_id: str) -> str | None:
        """The CSRF token bound to this management session (see
        create_management_session above), or None if the session id is unknown, its
        TTL has passed (same rule as load_management_session), or the row predates the
        csrf_token column. Kept as a separate lookup rather than folded into
        load_management_session's return value so every existing caller of that method
        (there are several) keeps working unchanged - callers that actually need the
        CSRF token (the /connected-apps GET and revoke routes) fetch it explicitly."""
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT csrf_token, created_at FROM management_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        csrf_token, created_at = row
        if created_at < time.time() - MANAGEMENT_SESSION_TTL_SECONDS:
            return None
        return str(csrf_token) if csrf_token is not None else None

    async def list_connected_clients(self, subject: str) -> list[ConnectedClient]:
        """Apps with at least one *live* (non-expired) refresh token for this subject -
        registering (DCR) alone doesn't grant access, completing the login flow at
        least once does, and that's exactly what issuing a refresh token represents.
        Filtered in Python rather than by a SQL column: subject lives inside
        refresh_tokens.token_json (the SDK's own RefreshToken shape), and this table is
        tiny at this server's traffic scale.

        A client can have more than one refresh_tokens row for the same subject (e.g.
        it reconnected via a fresh /authorize round trip instead of a refresh-token
        exchange, which would have rotated/replaced the old row) - audit A15 item 2:
        a client is only excluded if EVERY row for it is expired, and if several rows
        are still live the furthest-out expires_at is what gets shown, so a page
        refresh right after a rotation can't make an app that's still connected briefly
        disappear or show a stale "active until" date."""
        now = time.time()
        async with self._connection() as conn:
            cursor = await conn.execute(
                "SELECT client_id, token_json, expires_at FROM refresh_tokens"
            )
            rows = await cursor.fetchall()

            def rank(expires_at: float | None) -> float:
                # No-expiry (None) always outranks any concrete expires_at.
                return float("inf") if expires_at is None else expires_at

            live_expiry_by_client: dict[str, float | None] = {}
            for client_id, token_json, expires_at in rows:
                token = RefreshToken.model_validate_json(token_json)
                if token.subject != subject:
                    continue
                if expires_at is not None and expires_at < now:
                    continue  # this row's grant has lapsed - doesn't count towards "live"
                if client_id not in live_expiry_by_client or rank(expires_at) > rank(
                    live_expiry_by_client[client_id]
                ):
                    live_expiry_by_client[client_id] = expires_at

            results = []
            for client_id, expires_at in live_expiry_by_client.items():
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
