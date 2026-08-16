import re
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import truststore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from studylife_mcp.client import StudyLifeClient, _build_ssl_context
from studylife_mcp.config import Settings

COURSE_PAYLOAD = [
    {
        "id": 1,
        "semester": 1,
        "name": "Machine Learning",
        "code": "ML101",
        "color": "#6C5CE7",
        "icon": "📚",
        "topics": ["Regression", "Classification"],
        "ects": 5,
        "group": None,
    }
]

NOTE_PAYLOAD = [
    {
        "id": 1,
        "title": "Lecture recap",
        "content": "Covered gradient descent and regularization.",
        "createdAt": "2026-08-01T10:00:00",
        "updatedAt": "2026-08-02T11:30:00",
        "courseId": 1,
        "sessionId": None,
        "isMarkdown": False,
    }
]

SESSION_PAYLOAD = [
    {
        "id": 1,
        "courseId": 1,
        "courseName": "Machine Learning",
        "courseColor": "#6C5CE7",
        "startTime": "2026-08-01T10:00:00",
        "endTime": "2026-08-01T11:30:00",
        "topic": "Regression",
        "notes": "Went well",
        "isCompleted": True,
        "timerModeId": 1,
        "recurrenceGroupId": None,
    }
]

COURSE_GOAL_PAYLOAD = [
    {
        "courseId": 1,
        "courseName": "Machine Learning",
        "targetDate": "2026-09-01T00:00:00",
        "completionNote": "Finished early",
        "completedAt": "2026-08-30T00:00:00",
        "grade": 1.3,
        "completedTopics": "Regression,Classification",
        "tag": "core",
    }
]


@respx.mock
async def test_list_courses_happy_path(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(
        return_value=httpx.Response(200, json=COURSE_PAYLOAD)
    )
    client = StudyLifeClient(settings)

    courses = await client.list_courses()

    assert len(courses) == 1
    assert courses[0].name == "Machine Learning"
    assert courses[0].topics == ["Regression", "Classification"]
    await client.aclose()


@respx.mock
async def test_list_courses_unauthorized_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(return_value=httpx.Response(401))
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.HTTPStatusError):
        await client.list_courses()
    await client.aclose()


@respx.mock
async def test_list_courses_timeout_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/courses").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.TimeoutException):
        await client.list_courses()
    await client.aclose()


@respx.mock
async def test_list_notes_happy_path(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/notes").mock(
        return_value=httpx.Response(200, json=NOTE_PAYLOAD)
    )
    client = StudyLifeClient(settings)

    notes = await client.list_notes()

    assert len(notes) == 1
    assert notes[0].title == "Lecture recap"
    assert notes[0].course_id == 1
    assert notes[0].session_id is None
    await client.aclose()


@respx.mock
async def test_list_notes_unauthorized_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/notes").mock(return_value=httpx.Response(401))
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.HTTPStatusError):
        await client.list_notes()
    await client.aclose()


@respx.mock
async def test_list_notes_timeout_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/notes").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.TimeoutException):
        await client.list_notes()
    await client.aclose()


@respx.mock
async def test_search_notes_happy_path(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/notes/search", params={"q": "gradient"}).mock(
        return_value=httpx.Response(200, json=NOTE_PAYLOAD)
    )
    client = StudyLifeClient(settings)

    notes = await client.search_notes("gradient")

    assert len(notes) == 1
    assert notes[0].title == "Lecture recap"
    await client.aclose()


@respx.mock
async def test_search_notes_unauthorized_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/notes/search", params={"q": "gradient"}).mock(
        return_value=httpx.Response(401)
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.HTTPStatusError):
        await client.search_notes("gradient")
    await client.aclose()


@respx.mock
async def test_search_notes_timeout_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/notes/search", params={"q": "gradient"}).mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.TimeoutException):
        await client.search_notes("gradient")
    await client.aclose()


@respx.mock
async def test_list_sessions_happy_path(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/sessions").mock(
        return_value=httpx.Response(200, json=SESSION_PAYLOAD)
    )
    client = StudyLifeClient(settings)

    sessions = await client.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].course_name == "Machine Learning"
    assert sessions[0].is_completed is True
    assert sessions[0].recurrence_group_id is None
    await client.aclose()


@respx.mock
async def test_list_sessions_unauthorized_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/sessions").mock(return_value=httpx.Response(401))
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.HTTPStatusError):
        await client.list_sessions()
    await client.aclose()


@respx.mock
async def test_list_sessions_timeout_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/sessions").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.TimeoutException):
        await client.list_sessions()
    await client.aclose()


