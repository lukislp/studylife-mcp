import pytest

from studylife_mcp.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        studylife_base_url="https://studylife.example.test/",
        studylife_api_key="test-key",
    )
