import ssl
from datetime import datetime
from typing import Any

import httpx
import truststore

from studylife_mcp.config import Settings
from studylife_mcp.models import Course, CourseGoal, Note, Session


def _build_ssl_context(ca_cert_path: str | None) -> ssl.SSLContext:
    if ca_cert_path is not None:
        # A private CA (e.g. a cluster-internal cert-manager issuer) isn't in any OS trust
        # store - load it explicitly instead. Scoped to just this one CA rather than added
        # on top of the OS store: this client only ever talks to one StudyLife instance, so
        # narrower trust here is strictly more correct, not just sufficient.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=ca_cert_path)
        return context
    # Verify against the OS certificate store instead of only certifi's bundle, so a
    # locally trusted cert (e.g. the ASP.NET Core HTTPS dev cert registered via
    # `dotnet dev-certs https --trust`) is accepted for local development.
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class StudyLifeClient:
    """Typed async client for the StudyLife REST API (X-Api-Key auth)."""

    def __init__(self, settings: Settings) -> None:
        self._client = httpx.AsyncClient(
            base_url=str(settings.studylife_base_url),
            headers={"X-Api-Key": settings.studylife_api_key},
            timeout=10.0,
            verify=_build_ssl_context(settings.studylife_ca_cert_path),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.request(method, path, **kwargs)
        if response.is_error:
            # httpx's own HTTPStatusError message omits the response body, which is
            # exactly where StudyLife puts its validation messages (e.g.
            # "EndTime must be after StartTime.") - include it so callers (and the
            # LLM acting on a failed write) see the actual reason, not just "400".
            raise httpx.HTTPStatusError(
                f"{response.status_code} {response.reason_phrase} for {method} {path}: "
                f"{response.text}",
                request=response.request,
                response=response,
            )
        return response

    async def list_courses(self) -> list[Course]:
        response = await self._request("GET", "/api/courses")
        return [Course.model_validate(item) for item in response.json()]

    async def list_notes(self) -> list[Note]:
        response = await self._request("GET", "/api/notes")
        return [Note.model_validate(item) for item in response.json()]

    async def search_notes(self, query: str) -> list[Note]:
        response = await self._request("GET", "/api/notes/search", params={"q": query})
        return [Note.model_validate(item) for item in response.json()]

    async def list_sessions(self) -> list[Session]:
        response = await self._request("GET", "/api/sessions")
        return [Session.model_validate(item) for item in response.json()]

    async def list_course_goals(self) -> list[CourseGoal]:
        response = await self._request("GET", "/api/coursegoals")
        return [CourseGoal.model_validate(item) for item in response.json()]

    async def create_note(
        self,
        title: str,
        content: str,
        course_id: int | None = None,
        session_id: int | None = None,
        is_markdown: bool = False,
    ) -> Note:
        # CreatedAt/UpdatedAt/Id are set server-side and ignored from the request
        # body (NotesController.Create) - not sent here.
        payload = {
            "title": title,
            "content": content,
            "courseId": course_id,
            "sessionId": session_id,
            "isMarkdown": is_markdown,
        }
        response = await self._request("POST", "/api/notes", json=payload)
        return Note.model_validate(response.json())

    async def create_session(
        self,
        course_id: int,
        course_name: str,
        course_color: str,
        start_time: datetime,
        end_time: datetime,
        topic: str | None = None,
        notes: str | None = None,
        is_completed: bool = False,
        # 1 = default/first timer mode - same fallback SessionsController's own
        # server-side session creation (PlannerController.GenerateExamPlan) uses
        # when there's no interactive timer selection.
        timer_mode_id: int = 1,
    ) -> Session:
        payload = {
            "courseId": course_id,
            "courseName": course_name,
            "courseColor": course_color,
            "startTime": start_time.isoformat(),
            "endTime": end_time.isoformat(),
            "topic": topic,
            "notes": notes,
            "isCompleted": is_completed,
            "timerModeId": timer_mode_id,
        }
        response = await self._request("POST", "/api/sessions", json=payload)
        return Session.model_validate(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()
