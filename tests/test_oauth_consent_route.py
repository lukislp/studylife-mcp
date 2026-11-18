"""Consent step + browser binding of the HTTP transport's OAuth flow (audit 2026-11-16,
finding H2).

The attack these tests pin down: dynamic client registration is open, so an attacker
registers a client with their own redirect URI, starts /authorize, and gets a victim to
complete the flow. Before the fix, the StudyLife callback looked the pending request up
by `state` alone, so the victim's one click sent the authorization code to the
attacker. Each test drives real HTTP requests through the ASGI app; two separate
httpx clients stand in for two separate browsers (separate cookie jars).
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.types import ASGIApp

from studylife_mcp.config import Settings
from studylife_mcp.consent import BROWSER_BINDING_COOKIE
from studylife_mcp.oauth_provider import SCOPE, StudyLifeOAuthProvider, register_oauth_routes
from studylife_mcp.oauth_store import OAuthStore
from studylife_mcp.pkce import challenge_for

BASE_URL = "https://mcp.example.test"
EXCHANGE_URL = "https://studylife.example.test/api/auth/mcp-assertion-exchange"

ATTACKER_CLIENT = OAuthClientInformationFull(
    client_id="attacker-client",
    client_name="Claude",  # self-declared, so it can be anything - hence the host line
    redirect_uris=[AnyUrl("https://attacker.example/cb")],
)
LEGIT_CLIENT = OAuthClientInformationFull(
    client_id="legit-client",
    client_name="Legit <b>App</b>",
    redirect_uris=[AnyUrl("https://client.example/callback")],
)


@pytest.fixture
async def store(tmp_path: Path) -> OAuthStore:
    s = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await s.initialize()
    await s.register_client(ATTACKER_CLIENT)
    await s.register_client(LEGIT_CLIENT)
    return s


@pytest.fixture
def provider(store: OAuthStore) -> StudyLifeOAuthProvider:
    return StudyLifeOAuthProvider(store, BASE_URL, "https://connect.studylife.example.test")


@pytest.fixture
def app(store: OAuthStore, settings: Settings, provider: StudyLifeOAuthProvider) -> ASGIApp:
    mcp = MCPServer(
        "studylife-mcp-test",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyUrl(BASE_URL),
            resource_server_url=AnyUrl(BASE_URL),
            client_registration_options=ClientRegistrationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )
    register_oauth_routes(mcp, store, settings, provider)
    return mcp.streamable_http_app()


@pytest.fixture
async def victim(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


@pytest.fixture
async def attacker(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as c:
        yield c


async def _authorize(provider: StudyLifeOAuthProvider, client: OAuthClientInformationFull) -> str:
    """Runs authorize() as the SDK's /authorize handler would and returns the
    request_id from the consent URL it redirects to."""
    params = AuthorizationParams(
        state="client-state",
        scopes=[SCOPE],
        code_challenge="challenge",
        redirect_uri=AnyUrl(str((client.redirect_uris or [])[0])),
        redirect_uri_provided_explicitly=True,
    )
    url = await provider.authorize(client, params)
    assert url.startswith(f"{BASE_URL}/auth/consent?")
    return parse_qs(urlparse(url).query)["request_id"][0]


def _csrf_from(page: httpx.Response) -> str:
    return page.text.split('name="csrf_token" value="')[1].split('"')[0]


async def _open_and_approve(browser: httpx.AsyncClient, request_id: str) -> httpx.Response:
    page = await browser.get("/auth/consent", params={"request_id": request_id})
    assert page.status_code == 200
    return await browser.post(
        "/auth/consent",
        data={"request_id": request_id, "csrf_token": _csrf_from(page), "decision": "approve"},
        follow_redirects=False,
    )


def _mock_exchange() -> respx.Route:
    return respx.post(EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"userId": 42, "mcpApiKey": "rotated-key"})
    )


# --- happy path -------------------------------------------------------------------


@respx.mock
async def test_happy_path_consent_then_callback_issues_code_to_same_browser(
    victim: httpx.AsyncClient, store: OAuthStore, provider: StudyLifeOAuthProvider
) -> None:
    exchange = _mock_exchange()
    request_id = await _authorize(provider, LEGIT_CLIENT)

    page = await victim.get("/auth/consent", params={"request_id": request_id})
    assert page.status_code == 200
    # Client name (escaped) and the redirect host are what the person decides on.
    assert "Legit &lt;b&gt;App&lt;/b&gt;" in page.text
    assert "<b>App</b>" not in page.text
    assert "client.example" in page.text
    # The public brand, not the former product name.
    assert "Allow access to your StudyAtrium account?" in page.text
    assert "StudyLife" not in page.text
    # Clickjacking / caching protection on a one-click grant page.
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    set_cookie = page.headers["set-cookie"].lower()
    assert f"{BROWSER_BINDING_COOKIE}=" in set_cookie
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/auth" in set_cookie

    approve = await victim.post(
        "/auth/consent",
        data={"request_id": request_id, "csrf_token": _csrf_from(page), "decision": "approve"},
        follow_redirects=False,
    )
    assert approve.status_code == 303
    connect = urlparse(approve.headers["location"])
    assert connect.netloc == "connect.studylife.example.test"
    assert connect.path == "/connect/mcp"
    connect_query = parse_qs(connect.query)
    assert connect_query["state"] == [request_id]
    assert connect_query["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in connect_query
    # The verifier is held server-side with the pending state, not in the URL or a cookie.
    stored_verifier = await store.take_code_verifier(request_id)
    assert stored_verifier is not None
    assert connect_query["code_challenge"] == [challenge_for(stored_verifier)]
    assert stored_verifier not in approve.headers["location"]
    assert stored_verifier not in "".join(approve.headers.get_list("set-cookie"))
    # Put it back for the callback below (take consumed it).
    await store.save_code_verifier(request_id, stored_verifier)

    callback = await victim.get(
        "/auth/studylife/callback",
        params={"assertion": "single-use", "state": request_id},
        follow_redirects=False,
    )

    assert callback.status_code == 302
    redirect = urlparse(callback.headers["location"])
    assert redirect.netloc == "client.example"
    query = parse_qs(redirect.query)
    assert "code" in query
    assert query["state"] == ["client-state"]
    assert exchange.call_count == 1
    assert json.loads(exchange.calls.last.request.content) == {
        "assertion": "single-use",
        "codeVerifier": stored_verifier,
    }
    # Consumed by the exchange along with the pending authorization.
    assert await store.take_code_verifier(request_id) is None
    assert await store.load_pending_authorization(request_id) is None


async def test_reopening_consent_in_same_browser_reuses_binding(
    victim: httpx.AsyncClient, provider: StudyLifeOAuthProvider
) -> None:
    request_id = await _authorize(provider, LEGIT_CLIENT)

    first = await victim.get("/auth/consent", params={"request_id": request_id})
    second = await victim.get("/auth/consent", params={"request_id": request_id})

    assert second.status_code == 200
    assert _csrf_from(first) == _csrf_from(second)


@respx.mock
async def test_deny_redirects_access_denied_and_consumes_request(
    victim: httpx.AsyncClient, store: OAuthStore, provider: StudyLifeOAuthProvider
) -> None:
    request_id = await _authorize(provider, LEGIT_CLIENT)
    page = await victim.get("/auth/consent", params={"request_id": request_id})

    deny = await victim.post(
        "/auth/consent",
        data={"request_id": request_id, "csrf_token": _csrf_from(page), "decision": "deny"},
        follow_redirects=False,
    )

    assert deny.status_code == 303
    redirect = urlparse(deny.headers["location"])
    assert redirect.netloc == "client.example"
    assert parse_qs(redirect.query) == {"error": ["access_denied"], "state": ["client-state"]}
    assert await store.load_pending_authorization(request_id) is None


async def test_consent_unknown_request_returns_400(victim: httpx.AsyncClient) -> None:
    response = await victim.get("/auth/consent", params={"request_id": "nope"})

    assert response.status_code == 400


# --- the attack -------------------------------------------------------------------


@respx.mock
async def test_state_approved_in_attacker_browser_is_rejected_in_victim_browser(
    victim: httpx.AsyncClient,
    attacker: httpx.AsyncClient,
    store: OAuthStore,
    provider: StudyLifeOAuthProvider,
) -> None:
    """The attacker starts the flow, approves it on the consent page in their own
    browser, and sends the victim the resulting StudyLife /connect/mcp link. The victim
    logs in at StudyLife, which redirects the victim's browser to our callback. No
    binding cookie for this request there - no code, assertion not redeemed."""
    exchange = _mock_exchange()
    request_id = await _authorize(provider, ATTACKER_CLIENT)
    assert (await _open_and_approve(attacker, request_id)).status_code == 303

    callback = await victim.get(
        "/auth/studylife/callback",
        params={"assertion": "victims-assertion", "state": request_id},
        follow_redirects=False,
    )

    assert callback.status_code == 403
    assert "location" not in callback.headers
    assert "attacker.example" not in callback.text
    assert exchange.call_count == 0
    assert await store.load_user_key("42") is None


@respx.mock
async def test_state_from_another_session_rejected_even_with_own_binding_cookie(
    victim: httpx.AsyncClient,
    attacker: httpx.AsyncClient,
    provider: StudyLifeOAuthProvider,
) -> None:
    """The victim has a binding cookie of their own (from their own, legitimate flow).
    That cookie must not unlock a request approved in another browser."""
    exchange = _mock_exchange()
    victims_own = await _authorize(provider, LEGIT_CLIENT)
    assert (await _open_and_approve(victim, victims_own)).status_code == 303
    attackers = await _authorize(provider, ATTACKER_CLIENT)
    assert (await _open_and_approve(attacker, attackers)).status_code == 303

    callback = await victim.get(
        "/auth/studylife/callback",
        params={"assertion": "victims-assertion", "state": attackers},
        follow_redirects=False,
    )

    assert callback.status_code == 403
    assert exchange.call_count == 0


@respx.mock
async def test_callback_without_consent_approval_issues_no_code(
    victim: httpx.AsyncClient, store: OAuthStore, provider: StudyLifeOAuthProvider
) -> None:
    """Skipping the consent page (state taken straight to StudyLife) never works -
    neither without a cookie nor after only viewing the consent page."""
    exchange = _mock_exchange()
    request_id = await _authorize(provider, ATTACKER_CLIENT)

    without_cookie = await victim.get(
        "/auth/studylife/callback",
        params={"assertion": "a", "state": request_id},
        follow_redirects=False,
    )
    await victim.get("/auth/consent", params={"request_id": request_id})  # viewed, not approved
    viewed_only = await victim.get(
        "/auth/studylife/callback",
        params={"assertion": "a", "state": request_id},
        follow_redirects=False,
    )

    assert without_cookie.status_code == 403
    assert viewed_only.status_code == 403
    assert exchange.call_count == 0
    assert await store.load_pending_authorization(request_id) is not None


async def test_consent_link_opened_by_attacker_first_cannot_be_approved_by_victim(
    victim: httpx.AsyncClient, attacker: httpx.AsyncClient, provider: StudyLifeOAuthProvider
) -> None:
    """First browser wins: once the attacker opened the consent page, the link is dead
    for everyone else - no form, no CSRF token for the victim."""
    request_id = await _authorize(provider, ATTACKER_CLIENT)
    attacker_page = await attacker.get("/auth/consent", params={"request_id": request_id})
    assert attacker_page.status_code == 200

    victim_page = await victim.get("/auth/consent", params={"request_id": request_id})

    assert victim_page.status_code == 403
    assert 'name="csrf_token"' not in victim_page.text
    # Even with the attacker's leaked CSRF token, the victim's browser can't approve.
    approve = await victim.post(
        "/auth/consent",
        data={
            "request_id": request_id,
            "csrf_token": _csrf_from(attacker_page),
            "decision": "approve",
        },
        follow_redirects=False,
    )
    assert approve.status_code == 403


async def test_approve_without_binding_cookie_is_rejected(
    victim: httpx.AsyncClient, attacker: httpx.AsyncClient, provider: StudyLifeOAuthProvider
) -> None:
    """A cross-site form post (SameSite=Lax keeps the cookie off it) carrying a valid
    request_id and CSRF token still can't approve."""
    request_id = await _authorize(provider, LEGIT_CLIENT)
    page = await victim.get("/auth/consent", params={"request_id": request_id})

    forged = await attacker.post(
        "/auth/consent",
        data={"request_id": request_id, "csrf_token": _csrf_from(page), "decision": "approve"},
        follow_redirects=False,
    )

    assert forged.status_code == 403


@respx.mock
async def test_approve_with_wrong_csrf_token_is_rejected(
    victim: httpx.AsyncClient, provider: StudyLifeOAuthProvider
) -> None:
    exchange = _mock_exchange()
    request_id = await _authorize(provider, LEGIT_CLIENT)
    await victim.get("/auth/consent", params={"request_id": request_id})

    approve = await victim.post(
        "/auth/consent",
        data={"request_id": request_id, "csrf_token": "guessed", "decision": "approve"},
        follow_redirects=False,
    )
    callback = await victim.get(
        "/auth/studylife/callback",
        params={"assertion": "a", "state": request_id},
        follow_redirects=False,
    )

    assert approve.status_code == 403
    assert callback.status_code == 403
    assert exchange.call_count == 0
