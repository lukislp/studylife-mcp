"""Atheris fuzz harness for the StudyLife DTO models and the rate-limit bucket keys.

Contract under test: the DTO models either validate a JSON body (attaching the server zone
to naive timestamps) or raise pydantic's ValidationError - never anything else; the
rate-limit key functions never raise for any client address or header bytes and always
yield a non-empty key, so a malformed request can never bypass the buckets.

Run locally (Linux, needs the atheris wheel):
    uv sync --frozen --group fuzz
    uv run python fuzz/fuzz_models_and_keys.py -max_total_time=60
CI runs the same harness for a short, fixed time budget (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import contextlib
import sys

import atheris
from pydantic import ValidationError
from starlette.requests import Request

from studylife_mcp.models import Course, CourseGoal, Note, Session
from studylife_mcp.rate_limit import bearer_token_key, client_ip_key

MODELS = (Course, CourseGoal, Note, Session)


def _request(fdp: atheris.FuzzedDataProvider) -> Request:
    headers = []
    if fdp.ConsumeBool():
        headers.append((b"x-forwarded-for", fdp.ConsumeBytes(64)))
    if fdp.ConsumeBool():
        headers.append((b"authorization", fdp.ConsumeBytes(96)))
    client = None
    if fdp.ConsumeBool():
        client = (fdp.ConsumeUnicodeNoSurrogates(40), fdp.ConsumeIntInRange(0, 65535))
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers, "client": client}
    return Request(scope)


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    request = _request(fdp)
    if not client_ip_key(request) or not bearer_token_key(request):
        raise AssertionError("rate-limit key must never be empty")
    body = fdp.ConsumeBytes(512)
    for model in MODELS:
        with contextlib.suppress(ValidationError):
            model.model_validate_json(body)


if __name__ == "__main__":
    # instrument_all() instead of instrument_imports(): the package is loaded through uv's
    # editable-install loader, which the import hook does not see (no coverage feedback,
    # so libFuzzer would never grow its inputs past a few bytes).
    atheris.instrument_all()
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
