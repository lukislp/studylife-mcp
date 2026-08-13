from pydantic import BaseModel


class Course(BaseModel):
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
