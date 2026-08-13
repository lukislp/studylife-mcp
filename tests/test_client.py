import httpx
import pytest
import respx

from studylife_mcp.client import StudyLifeClient
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
