from mcp.server.mcpserver import MCPServer

from studylife_mcp.client import StudyLifeClient
from studylife_mcp.config import Settings
from studylife_mcp.models import Course

mcp = MCPServer("studylife-mcp")
# studylife_base_url/studylife_api_key have no default on purpose (fail loudly
# if unset) - pydantic-settings fills them from the environment/.env at
# runtime, which mypy's synthesized __init__ can't see. Sanctioned pattern,
# see pydantic-settings docs on type checking.
_settings = Settings()  # type: ignore[call-arg]
_client = StudyLifeClient(_settings)


@mcp.tool()
async def list_courses() -> list[Course]:
    """Lists all courses of the currently active study program in StudyLife
    (semester, code, color, icon, topics, ECTS credits). Read-only — does not
    modify any data in StudyLife."""
    return await _client.list_courses()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
