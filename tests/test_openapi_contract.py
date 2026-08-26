"""Contract tests: studylife-mcp's hand-mirrored DTOs (models.py) and hardcoded
endpoints (client.py) against StudyLife's committed OpenAPI spec (studylife repo,
docs/api/openapi.json).

Audit finding D2 (consumer side): this server hand-mirrors StudyLife's C# DTOs in
models.py and hand-codes the paths/methods it calls in client.py, instead of
generating either from the spec. These tests don't fix that duplication - they
catch it silently drifting out of sync, by diffing the mirrored surface against
the real spec on every CI run.

Spec source: STUDYLIFE_OPENAPI_SPEC env var, either a local file path or an http(s)
URL, defaulting to the raw GitHub URL of the studylife repo's committed spec. CI is
expected to be able to reach that URL - an unreachable spec there is a real
failure, not a reason to skip. Locally, an unreachable/unset spec just skips these
tests (point STUDYLIFE_OPENAPI_SPEC at a local checkout of studylife's
docs/api/openapi.json to run them offline).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from studylife_mcp.models import Course, CourseGoal, Note, Session

DEFAULT_SPEC_SOURCE = (
    "https://raw.githubusercontent.com/lukislp/studylife/main/docs/api/openapi.json"
)


def _running_in_ci() -> bool:
    # Both are set by GitHub Actions; CI is the more general convention other CI
    # systems also set, GITHUB_ACTIONS narrows it to "really is Actions".
    return os.environ.get("CI", "").lower() == "true" or "GITHUB_ACTIONS" in os.environ


def _load_spec() -> dict[str, Any]:
    source = os.environ.get("STUDYLIFE_OPENAPI_SPEC", DEFAULT_SPEC_SOURCE)
    try:
        if source.startswith("http://") or source.startswith("https://"):
            response = httpx.get(source, timeout=10.0, follow_redirects=True)
            response.raise_for_status()
            spec: dict[str, Any] = response.json()
        else:
            spec = json.loads(Path(source).read_text(encoding="utf-8"))
    except Exception as exc:
        if _running_in_ci():
            pytest.fail(
                f"Could not load the StudyLife OpenAPI spec from {source!r}: {exc}. "
                "The spec must be reachable in CI - this is a contract test, not an "
                "optional one."
            )
        pytest.skip(
            f"StudyLife OpenAPI spec unreachable ({source!r}): {exc}. Skipping contract "
            "tests locally-offline. Set STUDYLIFE_OPENAPI_SPEC to a local "
            "docs/api/openapi.json path to run them offline."
        )
    return spec


@pytest.fixture(scope="session")
def spec() -> dict[str, Any]:
    return _load_spec()


@pytest.fixture(scope="session")
def spec_paths(spec: dict[str, Any]) -> dict[str, Any]:
    paths = spec.get("paths")
    assert isinstance(paths, dict), "spec has no top-level 'paths' object"
    return paths


@pytest.fixture(scope="session")
def spec_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    schemas = spec.get("components", {}).get("schemas")
    assert isinstance(schemas, dict), "spec has no components.schemas object"
    return schemas


# ---------------------------------------------------------------------------
# Endpoint coverage: every path+method client.py hardcodes must exist in the spec.
# ---------------------------------------------------------------------------

# (method, path, caller) - path is the exact template client.py sends; none of
# these StudyLife routes take path parameters, so no {id}-style placeholders are
# needed here.
#
# /api/auth/whoami is deliberately NOT in this list: grepping client.py,
# oauth_provider.py, client_resolver.py and server.py turns up no call to it
# anywhere in this repo (_validate_studylife_key re-proves key ownership via
# list_courses, not a whoami call) - there's nothing to check drift against yet.
# Add it here the moment client.py actually starts calling it.
CLIENT_ENDPOINTS: list[tuple[str, str, str]] = [
    ("get", "/api/courses", "StudyLifeClient.list_courses"),
    ("get", "/api/notes", "StudyLifeClient.list_notes"),
    ("get", "/api/notes/search", "StudyLifeClient.search_notes"),
    ("get", "/api/sessions", "StudyLifeClient.list_sessions"),
    ("get", "/api/coursegoals", "StudyLifeClient.list_course_goals"),
    ("post", "/api/notes", "StudyLifeClient.create_note"),
    ("post", "/api/sessions", "StudyLifeClient.create_session"),
    ("post", "/api/auth/mcp-assertion-exchange", "exchange_mcp_assertion"),
]


@pytest.mark.parametrize(
    "method,path,caller",
    CLIENT_ENDPOINTS,
    ids=[f"{m.upper()}_{p}" for m, p, _ in CLIENT_ENDPOINTS],
)
def test_client_endpoint_exists_in_spec(
    spec_paths: dict[str, Any], method: str, path: str, caller: str
) -> None:
    path_item = spec_paths.get(path)
    assert path_item is not None, (
        f"{caller} calls {method.upper()} {path}, but the spec has no 'paths' entry "
        f"for {path!r} at all. Known spec paths: {sorted(spec_paths)}"
    )
    assert method in path_item, (
        f"{caller} calls {method.upper()} {path}, but the spec's entry for {path!r} "
        f"has no {method!r} operation. Methods it does have: {sorted(path_item)}"
    )


# ---------------------------------------------------------------------------
# Model compatibility: every pydantic model in models.py vs its C#-named
# component schema.
# ---------------------------------------------------------------------------

# Explicit model -> OpenAPI component schema name map. Names come straight from
# each model's own docstring ("Mirrors StudyLife.Shared.XyzDto") - if the C# DTO
# gets renamed, or Swashbuckle's SchemaId strategy changes, update this map (the
# assertion messages below list every available schema name to make that easy).
MODEL_SCHEMA_MAP: dict[type[BaseModel], str] = {
    Course: "CourseDto",
    Note: "NoteDto",
    Session: "StudySessionDto",
    CourseGoal: "CourseGoalDto",
}

# Component-schema properties known to exist without a mirrored model field, and
# accepted as intentional (e.g. server-only/navigation properties). Empty for now -
# add an entry here (model -> {"propName", ...}) if a first real run reports a
# property that's a deliberate omission rather than drift to fix.
ALLOWED_UNMIRRORED_PROPERTIES: dict[type[BaseModel], set[str]] = {}


def _schema_for(spec_schemas: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    schema_name = MODEL_SCHEMA_MAP[model]
    schema: dict[str, Any] | None = spec_schemas.get(schema_name)
    assert schema is not None, (
        f"{model.__name__} is mapped to component schema {schema_name!r} "
        f"(see its docstring in models.py), but the spec has no such component. "
        "Either the C# DTO was renamed/removed (update MODEL_SCHEMA_MAP in this test "
        f"to match), or this is real drift. Available schemas: {sorted(spec_schemas)}"
    )
    return schema


def _schema_type_list(prop: dict[str, Any]) -> list[str]:
    prop_type = prop.get("type")
    if isinstance(prop_type, list):
        return prop_type
    if isinstance(prop_type, str):
        return [prop_type]
    return []


def _is_nullable(prop: dict[str, Any]) -> bool:
    # OpenAPI 3.0 style (nullable: true) and OpenAPI 3.1 style (type: [x, "null"])
    # both show up depending on the generator/version - check both.
    return prop.get("nullable") is True or "null" in _schema_type_list(prop)


@pytest.mark.parametrize("model", list(MODEL_SCHEMA_MAP), ids=lambda m: m.__name__)
def test_model_fields_exist_in_schema(spec_schemas: dict[str, Any], model: type[BaseModel]) -> None:
    """Every field mirrored in the pydantic model - required or optional - must
    exist as a property on the spec schema. If StudyLife drops or renames a field,
    a tool reading it should fail loudly here rather than silently getting `None`
    (or a validation error deep in a live call) at runtime."""
    schema = _schema_for(spec_schemas, model)
    properties = schema.get("properties", {})
    schema_name = MODEL_SCHEMA_MAP[model]
    for field_name, field_info in model.model_fields.items():
        json_name = field_info.alias or field_name
        assert json_name in properties, (
            f"{model.__name__}.{field_name} (JSON {json_name!r}) has no matching "
            f"property on spec schema {schema_name!r}. Spec properties: "
            f"{sorted(properties)}"
        )


@pytest.mark.parametrize("model", list(MODEL_SCHEMA_MAP), ids=lambda m: m.__name__)
def test_model_required_fields_match_schema(
    spec_schemas: dict[str, Any], model: type[BaseModel]
) -> None:
    """Pydantic fields with no default are treated as always-present by every tool
    that reads them (`Course.model_validate(...)` raises if they're missing). If the
    spec marks the matching property optional or nullable, that's real drift the
    model is silently over-promising on (audit D2 flagged course_color/timer_mode_id
    on Session as exactly this) - fail here instead of papering over it."""
    schema = _schema_for(spec_schemas, model)
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    schema_name = MODEL_SCHEMA_MAP[model]

    for field_name, field_info in model.model_fields.items():
        if not field_info.is_required():
            continue  # model itself treats this as optional - nothing to compare
        json_name = field_info.alias or field_name
        prop = properties.get(json_name)
        if prop is None:
            continue  # already reported by test_model_fields_exist_in_schema

        nullable = _is_nullable(prop)
        assert json_name in required and not nullable, (
            f"{model.__name__}.{field_name} is REQUIRED in the pydantic model (no "
            f"default), but spec schema {schema_name!r}'s property {json_name!r} is "
            f"{'nullable' if nullable else 'not in the schema required list'} "
            f"(schema required: {sorted(required)}). This is real drift: "
            f"{model.__name__}.model_validate(...) will raise on any real StudyLife "
            f"response that omits or nulls {json_name!r}, even though the API allows "
            f"it. Fix by either loosening {model.__name__}.{field_name} to Optional "
            "to match reality, or tightening the field on the StudyLife side if it "
            "should truly always be present."
        )


@pytest.mark.parametrize("model", list(MODEL_SCHEMA_MAP), ids=lambda m: m.__name__)
def test_schema_properties_all_mirrored(
    spec_schemas: dict[str, Any], model: type[BaseModel]
) -> None:
    """The inverse direction: every property the spec declares should have a
    matching pydantic field. An unmirrored property is either a field StudyLife
    added that this client silently drops on every response, or a naming mismatch
    this test's alias handling hasn't caught."""
    schema = _schema_for(spec_schemas, model)
    properties = schema.get("properties", {})
    schema_name = MODEL_SCHEMA_MAP[model]
    mirrored = {info.alias or name for name, info in model.model_fields.items()}
    allowed = ALLOWED_UNMIRRORED_PROPERTIES.get(model, set())
    missing = set(properties) - mirrored - allowed
    assert not missing, (
        f"Spec schema {schema_name!r} declares "
        f"propert{'y' if len(missing) == 1 else 'ies'} {sorted(missing)} that "
        f"{model.__name__} has no matching field for. If StudyLife added these, "
        "mirror them in models.py; if they're intentionally unused, add them to "
        "ALLOWED_UNMIRRORED_PROPERTIES in this test instead of leaving this failing."
    )
