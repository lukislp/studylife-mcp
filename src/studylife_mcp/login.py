"""Browser-based login bootstrap for stdio mode (`studylife-mcp-login` entry point).

Before this existed, a stdio user had to open StudyLife's setup page by hand, generate
an MCP API key, and paste it into `.env` (still documented in the README as a fallback).
This module drives the same round trip HTTP+OAuth mode already uses
(`oauth_provider.py`'s `/auth/studylife/callback`), just from the CLI instead of a
running server: it opens the user's browser to StudyLife's own `/connect/mcp` consent
page with a loopback redirect_uri (RFC 8252 - `http://127.0.0.1:<port>/callback`),
receives the resulting single-use assertion on a short-lived local HTTP listener,
exchanges it server-to-server for a freshly rotated MCP API key
(`client.exchange_mcp_assertion_verbose`), and writes that key into `.env` as
`STUDYLIFE_API_KEY` - the exact variable `config.Settings` and `server.main()` already
read for stdio mode.

Requires a StudyLife server release with the RFC 8252 loopback exception for
`/connect/mcp` (a loopback redirect_uri is otherwise rejected as untrusted) - see
MIN_STUDYLIFE_RELEASE_FOR_LOOPBACK below.
"""

from __future__ import annotations

import argparse
import hmac
import secrets
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import anyio
from pydantic import HttpUrl, ValidationError

from studylife_mcp.client import AssertionExchangeError, exchange_mcp_assertion_verbose
from studylife_mcp.config import Settings

# Generous default: a passkey login plus any consent-screen friction, but still finite
# so a closed/abandoned browser tab doesn't hang the CLI forever.
CALLBACK_TIMEOUT_SECONDS = 300.0

# TODO(scheduler): fill in the actual StudyLife release/tag once the loopback-exception
# server PR (RFC 8252 - accepting http://127.0.0.1:<port> as a valid /connect/mcp
# redirect_uri) is cut and released. Referenced only in user-facing error messages below.
MIN_STUDYLIFE_RELEASE_FOR_LOOPBACK = "the StudyLife release containing lukislp/studylife#97"

# Same variable config.Settings/server.main() read STUDYLIFE_API_KEY from - see .env.example.
ENV_KEY_NAME = "STUDYLIFE_API_KEY"


@dataclass
class CallbackResult:
    """What the loopback callback received, before any validation - state/assertion
    default to "" (not missing) when StudyLife's redirect omits either, so callers can
    treat "" uniformly as "not provided" without a separate None case."""

    state: str
    assertion: str


def _parse_callback_query(query_string: str) -> CallbackResult:
    """Extracts `state` and `assertion` from the callback's raw query string. Pure
    function (no socket/server involved) so callback parsing is testable on its own."""
    query = parse_qs(query_string)
    return CallbackResult(
        state=query.get("state", [""])[0],
        assertion=query.get("assertion", [""])[0],
    )


def _states_match(received: str, expected: str) -> bool:
    """Constant-time comparison, same reasoning as oauth_provider.py's CSRF token
    check - the state isn't secret, but there's no reason to prefer a
    timing-observable comparison over a safe one that's just as easy to write."""
    return hmac.compare_digest(received, expected)


