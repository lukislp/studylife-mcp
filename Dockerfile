FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir uv

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