@respx.mock
async def test_list_course_goals_happy_path(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/coursegoals").mock(
        return_value=httpx.Response(200, json=COURSE_GOAL_PAYLOAD)
    )
    client = StudyLifeClient(settings)

    goals = await client.list_course_goals()

    assert len(goals) == 1
    assert goals[0].grade == 1.3
    assert goals[0].completed_topics == "Regression,Classification"
    assert goals[0].tag == "core"
    await client.aclose()


@respx.mock
async def test_list_course_goals_unauthorized_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/coursegoals").mock(
        return_value=httpx.Response(401)
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.HTTPStatusError):
        await client.list_course_goals()
    await client.aclose()


@respx.mock
async def test_list_course_goals_timeout_raises(settings: Settings) -> None:
    respx.get("https://studylife.example.test/api/coursegoals").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.TimeoutException):
        await client.list_course_goals()
    await client.aclose()


@respx.mock
async def test_create_note_happy_path(settings: Settings) -> None:
    created = {**NOTE_PAYLOAD[0], "id": 2, "title": "New note"}
    route = respx.post("https://studylife.example.test/api/notes").mock(
        return_value=httpx.Response(200, json=created)
    )
    client = StudyLifeClient(settings)

    note = await client.create_note("New note", "Some content", course_id=1)

    assert note.id == 2
    assert note.title == "New note"
    sent_body = route.calls.last.request.content
    assert b'"title":"New note"' in sent_body
    assert b'"courseId":1' in sent_body
    assert b'"isMarkdown":false' in sent_body
    await client.aclose()


@respx.mock
async def test_create_note_markdown(settings: Settings) -> None:
    created = {**NOTE_PAYLOAD[0], "id": 3, "title": "Formatted note", "isMarkdown": True}
    route = respx.post("https://studylife.example.test/api/notes").mock(
        return_value=httpx.Response(200, json=created)
    )
    client = StudyLifeClient(settings)

    note = await client.create_note("Formatted note", "# Heading", is_markdown=True)

    assert note.is_markdown is True
    sent_body = route.calls.last.request.content
    assert b'"isMarkdown":true' in sent_body
    await client.aclose()


@respx.mock
async def test_create_note_bad_request_includes_body(settings: Settings) -> None:
    respx.post("https://studylife.example.test/api/notes").mock(
        return_value=httpx.Response(400, text="Title must not be empty.")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.HTTPStatusError, match=re.escape("Title must not be empty.")):
        await client.create_note("", "content")
    await client.aclose()


@respx.mock
async def test_create_note_timeout_raises(settings: Settings) -> None:
    respx.post("https://studylife.example.test/api/notes").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.TimeoutException):
        await client.create_note("title", "content")
    await client.aclose()


@respx.mock
async def test_create_session_happy_path(settings: Settings) -> None:
    created = {**SESSION_PAYLOAD[0], "id": 2}
    route = respx.post("https://studylife.example.test/api/sessions").mock(
        return_value=httpx.Response(200, json=created)
    )
    client = StudyLifeClient(settings)

    session = await client.create_session(
        course_id=1,
        course_name="Machine Learning",
        course_color="#6C5CE7",
        start_time=datetime(2026, 8, 1, 10, 0),
        end_time=datetime(2026, 8, 1, 11, 30),
    )

    assert session.id == 2
    assert session.course_name == "Machine Learning"
    sent_body = route.calls.last.request.content
    assert b'"courseId":1' in sent_body
    assert b'"timerModeId":1' in sent_body
    await client.aclose()


@respx.mock
async def test_create_session_bad_request_includes_body(settings: Settings) -> None:
    respx.post("https://studylife.example.test/api/sessions").mock(
        return_value=httpx.Response(400, text="EndTime must be after StartTime.")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.HTTPStatusError, match=re.escape("EndTime must be after StartTime.")):
        await client.create_session(
            course_id=1,
            course_name="Machine Learning",
            course_color="#6C5CE7",
            start_time=datetime(2026, 8, 1, 11, 30),
            end_time=datetime(2026, 8, 1, 10, 0),
        )
    await client.aclose()


@respx.mock
async def test_create_session_timeout_raises(settings: Settings) -> None:
    respx.post("https://studylife.example.test/api/sessions").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    client = StudyLifeClient(settings)

    with pytest.raises(httpx.TimeoutException):
        await client.create_session(
            course_id=1,
            course_name="Machine Learning",
            course_color="#6C5CE7",
            start_time=datetime(2026, 8, 1, 10, 0),
            end_time=datetime(2026, 8, 1, 11, 30),
        )
    await client.aclose()


def test_build_ssl_context_defaults_to_os_trust_store() -> None:
    context = _build_ssl_context(None)

    assert isinstance(context, truststore.SSLContext)


def test_build_ssl_context_loads_custom_ca_when_path_given(tmp_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    context = _build_ssl_context(str(ca_path))

    assert isinstance(context, ssl.SSLContext)
    assert not isinstance(context, truststore.SSLContext)