_CALLBACK_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>studylife-mcp login</title></head>
<body style="font-family: sans-serif; text-align: center; padding-top: 3rem;">
<p>Login complete &mdash; you can close this tab and return to the terminal.</p>
</body>
</html>
"""


class _CallbackState:
    """Shared between the HTTP handler (invoked synchronously inside
    `HTTPServer.handle_request()`) and the caller waiting on it - a single request is
    ever served per `_CallbackHTTPServer` instance, so a plain attribute is enough."""

    def __init__(self) -> None:
        self.result: CallbackResult | None = None


def _make_handler(state: _CallbackState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # Silence the default stderr access log - nothing useful, just noise.

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return

            state.result = _parse_callback_query(parsed.query)

            body = _CALLBACK_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class _CallbackHTTPServer:
    """Localhost-only (127.0.0.1) HTTP listener on an OS-assigned ephemeral port for the
    single `/callback` request StudyLife's `/connect/mcp` redirects the browser to.
    Serves exactly one request: `wait_for_callback()` blocks until it arrives (or the
    given timeout elapses), then the listener is done - each login run creates a fresh
    instance, so there's nothing to clean up between runs.
    """

    def __init__(self) -> None:
        self._state = _CallbackState()
        self._server = HTTPServer(("127.0.0.1", 0), _make_handler(self._state))

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def wait_for_callback(self, timeout_seconds: float) -> CallbackResult | None:
        self._server.timeout = timeout_seconds
        self._server.handle_request()  # returns on request OR timeout, whichever first
        self._server.server_close()
        return self._state.result


def _write_env_var(env_path: Path, key: str, value: str) -> None:
    """Idempotently sets `key=value` in the given .env file: replaces an existing
    `KEY=...` line in place (preserving every other line - comments, blank lines, other
    variables, order), appends a new line if the key isn't present yet, and creates the
    file (and its parent directory) if it doesn't exist at all. Mirrors what a developer
    hand-editing the file would do, so re-running login (e.g. after StudyLife rotates
    the key) never disturbs anything else in it.
    """
    prefix = f"{key}="
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}"
            break
    else:
        lines.append(f"{prefix}{value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_settings(base_url_override: str | None) -> Settings:
    """Resolves STUDYLIFE_BASE_URL from the CLI override if given, otherwise from
    .env/the environment via pydantic-settings' own precedence (config.py) - passing
    only studylife_base_url as an explicit kwarg still lets every other field (e.g.
    studylife_ca_cert_path) resolve from .env normally. Raises pydantic's
    ValidationError if neither source has it set."""
    if base_url_override:
        return Settings(studylife_base_url=HttpUrl(base_url_override))
    return Settings()  # type: ignore[call-arg]


def run_login(
    *,
    base_url_override: str | None = None,
    env_path: Path = Path(".env"),
    timeout_seconds: float = CALLBACK_TIMEOUT_SECONDS,
    open_browser: Callable[[str], bool] = webbrowser.open,
) -> int:
    """Drives one full browser login round trip and writes the resulting key into
    `env_path`. Returns a process exit code (0 on success) instead of calling
    sys.exit() directly, so this can be exercised in tests without a SystemExit."""
    try:
        settings = _load_settings(base_url_override)
    except ValidationError:
        print(
            "STUDYLIFE_BASE_URL isn't set. Pass --base-url, or set STUDYLIFE_BASE_URL in "
            ".env or the environment, then try again.",
            file=sys.stderr,
        )
        return 1

    base_url = str(settings.studylife_base_url).rstrip("/")
    state_token = secrets.token_urlsafe(32)

    callback_server = _CallbackHTTPServer()
    redirect_uri = f"http://127.0.0.1:{callback_server.port}/callback"
    connect_url = (
        f"{base_url}/connect/mcp?{urlencode({'redirect_uri': redirect_uri, 'state': state_token})}"
    )

    print(f"Opening your browser to log in to StudyLife:\n  {connect_url}")
    print("Waiting for you to finish logging in and approving the connection...")
    open_browser(connect_url)

    result = callback_server.wait_for_callback(timeout_seconds)
    if result is None:
        print(
            f"Timed out after {int(timeout_seconds)}s waiting for StudyLife to redirect "
            "back. Either the login/approval wasn't completed in the browser tab, or "
            "this StudyLife instance doesn't yet accept a loopback redirect_uri "
            f"(requires StudyLife {MIN_STUDYLIFE_RELEASE_FOR_LOOPBACK} or newer) - if the "
            "browser tab showed an error from StudyLife itself, that's the likely cause. "
            "You can also set up access manually - see the README.",
            file=sys.stderr,
        )
        return 1

    if not _states_match(result.state, state_token):
        print(
            "Rejected the login callback: its state didn't match what this command sent "
            "(possible cross-request mix-up). Please run this command again.",
            file=sys.stderr,
        )
        return 1

    if not result.assertion:
        print(
            "StudyLife's callback didn't include a login assertion - the connection may "
            "have been denied, or this instance may not fully support the loopback login "
            f"flow yet (requires StudyLife {MIN_STUDYLIFE_RELEASE_FOR_LOOPBACK} or newer). "
            "You can also set up access manually - see the README.",
            file=sys.stderr,
        )
        return 1

    try:
        _user_id, mcp_api_key = anyio.run(
            exchange_mcp_assertion_verbose, settings, result.assertion
        )
    except AssertionExchangeError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    # mcp_api_key deliberately never printed - only its destination is.
    _write_env_var(env_path, ENV_KEY_NAME, mcp_api_key)
    print(f"Login successful - {ENV_KEY_NAME} was written to {env_path}.")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="studylife-mcp-login",
        description=(
            "Browser-based login for stdio mode: opens StudyLife's consent page, "
            "exchanges the resulting assertion for an MCP API key, and writes it into "
            "the .env file stdio mode reads it from."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="StudyLife base URL to log in against. Defaults to STUDYLIFE_BASE_URL "
        "from .env / the environment.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env file to write STUDYLIFE_API_KEY into (default: .env in "
        "the current directory - the same file Settings/main() read it from).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=CALLBACK_TIMEOUT_SECONDS,
        help="Seconds to wait for the browser login to complete (default: 300).",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    exit_code = run_login(
        base_url_override=args.base_url,
        env_path=Path(args.env_file),
        timeout_seconds=args.timeout,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
