"""Consent and browser-binding rules for the HTTP transport's OAuth flow.

Pure policy, no I/O: the provider/routes (oauth_provider.py) and the store
(oauth_store.py) call into this module, never the other way round.

Why it exists (audit 2026-11-16, finding H2): dynamic client registration is open by
design (RFC 7591), so anyone can register a client with their own redirect URI, start
/authorize themselves and send the resulting link to a victim. Before this module the
pending authorization was looked up by `state` alone, so one click by the victim on
StudyLife's consent page delivered the authorization code to the attacker's redirect
URI. Two rules close that:

1. Consent on this server, before the code is issued: the browser sees which client
   (its self-declared name) and which redirect host will receive access, and has to
   approve it with a CSRF-protected POST.
2. Browser binding: the browser that opens the consent page gets an HttpOnly cookie,
   and the pending authorization is bound to a hash of it. Approval and the StudyLife
   callback are only honored from that same browser, so a `state` started (or approved)
   in another browser can never be completed in the victim's.
"""

import hashlib
import ipaddress
import secrets
from urllib.parse import urlsplit

# Cookie that ties a pending authorization to one browser. HttpOnly + Secure, and
# SameSite=Lax so it still rides along on the top-level GET redirect back from
# StudyLife's /connect/mcp (a cross-site navigation) but never on a cross-site POST.
# Path-scoped to /auth, which covers both the consent page and the StudyLife callback.
BROWSER_BINDING_COOKIE = "studylife_mcp_auth_binding"
BROWSER_BINDING_COOKIE_PATH = "/auth"

# Schemes that are never a legitimate OAuth redirect target for a dynamically registered
# client: script/data URLs, local files, and plain-text transports. `http` is handled
# separately below (allowed for loopback only, RFC 8252 section 7.3).
_FORBIDDEN_REDIRECT_SCHEMES = frozenset(
    {"javascript", "data", "vbscript", "file", "blob", "about", "ftp", "ws", "wss"}
)


def new_browser_binding() -> str:
    """A fresh, unguessable value for BROWSER_BINDING_COOKIE."""
    return secrets.token_urlsafe(32)


def browser_binding_hash(binding: str) -> str:
    """What the store keeps instead of the raw cookie value, so reading the database
    is not enough to forge the cookie of a browser with a flow in progress."""
    return hashlib.sha256(binding.encode()).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_allowed_redirect_uri(redirect_uri: str) -> bool:
    """Registration-time policy for a dynamically registered client's redirect URIs.

    The MCP SDK accepts any absolute URL here. This narrows it to:
    - https to any host (hosted clients such as claude.ai),
    - http only to a loopback host (RFC 8252 section 7.3, desktop clients and CLIs),
    - private-use schemes of native apps (RFC 8252 section 7.1, e.g. ``cursor://``),
      except script/data/file and similar schemes that are never a real callback.

    A URI with a fragment is rejected (RFC 6749 section 3.1.2). This is defense in
    depth: it keeps codes off cleartext transports and out of script URLs, but an
    attacker can still register an https host they own - the consent page is what
    protects against that.
    """
    parts = urlsplit(redirect_uri)
    scheme = parts.scheme.lower()
    if not scheme or parts.fragment:
        return False
    if scheme == "https":
        return bool(parts.hostname)
    if scheme == "http":
        return _is_loopback_host(parts.hostname)
    return scheme not in _FORBIDDEN_REDIRECT_SCHEMES


def describe_redirect_target(redirect_uri: str) -> str:
    """The part of a redirect URI a person can judge on the consent page: the host
    (with port, if any) for web callbacks, a plain-language label for loopback, and the
    scheme for a native app's private-use URI."""
    parts = urlsplit(redirect_uri)
    scheme = parts.scheme.lower()
    if scheme in {"http", "https"}:
        if _is_loopback_host(parts.hostname):
            return f"an app on this computer ({parts.netloc})"
        return parts.netloc
    return f"the app registered for {scheme}://"
