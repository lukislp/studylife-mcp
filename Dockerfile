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
