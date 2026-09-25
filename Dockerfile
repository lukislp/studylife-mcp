# uv is taken from Astral's own image (pinned by tag + digest, both kept current by
# Dependabot's docker updates) instead of an unpinned `pip install uv`.
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 AS uv

FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# Pull in Debian's security updates on every build: the digest-pinned base image lags behind
# the security archive (fixed CVEs in the base layer blocked the Trivy CRITICAL gate on
# 2026-09-13) and a rebuild is cheaper than waiting for the next python:3.12-slim digest.
# HTTPS mirror because plain-http port 80 is blocked on some build hosts.
RUN sed -i 's#http://deb.debian.org#https://deb.debian.org#' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get -y --no-install-recommends upgrade \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first so this layer is cached as long as
# pyproject.toml / uv.lock don't change (source changes shouldn't
# trigger a full dependency reinstall).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# hatch-vcs derives the package version from git tags, but this image never COPYs .git
# (deliberately, to keep the dependency layer above cacheable across releases) - so
# building the project itself below has no VCS history to read. CI passes the exact
# semantic-release version it already computed as a build-arg (see the docker job in
# ci.yml); the fallback below is only for an ad-hoc local `docker build` with no
# --build-arg, and is never what ends up in a published image.
# Deliberately the un-suffixed SETUPTOOLS_SCM_PRETEND_VERSION, not the dist-specific
# `_FOR_STUDYLIFE_MCP` variant - hatch-vcs's own get_version() call never passes a
# dist_name, so the dist-specific form is silently never matched (verified locally: it
# builds with the wrong/fallback version with no error). Fine here since exactly one
# package is ever built in this image.
ARG PACKAGE_VERSION=0.0.0+unknown
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${PACKAGE_VERSION}

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# /app/data must exist (and be owned by appuser) before the named volume mounts
# over it - otherwise Docker auto-creates the mount point as root, and the
# non-root appuser below can't open its OAuth SQLite store there.
RUN mkdir -p /app/data && useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH"
# Container-appropriate defaults, distinct from the bare-metal-dev defaults in
# config.py: bind on all interfaces (the host's own reverse proxy is the only
# thing that should be internet-facing) and persist the OAuth store under the
# volume-mountable /app/data instead of the working directory.
ENV MCP_HTTP_HOST=0.0.0.0
ENV MCP_OAUTH_DB_PATH=/app/data/oauth.db

# This image only runs the S4 HTTP+OAuth entrypoint (main_http) - stdio mode
# is meant to be launched locally by Claude Desktop as a subprocess, not
# containerized. STUDYLIFE_BASE_URL/STUDYLIFE_API_KEY/MCP_PUBLIC_URL/
# MCP_TOKEN_ENCRYPTION_KEY must be supplied at runtime (env vars / a mounted
# .env) - none of them are baked into the image.
EXPOSE 8000

CMD ["studylife-mcp-http"]
