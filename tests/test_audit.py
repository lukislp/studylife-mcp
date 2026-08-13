import logging

import pytest

from studylife_mcp.audit import audited


async def test_audited_logs_success_and_returns_result(caplog: pytest.LogCaptureFixture) -> None:
    @audited("some_tool")
    async def fn(x: int) -> int:
        return x * 2

    with caplog.at_level(logging.INFO, logger="studylife_mcp.audit"):
        result = await fn(21)

    assert result == 42
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "tool=some_tool" in message
    assert "result=ok" in message
    assert "args_digest=" in message
    assert "duration_ms=" in message


async def test_audited_logs_error_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    @audited("failing_tool")
    async def fn() -> None:
        raise ValueError("boom")

    with (
        caplog.at_level(logging.INFO, logger="studylife_mcp.audit"),
        pytest.raises(ValueError, match="boom"),
    ):
        await fn()

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "tool=failing_tool" in message
    assert "result=error" in message


async def test_audited_digest_excludes_raw_argument_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @audited("create_note")
    async def fn(title: str, content: str) -> None:
        return None

    with caplog.at_level(logging.INFO, logger="studylife_mcp.audit"):
        await fn(title="secret title", content="secret content")

    message = caplog.records[0].getMessage()
    assert "secret title" not in message
    assert "secret content" not in message
