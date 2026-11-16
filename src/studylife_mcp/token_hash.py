"""Hashing of OAuth access/refresh tokens for storage at rest (audit 2026-11-16 L22).

The tokens this server mints are high-entropy random strings, so a plain SHA-256
digest is enough: there is nothing to brute-force, and a deterministic digest keeps
`WHERE token = ?` an indexed primary-key lookup. A database leak (backup, volume
snapshot) therefore no longer yields usable bearer tokens.

Stored values carry a `sha256:` prefix. A freshly minted token is URL-safe base64
and can never contain a colon, so the prefix doubles as the marker that tells an
already-upgraded row from a legacy plaintext one.
"""

import hashlib

_PREFIX = "sha256:"


def hash_token(token: str) -> str:
    """The value stored in (and looked up from) the `token` column."""
    return _PREFIX + hashlib.sha256(token.encode()).hexdigest()


def is_hashed(stored: str) -> bool:
    return stored.startswith(_PREFIX)
