import ssl

import httpx
import truststore

from studylife_mcp.config import Settings
from studylife_mcp.models import Course


class StudyLifeClient:
    """Typed async client for the StudyLife REST API (X-Api-Key auth)."""

    def __init__(self, settings: Settings) -> None:
        self._client = httpx.AsyncClient(
            base_url=str(settings.studylife_base_url),
            headers={"X-Api-Key": settings.studylife_api_key},
            timeout=10.0,
            # Verify against the OS certificate store instead of only certifi's bundle,
            # so a locally trusted cert (e.g. the ASP.NET Core HTTPS dev cert registered
            # via `dotnet dev-certs https --trust`) is accepted for local development.
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    async def list_courses(self) -> list[Course]:
        response = await self._client.get("/api/courses")
        response.raise_for_status()
        return [Course.model_validate(item) for item in response.json()]

    async def aclose(self) -> None:
        await self._client.aclose()
