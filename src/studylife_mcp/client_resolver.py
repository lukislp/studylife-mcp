from mcp.server.auth.middleware.auth_context import get_access_token

from studylife_mcp.client import StudyLifeClient
from studylife_mcp.config import Settings
from studylife_mcp.oauth_store import OAuthStore


class StudyLifeClientResolver:
    """Resolves which StudyLife account a tool call should run against.

    stdio mode (Claude Desktop): always the single `.env`-configured account -
    `get_access_token()` returns None outside an HTTP request, so this is a
    no-op there. HTTP+OAuth mode: resolves to whichever StudyLife account the
    caller's access token was issued for (see oauth_provider.py), cached per
    subject for the life of the process so each tool call doesn't re-decrypt
    the stored key.

    Kept out of server.py deliberately: server.py instantiates `Settings()`
    from the real environment at import time, which would make any test that
    imports it depend on a real `.env` (absent in CI). This class takes its
    `Settings` as a constructor argument instead, so it's testable on its own.
    """

    def __init__(self, settings: Settings, oauth_store: OAuthStore | None) -> None:
        self._settings = settings
        self._default_client = StudyLifeClient(settings)
        self._oauth_store = oauth_store
        self._client_by_subject: dict[str, StudyLifeClient] = {}

    async def resolve(self) -> StudyLifeClient:
        if self._oauth_store is None:
            # stdio mode, or HTTP mode not configured at all - always the single
            # .env-configured account.
            return self._default_client

        # HTTP mode is configured: never fall back to the operator's own .env account for
        # an authenticated request missing a usable subject - that would silently leak the
        # operator's StudyLife data to an unrelated caller (audit A14). The MCP SDK's own
        # auth layer should already reject an unauthenticated /mcp call before a tool is
        # ever invoked, so this is defense in depth, not the primary gate.
        access_token = get_access_token()
        if access_token is None or access_token.subject is None:
            raise PermissionError("This request isn't authenticated as a StudyLife account.")

        subject = access_token.subject
        cached = self._client_by_subject.get(subject)
        if cached is not None:
            return cached

        api_key = await self._oauth_store.load_user_key(subject)
        if api_key is None:
            # The token is valid but its subject has no stored key anymore (e.g.
            # removed out of band). Fail closed rather than falling back to the
            # .env account, which would leak that account's data to an unrelated
            # caller.
            raise PermissionError("No StudyLife account is linked to this session anymore.")

        client = StudyLifeClient(self._settings.model_copy(update={"studylife_api_key": api_key}))
        self._client_by_subject[subject] = client
        return client
