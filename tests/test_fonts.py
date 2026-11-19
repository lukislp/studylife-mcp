"""GDPR audit 2026-11-19, S11: the pages must not load fonts (or anything else) from a
third-party host, and the self-hosted font files must actually be served."""

import re
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyUrl

from studylife_mcp import oauth_provider
from studylife_mcp.config import Settings
from studylife_mcp.fonts import (
    FONT_CONTENT_TYPE,
    FONT_DIR,
    FONT_FILES,
    FONT_URL_PREFIX,
    font_face_css,
)
from studylife_mcp.oauth_provider import SCOPE, StudyLifeOAuthProvider, register_oauth_routes
from studylife_mcp.oauth_store import ConnectedClient, OAuthStore

THIRD_PARTY_FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def _rendered_pages() -> dict[str, str]:
    return {
        "expired": oauth_provider._EXPIRED_LINK_HTML,
        "connect_failed": oauth_provider._CONNECT_FAILED_HTML,
        "other_browser": oauth_provider._OTHER_BROWSER_HTML,
        "consent": oauth_provider._render_consent_page(
            request_id="r", csrf_token="c", client_name="Client", redirect_target="example.test"
        ),
        "connected_apps_login": oauth_provider._render_connected_apps_login_page(),
        "connected_apps_list": oauth_provider._render_connected_apps_list_page(
            [ConnectedClient(client_id="id", client_name="Client", expires_at=None)], "csrf"
        ),
    }


@pytest.mark.parametrize("name", sorted(_rendered_pages()))
def test_page_references_no_external_host(name: str) -> None:
    page = _rendered_pages()[name]
    for host in THIRD_PARTY_FONT_HOSTS:
        assert host not in page
    # No stylesheet, font or script may come from an absolute URL; the only src/href/url()
    # targets are this server's own paths.
    assert not re.search(r"""(?:src|href|action)=["']https?://""", page)
    assert not re.search(r"""url\(\s*["']?(?:https?:)?//""", page)
    assert "@import" not in page


def test_font_face_css_points_at_own_route() -> None:
    css = font_face_css()
    urls = re.findall(r"url\('([^']+)'\)", css)
    assert urls
    assert all(url.startswith(FONT_URL_PREFIX + "/") for url in urls)
    assert {url.rsplit("/", 1)[1] for url in urls} == set(FONT_FILES)


def test_vendored_fonts_and_license_exist() -> None:
    for name in FONT_FILES:
        assert (FONT_DIR / name).read_bytes()[:4] == b"wOF2"
    assert "SIL OPEN FONT LICENSE" in (FONT_DIR / "OFL.txt").read_text(encoding="utf-8")


@pytest.fixture
async def client(tmp_path: Path, settings: Settings):
    store = OAuthStore(str(tmp_path / "oauth.db"), Fernet.generate_key().decode())
    await store.initialize()
    provider = StudyLifeOAuthProvider(
        store, "https://mcp.example.test", "https://connect.studylife.example.test"
    )
    mcp = MCPServer(
        "studylife-mcp-test",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyUrl("https://mcp.example.test"),
            resource_server_url=AnyUrl("https://mcp.example.test"),
            client_registration_options=ClientRegistrationOptions(enabled=True),
            required_scopes=[SCOPE],
        ),
    )
    register_oauth_routes(mcp, store, settings, provider)
    transport = httpx.ASGITransport(app=mcp.streamable_http_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.example.test") as c:
        yield c


@pytest.mark.parametrize("name", sorted(FONT_FILES))
async def test_font_is_served_as_woff2(client: httpx.AsyncClient, name: str) -> None:
    response = await client.get(f"{FONT_URL_PREFIX}/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == FONT_CONTENT_TYPE
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == (FONT_DIR / name).read_bytes()


@pytest.mark.parametrize("name", ["OFL.txt", "..%2Foauth_provider.py", "missing.woff2"])
async def test_only_known_font_files_are_served(client: httpx.AsyncClient, name: str) -> None:
    response = await client.get(f"{FONT_URL_PREFIX}/{name}")
    assert response.status_code == 404


def test_consent_csp_limits_fonts_to_self() -> None:
    assert "font-src 'self'" in oauth_provider._CONSENT_PAGE_HEADERS["Content-Security-Policy"]
