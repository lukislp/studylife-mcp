import pytest
from prometheus_client.parser import text_string_to_metric_families

from studylife_mcp.audit import audited
from studylife_mcp.metrics import RATE_LIMIT_REJECTIONS_TOTAL, render_latest


def _sample_value(metric_name: str, labels: dict[str, str]) -> float | None:
    body, _content_type = render_latest()
    for family in text_string_to_metric_families(body.decode()):
        for sample in family.samples:
            if sample.name == metric_name and sample.labels == labels:
                return sample.value
    return None


async def test_audited_records_ok_call_count_and_duration() -> None:
    @audited("metrics_test_ok_tool")
    async def tool() -> str:
        return "done"

    before = _sample_value(
        "studylife_mcp_tool_calls_total", {"tool": "metrics_test_ok_tool", "status": "ok"}
    )
    await tool()
    after = _sample_value(
        "studylife_mcp_tool_calls_total", {"tool": "metrics_test_ok_tool", "status": "ok"}
    )

    assert (before or 0) + 1 == after

    duration_count = _sample_value(
        "studylife_mcp_tool_call_duration_seconds_count", {"tool": "metrics_test_ok_tool"}
    )
    assert duration_count is not None and duration_count >= 1


async def test_audited_records_error_call_count_and_reraises() -> None:
    @audited("metrics_test_error_tool")
    async def tool() -> None:
        raise ValueError("boom")

    before = _sample_value(
        "studylife_mcp_tool_calls_total", {"tool": "metrics_test_error_tool", "status": "error"}
    )
    with pytest.raises(ValueError, match="boom"):
        await tool()
    after = _sample_value(
        "studylife_mcp_tool_calls_total", {"tool": "metrics_test_error_tool", "status": "error"}
    )

    assert (before or 0) + 1 == after


def test_rate_limit_rejections_metric_is_incrementable() -> None:
    before = _sample_value(
        "studylife_mcp_rate_limit_rejections_total", {"path": "/metrics-test-path"}
    )
    RATE_LIMIT_REJECTIONS_TOTAL.labels(path="/metrics-test-path").inc()
    after = _sample_value(
        "studylife_mcp_rate_limit_rejections_total", {"path": "/metrics-test-path"}
    )

    assert (before or 0) + 1 == after


def test_render_latest_returns_prometheus_text_format() -> None:
    body, content_type = render_latest()

    assert b"studylife_mcp_tool_calls_total" in body
    assert "text/plain" in content_type
