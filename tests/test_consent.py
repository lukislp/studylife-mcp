import pytest

from studylife_mcp.consent import (
    browser_binding_hash,
    describe_redirect_target,
    is_allowed_redirect_uri,
    new_browser_binding,
)


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://example.com:8443/cb?x=1",
        "http://127.0.0.1:33418/callback",
        "http://localhost:6274/oauth/callback",
        "http://[::1]:8080/cb",
        "cursor://anysphere.cursor-mcp/oauth/callback",
        "com.example.app:/oauth2redirect",
    ],
)
def test_allowed_redirect_uris(uri: str) -> None:
    assert is_allowed_redirect_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "http://attacker.example/cb",
        "http://127.0.0.1.attacker.example/cb",
        "https://example.com/cb#fragment",
        "javascript:alert(1)",
        "JavaScript://x/%0Aalert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "vbscript:msgbox",
        "https:///no-host",
        "not a url",
    ],
)
def test_rejected_redirect_uris(uri: str) -> None:
    assert not is_allowed_redirect_uri(uri)


def test_describe_redirect_target() -> None:
    assert describe_redirect_target("https://claude.ai/api/mcp/auth_callback") == "claude.ai"
    assert describe_redirect_target("https://evil.example:8443/cb") == "evil.example:8443"
    assert "this computer" in describe_redirect_target("http://127.0.0.1:5000/cb")
    assert describe_redirect_target("cursor://x/cb") == "the app registered for cursor://"


def test_browser_binding_is_random_and_hash_is_stable() -> None:
    a, b = new_browser_binding(), new_browser_binding()
    assert a != b
    assert browser_binding_hash(a) == browser_binding_hash(a)
    assert browser_binding_hash(a) != a
