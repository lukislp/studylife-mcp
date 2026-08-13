from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# Mirrors what audit.py already logs per tool call (tool, ok/error, duration) as
# Prometheus metrics instead of/alongside the stderr log line - scraped by the existing
# self-hosted Prometheus (studylife repo's k8s/14-prometheus.yaml), same setup
# studylife-ai already uses. Only meaningful for the HTTP transport (stdio has no
# scrapeable endpoint and is a short-lived per-session process anyway) - see
# server.py's /metrics route, only registered when HTTP mode is active.
TOOL_CALLS_TOTAL = Counter(
    "studylife_mcp_tool_calls_total",
    "Total MCP tool calls, by tool and outcome.",
    ["tool", "status"],
)

TOOL_CALL_DURATION_SECONDS = Histogram(
    "studylife_mcp_tool_call_duration_seconds",
    "MCP tool call duration in seconds, by tool.",
    ["tool"],
)

# Incremented by rate_limit.py's RateLimitMiddleware whenever it actually rejects a
# request (429) - visibility into whether the DCR/mcp-call rate limits are being hit at
# all, not just that they exist.
RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "studylife_mcp_rate_limit_rejections_total",
    "Total requests rejected by rate limiting, by path.",
    ["path"],
)

# A Gauge, not a Counter - the count goes down too (TTL cleanup, oauth_store.py's
# UNUSED_CLIENT_TTL_SECONDS). "pending" means registered but never completed the OAuth
# flow (no access token issued yet); a client stuck at a high "pending" count relative to
# "activated" is exactly the shape registration-spam abuse would take, so this is the
# metric that would actually show whether the rate-limit/TTL-cleanup pair (see
# rate_limit.py, oauth_store.py) is doing its job under real abuse, not just that it's
# wired up. Set fresh from the database on every /metrics scrape (server.py) rather than
# tracked incrementally in-process - the source of truth is the DB row count, not
# something worth risking drift by mirroring in memory.
REGISTERED_CLIENTS = Gauge(
    "studylife_mcp_registered_clients",
    "Currently registered OAuth clients, by activation status.",
    ["status"],
)


def render_latest() -> tuple[bytes, str]:
    """Returns (body, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
