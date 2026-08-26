import hmac
import html
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from studylife_mcp.client import StudyLifeClient, exchange_mcp_assertion
from studylife_mcp.config import Settings
from studylife_mcp.oauth_store import (
    MANAGEMENT_SESSION_TTL_SECONDS,
    ConnectedClient,
    OAuthStore,
)

# Single scope: no per-tool/read-vs-write scoping exists yet (every tool is available
# to any authenticated caller, same as stdio mode) - revisit if that ever needs to
# differ per client.
SCOPE = "studylife"

ACCESS_TOKEN_TTL_SECONDS = 60 * 60  # 1 hour
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days, rotated on every use
AUTHORIZATION_CODE_TTL_SECONDS = 300  # 5 minutes to complete the token exchange

# /connected-apps' management session id (audit A15 item 3) - carried as a cookie rather
# than the URL query param it used to be, since a query param ends up in browser history,
# the Referer header of any link/asset the page loads, and this server's own access logs.
# HttpOnly (JS on the page can't read it - not that this page has any of its own, but no
# reason to allow it), Secure (only sent back over HTTPS - mcp_public_url is always https,
# see config.py), SameSite=Lax (sent on the plain-GET navigations this flow depends on -
# following the /connected-apps link, the login form's redirect - but not attached to a
# cross-site POST, which is exactly the CSRF surface item 4 below closes the rest of the
# way), and Path-scoped to MANAGEMENT_COOKIE_PATH so it's never sent to /mcp or any other
# route on this server.
MANAGEMENT_SESSION_COOKIE = "studylife_mcp_mgmt_session"
MANAGEMENT_COOKIE_PATH = "/connected-apps"


class StudyLifeOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth 2.1 authorization server for studylife-mcp's HTTP transport.

    The resource-owner login step (`authorize()` below) redirects the browser to
    StudyLife's own `/connect/mcp` page (identity-contract-v1 section 2) - StudyLife
    handles the passkey login and consent, then hands back a single-use assertion this
    server exchanges server-to-server for the real StudyLife user id and a freshly
    rotated MCP API key. The subject every issued token is bound to is that user id
    (`str(userId)`), not a hash of the key - see the `/auth/studylife/callback` route in
    `register_oauth_routes` below, where the exchange and the subject binding happen.
    Older grants made before this change have subject = sha256(key)
    (`OAuthStore.subject_for_key`, still used by `/connected-apps`'s own key-based
    re-auth) and keep resolving untouched; only new logins get a userId subject.

    `authorize()` only returns a redirect URL, per the protocol - the actual
    login/consent UI lives entirely on StudyLife's side now, reached via the routes in
    `register_oauth_routes` below.
    """

    def __init__(self, store: OAuthStore, public_url: str, connect_url: str) -> None:
        self._store = store
        self._public_url = public_url.rstrip("/")
        # StudyLife's own public base URL - authorize() sends the browser to
        # {connect_url}/connect/mcp (identity-contract-v1 section 2 step 1).
        self._connect_url = connect_url.rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._store.register_client(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # request_id doubles as the "state" StudyLife echoes back on
        # /auth/studylife/callback - same round-trip mechanics the old /login form used
        # (a pending_auth row keyed by an opaque id), just carried via a query param
        # through StudyLife instead of a form post to this server.
        request_id = secrets.token_urlsafe(24)
        await self._store.save_pending_authorization(
            request_id, client.client_id, params.model_dump_json()
        )
        callback_url = f"{self._public_url}/auth/studylife/callback"
        query = urlencode({"redirect_uri": callback_url, "state": request_id})
        return f"{self._connect_url}/connect/mcp?{query}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await self._store.load_authorization_code(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Authorization codes are single-use (RFC 6749 §10.5).
        await self._store.delete_authorization_code(authorization_code.code)
        return await self._issue_tokens(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await self._store.load_refresh_token(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate on use: the old refresh token stops working once exchanged.
        await self._store.delete_refresh_token(refresh_token.token)
        return await self._issue_tokens(
            client_id=client.client_id,
            scopes=scopes,
            resource=None,
            subject=refresh_token.subject,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = await self._store.load_access_token(token)
        if access_token is None:
            return None
        if access_token.expires_at is not None and access_token.expires_at < time.time():
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            await self._store.delete_access_token(token.token)
        else:
            await self._store.delete_refresh_token(token.token)

    async def _issue_tokens(
        self, *, client_id: str, scopes: list[str], resource: str | None, subject: str | None
    ) -> OAuthToken:
        now = time.time()
        access_token = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL_SECONDS),
            resource=resource,
            subject=subject,
        )
        refresh_token = RefreshToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL_SECONDS),
            subject=subject,
        )
        await self._store.save_access_token(access_token)
        await self._store.save_refresh_token(refresh_token)
        return OAuthToken(
            access_token=access_token.token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token.token,
            scope=" ".join(scopes) if scopes else None,
        )


# Mirrors StudyLife's own design system (StudyLife.Client/wwwroot/css/base.css +
# shared.css's .input/.btn-primary/.modal) rather than inventing a separate look for this
# page - same font/color tokens, same card shell, same brand mark ("✦ StudyLife"), so the
# login screen a caller lands on feels like part of StudyLife, not a generic OAuth form.
# Duplicated here (not shared via a stylesheet link) because this page is served standalone
# by this server, not by StudyLife itself.
_PAGE_STYLE = """
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;1,9..40,300&display=swap');
  :root {
    color-scheme: dark light;
    --font: 'DM Sans', sans-serif;
    --bg: #0e0e0f; --bg2: #161618; --bg3: #1e1e21;
    --border: rgba(255,255,255,0.07); --border2: rgba(255,255,255,0.12);
    --text: #e8e6e0; --text2: #9d9b93; --text3: #6b6965;
    --accent: #CC785C;
    --transition: 0.2s ease;
  }
  @media (prefers-color-scheme: light) {
    :root {
      color-scheme: light;
      --bg: #f4f2ee; --bg2: #ffffff; --bg3: #ebe8e2;
      --border: rgba(0,0,0,0.06); --border2: rgba(0,0,0,0.12);
      --text: #1a1916; --text2: #5a5752; --text3: #9a9892;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; padding: 1.5rem;
    display: flex; align-items: center; justify-content: center;
    font-family: var(--font); background: var(--bg); color: var(--text);
    -webkit-font-smoothing: antialiased;
  }
  .card {
    background: var(--bg2); border: 1px solid var(--border2); border-radius: 16px;
    width: 400px; max-width: 100%; padding: 1.75rem;
  }
  .brand { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1.5rem; }
  .brand-icon { font-size: 1.3rem; color: var(--accent); }
  .brand-name { font-size: 1rem; font-weight: 500; letter-spacing: -0.01em; }
  h1 { font-size: 1.4rem; font-weight: 300; letter-spacing: -0.02em; margin: 0 0 0.6rem; }
  p { margin: 0 0 0.5rem; font-size: 0.875rem; color: var(--text2); line-height: 1.5; }
  .hint { font-size: 0.8rem; color: var(--text3); margin-bottom: 1.25rem; }
  label {
    display: block; font-size: 0.7rem; font-weight: 500; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--text3); margin-bottom: 0.5rem;
  }
  .input {
    background: var(--bg3); border: 1px solid var(--border); border-radius: 8px;
    padding: 0.6rem 0.875rem; font-family: var(--font); font-size: 0.875rem; color: var(--text);
    outline: none; transition: var(--transition); width: 100%; margin-bottom: 1.25rem;
  }
  .input:focus { border-color: var(--accent); }
  .btn-primary {
    background: var(--accent); color: white; border: none; border-radius: 8px;
    padding: 0.65rem 1.25rem; font-size: 0.875rem; font-weight: 500; font-family: var(--font);
    cursor: pointer; transition: var(--transition); width: 100%;
  }
  .btn-primary:hover { opacity: 0.9; }
  .error {
    font-size: 0.8rem; color: #E17055; background: rgba(225,112,85,0.1);
    border: 1px solid rgba(225,112,85,0.3); border-radius: 8px; padding: 0.6rem 0.8rem;
    margin-bottom: 1.25rem;
  }
  .btn-danger {
    background: transparent; color: #E17055; border: 1px solid rgba(225,112,85,0.3);
    border-radius: 8px; padding: 0.4rem 0.85rem; font-size: 0.8rem; font-family: var(--font);
    cursor: pointer; transition: var(--transition); flex-shrink: 0;
  }
  .btn-danger:hover { background: rgba(225,112,85,0.1); }
  .app-row {
    display: flex; align-items: center; justify-content: space-between; gap: 0.75rem;
    padding: 0.85rem 0; border-top: 1px solid var(--border);
  }
  .app-row:first-of-type { border-top: none; }
  .app-name { font-size: 0.9rem; font-weight: 500; }
  .app-expires { font-size: 0.75rem; color: var(--text3); margin-top: 2px; }
