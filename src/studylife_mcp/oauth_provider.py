import html
import secrets
import time

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

from studylife_mcp.client import StudyLifeClient
from studylife_mcp.config import Settings
from studylife_mcp.oauth_store import OAuthStore

# Single scope: no per-tool/read-vs-write scoping exists yet (every tool is available
# to any authenticated caller, same as stdio mode) - revisit if that ever needs to
# differ per client.
SCOPE = "studylife"

ACCESS_TOKEN_TTL_SECONDS = 60 * 60  # 1 hour
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days, rotated on every use
AUTHORIZATION_CODE_TTL_SECONDS = 300  # 5 minutes to complete the token exchange


class StudyLifeOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth 2.1 authorization server for studylife-mcp's HTTP transport.

    The resource-owner login step (`authorize()` below) doesn't ask for a generic
    username/password - it asks for the caller's own StudyLife MCP API key (the one
    generated on StudyLife's setup page, same key stdio mode reads from `.env`).
    Every token this server issues is bound to whichever StudyLife account that key
    belongs to (see `oauth_store.OAuthStore.subject_for_key`), so one deployment of
    this server can serve multiple StudyLife users concurrently without ever mixing
    their data - each access token resolves back to exactly one person's StudyLife
    key at tool-call time (see `server.py`'s `resolve_studylife_client`).

    `authorize()` only returns a redirect URL, per the protocol - the actual login
    page is a separate pair of custom routes, see `register_oauth_routes` below.
    That split is deliberate: swapping this for a federated login (e.g. redirecting
    to Authentik/Keycloak instead) later only touches `authorize()` and the routes
    it points to, not the rest of this class.
    """

    def __init__(self, store: OAuthStore, public_url: str) -> None:
        self._store = store
        self._public_url = public_url.rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._store.register_client(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request_id = secrets.token_urlsafe(24)
        await self._store.save_pending_authorization(
            request_id, client.client_id, params.model_dump_json()
        )
        return f"{self._public_url}/login?request_id={request_id}"

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


def _render_login_page(request_id: str, *, error: str | None = None) -> str:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Connect to StudyLife</title>
<style>
  body {{
    font-family: system-ui, sans-serif; max-width: 28rem; margin: 4rem auto; padding: 0 1rem;
  }}
  input {{ width: 100%; padding: 0.5rem; margin: 0.5rem 0 1rem; box-sizing: border-box; }}
  button {{ padding: 0.5rem 1.5rem; }}
  .error {{ color: #b00020; }}
  .hint {{ color: #555; font-size: 0.9rem; }}
</style>
</head>
<body>
<h1>Connect to StudyLife</h1>
<p>Enter your StudyLife MCP API key to let this client access your StudyLife data.</p>
<p class="hint">Get one from StudyLife's Setup page &rarr; "StudyLife MCP Server" card.</p>
{error_html}
<form method="post" action="/login">
  <input type="hidden" name="request_id" value="{html.escape(request_id)}">
  <label for="api_key">API key</label>
  <input type="password" id="api_key" name="api_key" autocomplete="off" required>
  <button type="submit">Connect</button>
</form>
</body>
</html>
"""


_EXPIRED_LINK_HTML = (
    "<!doctype html><p>This login link is invalid or has expired. "
    "Please restart the connection from your MCP client.</p>"
)


def register_oauth_routes(mcp: MCPServer, store: OAuthStore, settings: Settings) -> None:
    """Registers the two custom routes the `authorize()` redirect and its form
    submission need. Kept separate from the OAuthAuthorizationServerProvider
    Protocol methods above - this is the specific piece that would change if
    login is ever federated to an external IdP instead."""

    # Unlike @mcp.tool(), the SDK's custom_route() has no return-type annotation,
    # so mypy can't see through it - narrow, sanctioned suppression.
    @mcp.custom_route("/login", methods=["GET"])  # type: ignore[untyped-decorator]
    async def login_form(request: Request) -> Response:
        request_id = request.query_params.get("request_id", "")
        if await store.load_pending_authorization(request_id) is None:
            return HTMLResponse(_EXPIRED_LINK_HTML, status_code=400)
        return HTMLResponse(_render_login_page(request_id))

    @mcp.custom_route("/login", methods=["POST"])  # type: ignore[untyped-decorator]
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        api_key = str(form.get("api_key", "")).strip()

        pending = await store.load_pending_authorization(request_id)
        if pending is None:
            return HTMLResponse(_EXPIRED_LINK_HTML, status_code=400)
        client_id, params_json = pending

        if not api_key:
            return HTMLResponse(
                _render_login_page(request_id, error="Please enter your StudyLife MCP API key."),
                status_code=400,
            )

        candidate = StudyLifeClient(settings.model_copy(update={"studylife_api_key": api_key}))
        try:
            await candidate.list_courses()
        except httpx.HTTPError:
            return HTMLResponse(
                _render_login_page(
                    request_id,
                    error="StudyLife rejected this key (or couldn't be reached). "
                    "Check it and try again.",
                ),
                status_code=401,
            )
        finally:
            await candidate.aclose()

        subject = store.subject_for_key(api_key)
        await store.save_user_key(subject, api_key)

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
        await store.delete_pending_authorization(request_id)

        redirect_url = construct_redirect_uri(
            str(params.redirect_uri), code=code.code, state=params.state
        )
        return RedirectResponse(redirect_url, status_code=302)
