import threading
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import respx

from studylife_mcp.login import (
    CallbackResult,
    _parse_callback_query,
    _states_match,
    _write_env_var,
    run_login,
)

EXCHANGE_URL = "https://studylife.example.test/api/auth/mcp-assertion-exchange"


# --- _parse_callback_query -------------------------------------------------


def test_parse_callback_query_extracts_state_and_assertion() -> None:
    result = _parse_callback_query("state=abc123&assertion=xyz789")

    assert result == CallbackResult(state="abc123", assertion="xyz789")


def test_parse_callback_query_missing_params_defaults_to_empty_string() -> None:
    result = _parse_callback_query("")

    assert result == CallbackResult(state="", assertion="")


def test_parse_callback_query_missing_assertion_only() -> None:
    result = _parse_callback_query("state=abc123")

    assert result == CallbackResult(state="abc123", assertion="")


# --- _states_match -----------------------------------------------------------


def test_states_match_identical_values() -> None:
    assert _states_match("same-token", "same-token") is True


def test_states_match_rejects_mismatch() -> None:
    assert _states_match("wrong-token", "expected-token") is False


def test_states_match_rejects_empty_against_nonempty() -> None:
    assert _states_match("", "expected-token") is False


# --- _write_env_var -----------------------------------------------------------


def test_write_env_var_creates_file_when_absent(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    _write_env_var(env_path, "STUDYLIFE_API_KEY", "brand-new-key")

    assert env_path.read_text(encoding="utf-8") == "STUDYLIFE_API_KEY=brand-new-key\n"


def test_write_env_var_appends_when_key_missing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("STUDYLIFE_BASE_URL=https://studylife.example.test/\n", encoding="utf-8")

    _write_env_var(env_path, "STUDYLIFE_API_KEY", "new-key")

    content = env_path.read_text(encoding="utf-8")
    assert "STUDYLIFE_BASE_URL=https://studylife.example.test/" in content
    assert "STUDYLIFE_API_KEY=new-key" in content


def test_write_env_var_replaces_existing_value_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "STUDYLIFE_BASE_URL=https://studylife.example.test/\n"
        "STUDYLIFE_API_KEY=old-key\n"
        "MCP_HTTP_PORT=8000\n",
        encoding="utf-8",
    )

    _write_env_var(env_path, "STUDYLIFE_API_KEY", "rotated-key")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "STUDYLIFE_BASE_URL=https://studylife.example.test/",
        "STUDYLIFE_API_KEY=rotated-key",
        "MCP_HTTP_PORT=8000",
    ]


def test_write_env_var_preserves_comments_and_blank_lines(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# a comment about the key\nSTUDYLIFE_API_KEY=old-key\n\n# trailing comment\n",
        encoding="utf-8",
    )

    _write_env_var(env_path, "STUDYLIFE_API_KEY", "rotated-key")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "# a comment about the key",
        "STUDYLIFE_API_KEY=rotated-key",
        "",
        "# trailing comment",
    ]


def test_write_env_var_is_idempotent_across_repeated_runs(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    _write_env_var(env_path, "STUDYLIFE_API_KEY", "first-key")
    _write_env_var(env_path, "STUDYLIFE_API_KEY", "second-key")
    _write_env_var(env_path, "STUDYLIFE_API_KEY", "third-key")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["STUDYLIFE_API_KEY=third-key"]


def test_write_env_var_creates_parent_directory(tmp_path: Path) -> None:
    env_path = tmp_path / "nested" / ".env"

    _write_env_var(env_path, "STUDYLIFE_API_KEY", "a-key")

    assert env_path.read_text(encoding="utf-8") == "STUDYLIFE_API_KEY=a-key\n"


# --- run_login (end-to-end against the real loopback listener) ---------------
#
# open_browser is stubbed to simulate what a real browser would do on StudyLife's
# /connect/mcp redirect: it fires a GET at the redirect_uri the code under test
# generated, from a background thread so it doesn't block before run_login gets a
# chance to start listening. No real browser opens and no external network is
# touched - only 127.0.0.1, which is the loopback listener under test.


def _browser_that_calls_back(
    *, assertion: str | None = "server-issued-assertion", state_override: str | None = None
) -> tuple[list[str], object]:
    captured_urls: list[str] = []

    def _open(url: str) -> bool:
        captured_urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        redirect_uri = query["redirect_uri"][0]
        state = state_override if state_override is not None else query["state"][0]

        params = {"state": state}
        if assertion is not None:
            params["assertion"] = assertion
        callback_url = f"{redirect_uri}?{urlencode(params)}"

        def _hit_callback() -> None:
            httpx.get(callback_url, timeout=5)

        threading.Thread(target=_hit_callback, daemon=True).start()
        return True

    return captured_urls, _open


@respx.mock
def test_run_login_happy_path_writes_key_and_reports_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.route(host="127.0.0.1").pass_through()
    respx.post(EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"userId": 42, "mcpApiKey": "rotated-key"})
    )
    env_path = tmp_path / ".env"
    captured_urls, fake_open = _browser_that_calls_back()

    exit_code = run_login(
        base_url_override="https://studylife.example.test",
        env_path=env_path,
        timeout_seconds=10,
        open_browser=fake_open,  # type: ignore[arg-type]
    )

    assert exit_code == 0
    assert env_path.read_text(encoding="utf-8") == "STUDYLIFE_API_KEY=rotated-key\n"
    out = capsys.readouterr().out
    assert str(env_path) in out
    assert "rotated-key" not in out  # the key itself must never be printed
    assert "/connect/mcp" in captured_urls[0]
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A" in captured_urls[0]