"""

_BRAND_HTML = (
    '<div class="brand"><span class="brand-icon">&#10022;</span>'
    '<span class="brand-name">StudyLife</span></div>'
)


async def _validate_studylife_key(settings: Settings, api_key: str) -> bool:
    """Used by /connected-apps: proves the caller owns a StudyLife account by checking
    the key actually works against the real instance. The main connect flow no longer
    needs this - StudyLife itself verifies the session before issuing an assertion -
    but /connected-apps still re-proves ownership with a raw key (see its routes
    below), so this stays."""
    candidate = StudyLifeClient(settings.model_copy(update={"studylife_api_key": api_key}))
    try:
        await candidate.list_courses()
    except httpx.HTTPError:
        return False
    finally:
        await candidate.aclose()
    return True


_EXPIRED_LINK_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link expired &mdash; StudyLife</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="card">
{_BRAND_HTML}
<h1>Link expired</h1>
<p>This connection link is invalid or has expired. Please restart the connection
from your MCP client.</p>
</div>
</body>
</html>
"""

_CONNECT_FAILED_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connection failed &mdash; StudyLife</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="card">
{_BRAND_HTML}
<h1>Connection failed</h1>
<p>StudyLife could not confirm this connection. Please restart it from your MCP client.</p>
</div>
</body>
</html>
"""


def _format_expiry(expires_at: float | None) -> str:
    if expires_at is None:
        return "No expiry set"
    return f"Active until {datetime.fromtimestamp(expires_at, tz=UTC):%Y-%m-%d}"


def _set_management_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        MANAGEMENT_SESSION_COOKIE,
        session_id,
        max_age=MANAGEMENT_SESSION_TTL_SECONDS,
        path=MANAGEMENT_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_management_session_cookie(response: Response) -> None:
    """Used whenever a session id turns out to be invalid/expired - drops the dead
    cookie instead of leaving it around for the browser to keep re-sending on every
    future /connected-apps visit."""
    response.delete_cookie(MANAGEMENT_SESSION_COOKIE, path=MANAGEMENT_COOKIE_PATH)


def _render_connected_apps_login_page(*, error: str | None = None) -> str:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connected Apps &mdash; StudyLife</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="card">
{_BRAND_HTML}
<h1>Connected apps</h1>
<p>Enter your StudyLife MCP API key to see which apps currently have access
to your StudyLife account, and revoke any you no longer recognize.</p>
{error_html}
<form method="post" action="/connected-apps">
  <label for="api_key">API key</label>
  <input class="input" type="password" id="api_key" name="api_key" autocomplete="off" required>
  <button class="btn-primary" type="submit">View connected apps</button>
</form>
</div>
</body>
</html>
"""


