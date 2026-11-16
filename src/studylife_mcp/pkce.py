"""PKCE (RFC 7636, S256 only) for StudyAtrium's `/connect/mcp` consent flow.

Shared by both callers of that flow - the `studylife-mcp-login` CLI (login.py) and the
HTTP transport's OAuth provider (oauth_provider.py) - so the pair generation lives in
exactly one place. The challenge travels in the consent URL; the verifier never leaves
this process except in the server-to-server assertion exchange body.
"""

from __future__ import annotations

import base64
import hashlib
import secrets


def challenge_for(verifier: str) -> str:
    """The S256 code_challenge for `verifier`: base64url(SHA-256(ASCII(verifier))), unpadded."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_pkce_pair() -> tuple[str, str]:
    """A fresh (code_verifier, code_challenge) pair: a 43-character verifier from 32
    CSPRNG bytes (base64url alphabet, a subset of RFC 7636's unreserved characters)."""
    verifier = secrets.token_urlsafe(32)
    return verifier, challenge_for(verifier)
