from pydantic import AnyHttpUrl, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    studylife_base_url: HttpUrl

    # Required only for stdio mode (main() checks this explicitly) - it's the single
    # account stdio always runs as. Optional here because HTTP mode no longer needs a
    # fallback account: StudyLifeClientResolver fails closed instead of falling back to
    # it for an unauthenticated/subjectless caller (audit A14), so a pure-HTTP deployment
    # can leave this unset.
    studylife_api_key: str | None = None

    # Path to a PEM CA bundle to trust for `studylife_base_url`, INSTEAD of the OS
    # certificate store - for a private CA the OS store doesn't know (e.g. a
    # cluster-internal cert-manager issuer). Unset (the default) keeps using the OS
    # store via truststore, correct for a normal publicly-trusted-or-plain-HTTP setup.
    studylife_ca_cert_path: str | None = None

    # --- S4: Streamable HTTP transport + OAuth 2.1 authorization server ---
    # All optional, with no bearing on stdio mode (main()) - only main_http() requires
    # mcp_public_url, mcp_token_encryption_key, and studylife_connect_url to be set,
    # checked explicitly there rather than making them required for every stdio user who
    # never touches HTTP mode.

    # Externally reachable base URL of this server behind the user's own reverse proxy
    # (TLS terminates there) - used as both the OAuth issuer_url and resource_server_url,
    # since this server is both AS and RS. E.g. "https://studylife-mcp.example.com".
    # Typed AnyHttpUrl (not HttpUrl) to match mcp.server.auth.settings.AuthSettings'
    # own field types directly, without a conversion at the call site.
    mcp_public_url: AnyHttpUrl | None = None

    # StudyLife's own public/browser-facing base URL (identity-contract-v1 §2) - the
    # OAuth authorize() step redirects the user's browser here to `/connect/mcp` for
    # login + consent. Deliberately separate from studylife_base_url, which stays the
    # cluster-internal/direct URL this server calls server-to-server (assertion
    # exchange, and every StudyLife API request) - the two are not interchangeable: the
    # browser can't reach the cluster-internal one, and this server shouldn't route
    # normal API traffic through the public one.
    studylife_connect_url: AnyHttpUrl | None = None

    # Fernet key (32 url-safe base64-encoded bytes, e.g. via
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
    # encrypting each user's StudyLife API key at rest in the OAuth store - unlike
    # StudyLife's own hash-only key storage, this server needs the plaintext back to call
    # StudyLife on the user's behalf, so hashing alone isn't an option here.
    mcp_token_encryption_key: str | None = None

    # SQLite file for OAuth clients/codes/tokens/per-user StudyLife keys (oauth_store.py).
    # Relative to the working directory the server is started from; gitignored like .env.
    mcp_oauth_db_path: str = "oauth.db"

    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8000
