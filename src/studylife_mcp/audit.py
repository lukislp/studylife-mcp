import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from studylife_mcp.metrics import TOOL_CALL_DURATION_SECONDS, TOOL_CALLS_TOTAL

logger = logging.getLogger("studylife_mcp.audit")

P = ParamSpec("P")
T = TypeVar("T")


def audited(tool_name: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Wraps an MCP tool with a structured audit log entry: tool name, a digest
    of its arguments (not the raw values - arguments may contain user free
    text), outcome, and duration. Logs via the standard `logging` module,
    never to stdout - stdout carries the stdio JSON-RPC transport and must
    stay uncontaminated. Also records the same outcome/duration as Prometheus
    metrics (metrics.py) - one measurement, two destinations, rather than a
    second wrapper duplicating the timing logic."""

    def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            digest_input = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
            args_digest = hashlib.sha256(digest_input.encode()).hexdigest()[:16]
            start = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception:
                duration = time.monotonic() - start
                logger.info(
                    "tool=%s args_digest=%s result=error duration_ms=%s",
                    tool_name,
                    args_digest,
                    round(duration * 1000, 1),
                )
                TOOL_CALLS_TOTAL.labels(tool=tool_name, status="error").inc()
                TOOL_CALL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
                raise
            duration = time.monotonic() - start
            logger.info(
                "tool=%s args_digest=%s result=ok duration_ms=%s",
                tool_name,
                args_digest,
                round(duration * 1000, 1),
            )
            TOOL_CALLS_TOTAL.labels(tool=tool_name, status="ok").inc()
            TOOL_CALL_DURATION_SECONDS.labels(tool=tool_name).observe(duration)
            return result

        return wrapper

    return decorator
