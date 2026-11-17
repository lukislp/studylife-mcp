"""Account-deletion purge (GDPR Art. 17): `POST /internal/users/{user_id}/purge`.

Called server-to-server by the StudyAtrium server when an account is deleted. The route is
internal-only - k8s/07-tailscale-funnel.yaml does not list `/internal`, k8s/06-httproute.yaml
answers it with an error, and k8s/05-network-policies.yaml admits only the StudyAtrium pods -
and additionally guarded by a shared secret that is compared in constant time. With no
secret configured the route answers 503 instead of being open.

This module only authenticates and validates the request; what gets removed lives in
OAuthStore.purge_subjects().
"""

import hmac
import json
import logging
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field, StringConstraints, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from studylife_mcp.config import Settings
from studylife_mcp.oauth_store import OAuthStore

logger = logging.getLogger(__name__)

PURGE_PATH = "/internal/users/{user_id}/purge"
SHARED_SECRET_HEADER = "X-StudyLife-Shared-Secret"
MAX_KEY_HASHES = 50

_Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class PurgeRequest(BaseModel):
    """Body of the purge call: the SHA-256 hex digests of every API key the StudyAtrium
    server issued to this add-on for the deleted user. Legacy grants used that digest as
    their OAuth subject (OAuthStore.subject_for_key)."""

    key_hashes: list[_Sha256Hex] = Field(default_factory=list, max_length=MAX_KEY_HASHES)


def _parse_user_id(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else None


async def _parse_body(request: Request) -> PurgeRequest | None:
    raw = await request.body()
    try:
        return PurgeRequest.model_validate(json.loads(raw) if raw.strip() else {})
    except (ValueError, ValidationError):
        return None


def _secret_matches(configured: str, request: Request) -> bool:
    provided = request.headers.get(SHARED_SECRET_HEADER, "")
    return hmac.compare_digest(provided.encode(), configured.encode())


def register_purge_route(mcp: MCPServer, store: OAuthStore, settings: Settings) -> None:
    # custom_route() has no return-type annotation, so mypy can't see through it -
    # same narrow suppression as the routes in oauth_provider.py / server.py.
    @mcp.custom_route(PURGE_PATH, methods=["POST"])  # type: ignore[untyped-decorator]
    async def purge_account(request: Request) -> Response:
        secret = settings.studylife_purge_secret
        if not secret:
            return JSONResponse({"error": "purge_disabled"}, status_code=503)
        if not _secret_matches(secret, request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        user_id = _parse_user_id(request.path_params["user_id"])
        body = await _parse_body(request)
        if user_id is None or body is None:
            return JSONResponse({"error": "invalid_request"}, status_code=422)

        counts = await store.purge_subjects({str(user_id), *body.key_hashes})
        logger.info(
            "account_purge user_id=%s user_keys=%s access_tokens=%s refresh_tokens=%s "
            "auth_codes=%s management_sessions=%s",
            user_id,
            counts.user_keys,
            counts.access_tokens,
            counts.refresh_tokens,
            counts.auth_codes,
            counts.management_sessions,
        )
        return Response(status_code=204)