def _render_connected_apps_list_page(clients: list[ConnectedClient], csrf_token: str) -> str:
    # session_id is no longer threaded through this form (audit A15 item 3) - the
    # browser already sends it automatically via MANAGEMENT_SESSION_COOKIE on this POST,
    # since the form's action is under MANAGEMENT_COOKIE_PATH. csrf_token still has to be
    # a hidden field (audit A15 item 4): unlike the cookie, it must NOT be something the
    # browser attaches on its own to a forged cross-site request - the whole point is
    # that only a same-origin page render (this one) knows it.
    if clients:
        body = "\n".join(
            f"""<div class="app-row">
  <div>
    <div class="app-name">{html.escape(c.client_name)}</div>
    <div class="app-expires">{_format_expiry(c.expires_at)}</div>
  </div>
  <form method="post" action="/connected-apps/revoke">
    <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
    <input type="hidden" name="client_id" value="{html.escape(c.client_id)}">
    <button class="btn-danger" type="submit">Revoke</button>
  </form>
</div>"""
            for c in clients
        )
    else:
        body = '<p class="hint">No apps currently have access to your StudyLife account.</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connected Apps &mdash; StudyLife</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
<div class="card">
{_BRAND_HTML}
<h1>Connected apps</h1>
<p>Apps with current access to your StudyLife account. Revoking one signs it
out immediately - it has to go through login again to reconnect.</p>
{body}
</div>
</body>
</html>
"""


def register_oauth_routes(mcp: MCPServer, store: OAuthStore, settings: Settings) -> None:
    """Registers the custom route the `authorize()` redirect's round trip needs, plus
    the self-service connected-apps pages. Kept separate from the
    OAuthAuthorizationServerProvider Protocol methods above - this is the specific piece
    that would change again if login were ever federated to a different IdP."""

    # Unlike @mcp.tool(), the SDK's custom_route() has no return-type annotation,
    # so mypy can't see through it - narrow, sanctioned suppression.

    # Completes the connect flow authorize() started (identity-contract-v1 section 2 step
    # 5): StudyLife's /connect/mcp redirects the browser back here once the user has
    # logged in and consented, carrying a single-use assertion and the state (our own
    # pending-authorization request_id) round-tripped through it unchanged.
    @mcp.custom_route("/auth/studylife/callback", methods=["GET"])  # type: ignore[untyped-decorator]
    async def studylife_callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        assertion = request.query_params.get("assertion", "")

        pending = await store.load_pending_authorization(state)
        if pending is None:
            return HTMLResponse(_EXPIRED_LINK_HTML, status_code=400)
        client_id, params_json = pending

        if not assertion:
            return HTMLResponse(_CONNECT_FAILED_HTML, status_code=400)

        exchanged = await exchange_mcp_assertion(settings, assertion)
        if exchanged is None:
            return HTMLResponse(_CONNECT_FAILED_HTML, status_code=401)
        user_id, mcp_api_key = exchanged

        # The real StudyLife AuthUserId, not a hash of the key (audit A1) - upsert so a
        # re-connect (e.g. after StudyLife rotated the key) replaces the stored key
        # rather than creating a second identity for the same person.
        subject = str(user_id)
        await store.save_user_key(subject, mcp_api_key)

        params = AuthorizationParams.model_validate_json(params_json)
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + AUTHORIZATION_CODE_TTL_SECONDS,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=subject,
        )
        await store.save_authorization_code(code)
        await store.delete_pending_authorization(state)

        redirect_url = construct_redirect_uri(
            str(params.redirect_uri), code=code.code, state=params.state
        )
        return RedirectResponse(redirect_url, status_code=302)

    # Self-service "which apps have access to my StudyLife account, revoke one" page.
    # Deliberately NOT reachable via the public Tailscale Funnel route (see
    # k8s/07-tailscale-funnel.yaml's path allowlist, which omits /connected-apps) - only
    # from the tailnet/LAN-only studylife-mcp.heim.lan route. Re-proves ownership with a
    # real StudyLife key (still validated the old way, see _validate_studylife_key)
    # rather than trusting anything from the already-revealed access token, since the
    # whole point of this page is to let someone audit/undo access even if a token were
    # compromised.
    @mcp.custom_route("/connected-apps", methods=["GET"])  # type: ignore[untyped-decorator]
    async def connected_apps_view(request: Request) -> Response:
        cookie_session_id = request.cookies.get(MANAGEMENT_SESSION_COOKIE)
        if cookie_session_id is None:
            # Transition for a bookmark/link from before session_id moved out of the
            # URL and into a cookie (audit A15 item 3): accept it once, immediately move
            # it into the cookie, and redirect to the clean URL so it stops appearing in
            # browser history / the Referer header / this server's own access logs from
            # here on.
            query_session_id = request.query_params.get("session_id")
            if query_session_id:
                redirect: Response = RedirectResponse("/connected-apps", status_code=302)
                _set_management_session_cookie(redirect, query_session_id)
                return redirect
            return HTMLResponse(_render_connected_apps_login_page())

        subject = await store.load_management_session(cookie_session_id)
        if subject is None:
            response = HTMLResponse(_render_connected_apps_login_page())
            _clear_management_session_cookie(response)
            return response

        csrf_token = await store.load_management_session_csrf_token(cookie_session_id) or ""
        clients = await store.list_connected_clients(subject)
        return HTMLResponse(_render_connected_apps_list_page(clients, csrf_token))

    @mcp.custom_route("/connected-apps", methods=["POST"])  # type: ignore[untyped-decorator]
    async def connected_apps_login(request: Request) -> Response:
        form = await request.form()
        api_key = str(form.get("api_key", "")).strip()

        if not api_key:
            return HTMLResponse(
                _render_connected_apps_login_page(error="Please enter your StudyLife MCP API key."),
                status_code=400,
            )
        if not await _validate_studylife_key(settings, api_key):
            return HTMLResponse(
                _render_connected_apps_login_page(
                    error="StudyLife rejected this key (or couldn't be reached). "
                    "Check it and try again."
                ),
                status_code=401,
            )

        subject = store.subject_for_key(api_key)
        # csrf_token isn't needed here - it travels only as far as the GET this redirect
        # lands on, which fetches it fresh (load_management_session_csrf_token below).
        session_id, _csrf_token = await store.create_management_session(subject)
        response = RedirectResponse("/connected-apps", status_code=303)
        _set_management_session_cookie(response, session_id)
        return response

    @mcp.custom_route("/connected-apps/revoke", methods=["POST"])  # type: ignore[untyped-decorator]
    async def connected_apps_revoke(request: Request) -> Response:
        form = await request.form()
        client_id = str(form.get("client_id", ""))
        submitted_csrf_token = str(form.get("csrf_token", ""))
        # Cookie is the normal source; a form-carried session_id is only accepted as a
        # fallback for a /connected-apps page rendered by the *previous* server version
        # (hidden session_id field, no cookie set) that a browser still has open across
        # a deploy - _render_connected_apps_list_page never renders that field anymore,
        # so a freshly-loaded page always relies on the cookie alone.
        session_id = request.cookies.get(MANAGEMENT_SESSION_COOKIE) or str(
            form.get("session_id", "")
        )

        subject = await store.load_management_session(session_id) if session_id else None
        if subject is None:
            response: Response = HTMLResponse(_render_connected_apps_login_page(), status_code=400)
            _clear_management_session_cookie(response)
            return response

        # CSRF check (audit A15 item 4): the session cookie alone doesn't prove this POST
        # was submitted from a page this server rendered - a browser attaches cookies to
        # a cross-site forged form post too. The csrf_token hidden field only ends up in
        # a request if whoever sent it first loaded /connected-apps and read it out of
        # that page, which a third-party site can't do (no way to read another origin's
        # response body). Constant-time compare so a byte-by-byte timing difference can't
        # leak the token faster than brute-forcing its full 24 bytes of entropy.
        stored_csrf_token = await store.load_management_session_csrf_token(session_id)
        csrf_ok = stored_csrf_token and hmac.compare_digest(submitted_csrf_token, stored_csrf_token)
        if not csrf_ok:
            return HTMLResponse(
                _render_connected_apps_login_page(
                    error="Your session could not be verified. Please sign in again."
                ),
                status_code=403,
            )

        await store.revoke_client_access(subject, client_id)
        response = RedirectResponse("/connected-apps", status_code=303)
        _set_management_session_cookie(response, session_id)
        return response