@respx.mock
def test_run_login_preserves_other_env_lines(
    tmp_path: Path,
) -> None:
    respx.route(host="127.0.0.1").pass_through()
    respx.post(EXCHANGE_URL).mock(
        return_value=httpx.Response(200, json={"userId": 42, "mcpApiKey": "rotated-key"})
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "STUDYLIFE_BASE_URL=https://studylife.example.test/\nSTUDYLIFE_API_KEY=old-key\n",
        encoding="utf-8",
    )
    _captured, fake_open = _browser_that_calls_back()

    exit_code = run_login(
        base_url_override="https://studylife.example.test",
        env_path=env_path,
        timeout_seconds=10,
        open_browser=fake_open,  # type: ignore[arg-type]
    )

    assert exit_code == 0
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "STUDYLIFE_BASE_URL=https://studylife.example.test/",
        "STUDYLIFE_API_KEY=rotated-key",
    ]


def test_run_login_state_mismatch_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _captured, fake_open = _browser_that_calls_back(state_override="attacker-supplied-state")

    exit_code = run_login(
        base_url_override="https://studylife.example.test",
        env_path=tmp_path / ".env",
        timeout_seconds=10,
        open_browser=fake_open,  # type: ignore[arg-type]
    )

    assert exit_code == 1
    assert not (tmp_path / ".env").exists()
    assert "state" in capsys.readouterr().err.lower()


def test_run_login_missing_assertion_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _captured, fake_open = _browser_that_calls_back(assertion=None)

    exit_code = run_login(
        base_url_override="https://studylife.example.test",
        env_path=tmp_path / ".env",
        timeout_seconds=10,
        open_browser=fake_open,  # type: ignore[arg-type]
    )

    assert exit_code == 1
    assert not (tmp_path / ".env").exists()
    err = capsys.readouterr().err
    assert "assertion" in err.lower()
    assert "<StudyLife release TBD" in err


def test_run_login_timeout_when_browser_never_calls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _open_without_ever_calling_back(url: str) -> bool:
        return True

    exit_code = run_login(
        base_url_override="https://studylife.example.test",
        env_path=tmp_path / ".env",
        timeout_seconds=0.2,
        open_browser=_open_without_ever_calling_back,
    )

    assert exit_code == 1
    assert not (tmp_path / ".env").exists()
    assert "timed out" in capsys.readouterr().err.lower()


@respx.mock
def test_run_login_exchange_rejected_shows_server_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.route(host="127.0.0.1").pass_through()
    respx.post(EXCHANGE_URL).mock(return_value=httpx.Response(401, text="Assertion has expired."))
    _captured, fake_open = _browser_that_calls_back()

    exit_code = run_login(
        base_url_override="https://studylife.example.test",
        env_path=tmp_path / ".env",
        timeout_seconds=10,
        open_browser=fake_open,  # type: ignore[arg-type]
    )

    assert exit_code == 1
    assert not (tmp_path / ".env").exists()
    err = capsys.readouterr().err
    assert "401" in err
    assert "Assertion has expired." in err


@respx.mock
def test_run_login_exchange_network_error_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.route(host="127.0.0.1").pass_through()
    respx.post(EXCHANGE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    _captured, fake_open = _browser_that_calls_back()

    exit_code = run_login(
        base_url_override="https://studylife.example.test",
        env_path=tmp_path / ".env",
        timeout_seconds=10,
        open_browser=fake_open,  # type: ignore[arg-type]
    )

    assert exit_code == 1
    assert not (tmp_path / ".env").exists()
    assert "Could not reach StudyLife" in capsys.readouterr().err


def test_run_login_missing_base_url_reports_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("STUDYLIFE_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env in this empty directory either

    exit_code = run_login(env_path=tmp_path / ".env")

    assert exit_code == 1
    assert not (tmp_path / ".env").exists()
    assert "STUDYLIFE_BASE_URL" in capsys.readouterr().err
