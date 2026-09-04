"""Wire-format regression tests for the StudyLife DTO models."""

import re
from datetime import UTC, datetime

from studylife_mcp.models import Note, Session, configure_server_timezone

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def _session(**overrides) -> Session:
    payload = {
        "id": 1,
        "courseId": 31,
        "courseName": "Objektorientierte Programmierung",
        "courseColor": "#123456",
        "startTime": "2026-09-04T18:00:00",
        "endTime": "2026-09-04T19:30:00",
        "isCompleted": False,
        "timerModeId": 1,
    }
    payload.update(overrides)
    return Session.model_validate(payload)


def test_naive_server_timestamps_become_rfc3339_with_the_server_zone():
    # The server sends local wall-clock times without an offset; the tool output schema
    # declares date-time (RFC 3339), which requires one - clients reject the bare form.
    session = _session()
    dumped = session.model_dump(mode="json", by_alias=True)
    assert dumped["startTime"] == "2026-09-04T18:00:00+02:00"  # CEST in September
    assert RFC3339.match(dumped["startTime"]) and RFC3339.match(dumped["endTime"])
    assert session.start_time.tzinfo is not None
    # Winter time gets the standard offset, not a hard-coded +02:00.
    winter = _session(startTime="2026-01-15T08:00:00", endTime="2026-01-15T09:00:00")
    assert winter.model_dump(mode="json", by_alias=True)["startTime"] == "2026-01-15T08:00:00+01:00"


def test_aware_timestamps_are_left_untouched():
    session = _session(startTime="2026-09-04T16:00:00Z", endTime="2026-09-04T17:00:00+00:00")
    assert session.start_time == datetime(2026, 9, 4, 16, 0, tzinfo=UTC)
    assert session.model_dump(mode="json", by_alias=True)["startTime"] == "2026-09-04T16:00:00Z"


def test_notes_get_the_same_treatment_and_the_zone_is_configurable():
    configure_server_timezone("UTC")
    try:
        note = Note.model_validate(
            {
                "id": 7,
                "title": "t",
                "content": "c",
                "createdAt": "2026-09-04T18:00:00",
                "updatedAt": "2026-09-04T18:05:00",
            }
        )
        dumped = note.model_dump(mode="json", by_alias=True)
        assert dumped["createdAt"] == "2026-09-04T18:00:00Z"
        assert dumped["updatedAt"] == "2026-09-04T18:05:00Z"
    finally:
        configure_server_timezone("Europe/Berlin")
