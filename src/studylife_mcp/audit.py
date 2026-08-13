import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

logger = logging.getLogger("studylife_mcp.audit")

P = ParamSpec("P")
T = TypeVar("T")


def audited(tool_name: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Wraps an MCP tool with a structured audit log entry: tool name, a digest
    of its arguments (not the raw values - arguments may contain user free
    text), outcome, and duration. Logs via the standard `logging` module,
    never to stdout - stdout carries the stdio JSON-RPC transport and must
    stay uncontaminated."""

    def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            digest_input = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
            args_digest = hashlib.sha256(digest_input.encode()).hexdigest()[:16]
            start = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                logger.info(
                    "tool=%s args_digest=%s result=error duration_ms=%s",
                    tool_name,
                    args_digest,
                    duration_ms,
                )
                raise
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            logger.info(
                "tool=%s args_digest=%s result=ok duration_ms=%s",
                tool_name,
                args_digest,
                duration_ms,
            )
            return result

        return wrapper

    return decorator
