from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

# The StudyLife server stores and serializes wall-clock times in its own local time zone
# WITHOUT an offset ("2026-09-04T18:00:00"). That is fine for the app, but the tool output
# schema declares these fields as JSON Schema `date-time`, i.e. RFC 3339, which requires an
# offset - and MCP clients validate structured tool output against that schema, so every
# list_sessions/list_notes call failed with "startTime must match format date-time"
# (2026-09-05). Attaching the server's zone turns the values into valid, unambiguous
# timestamps; already-aware values (a future server sending offsets) are left alone.
_server_timezone = ZoneInfo("Europe/Berlin")


def configure_server_timezone(name: str) -> None:
    """Set the zone the StudyLife server's naive timestamps are expressed in
    (Settings.studylife_timezone; main() calls this once at startup)."""
    global _server_timezone
    _server_timezone = ZoneInfo(name)


class StudyLifeModel(BaseModel):
    """Base for StudyLife DTOs: the API serializes camelCase JSON (ASP.NET
    Core's default), while Python fields stay snake_case. populate_by_name
    keeps snake_case kwargs usable too (e.g. in tests)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @model_validator(mode="after")
    def _attach_server_timezone(self) -> "StudyLifeModel":
        for name, value in list(self.__dict__.items()):
            if isinstance(value, datetime) and value.tzinfo is None:
                setattr(self, name, value.replace(tzinfo=_server_timezone))
        return self


class Course(StudyLifeModel):
    """Mirrors StudyLife.Shared.CourseDto (studylife repo, CourseCatalog.cs)."""

    id: int
    semester: int
    name: str
    code: str
    color: str
    icon: str
    topics: list[str]
    ects: int
    group: str | None = None


class Note(StudyLifeModel):
    """Mirrors StudyLife.Shared.NoteDto (studylife repo, Dtos.cs)."""

    id: int
    course_id: int | None = None
    title: str
    content: str
    created_at: datetime
    updated_at: datetime
    session_id: int | None = None
    is_markdown: bool = False
    source_url: str | None = None
    # 2026-08 note-enrichment fields (studylife repo) - nullable/server-generated, so
    # all three are optional here with defaults matching what an unenriched note (or
    # an older server) actually sends. tags is comma-separated free text like
    # CourseGoal.completed_topics below, not a JSON array on the wire.
    tags: str | None = None
    summary: str | None = None
    related_note_ids: list[int] = Field(default_factory=list)


class Session(StudyLifeModel):
    """Mirrors StudyLife.Shared.StudySessionDto (studylife repo, Dtos.cs)."""

    id: int
    course_id: int
    course_name: str
    course_color: str
    start_time: datetime
    end_time: datetime
    topic: str | None = None
    notes: str | None = None
    is_completed: bool
    timer_mode_id: int
    recurrence_group_id: str | None = None


class CourseGoal(StudyLifeModel):
    """Mirrors StudyLife.Shared.CourseGoalDto (studylife repo, Dtos.cs)."""

    course_id: int
    course_name: str
    target_date: datetime | None = None
    completion_note: str | None = None
    completed_at: datetime | None = None
    grade: float | None = None
    # Comma-separated topic names (CourseCatalog.Topics), not a JSON array on the wire.
    completed_topics: str = ""
    tag: str | None = None
