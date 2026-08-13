# MCP Inspector run (S4)

Verified with the official [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
v2 CLI (`npx @modelcontextprotocol/inspector --cli`) against the stdio server
(`uv run studylife-mcp`), 2026-08-13.

## `tools/list`

```bash
npx @modelcontextprotocol/inspector --cli uv run studylife-mcp --method tools/list --format json
```

All 7 tools (`list_courses`, `list_notes`, `search_notes`, `list_sessions`,
`list_course_goals`, `create_note`, `create_session`) were returned with correct,
well-formed JSON Schema `inputSchema`/`outputSchema` — including nested `$defs`
for the `Course`/`Note`/`Session`/`CourseGoal` models, correct `anyOf`
nullable-field handling, and `date-time` formatting on every timestamp field.

## `tools/call` (`list_courses`)

```bash
npx @modelcontextprotocol/inspector --cli uv run studylife-mcp \
  --method tools/call --tool-name list_courses --format json
```

Returned `isError: false`, `structuredContent.result` with all 58 real courses
from the connected StudyLife instance, matching `list_courses`' declared output
schema. The server's own audit log (`studylife_mcp.audit`, stderr) confirmed
the call was logged: `tool=list_courses args_digest=... result=ok
duration_ms=281.0`, alongside the underlying `httpx` request log line — visible
proof the audit-log decorator (`@audited`, see `audit.py`) fires on a real
Inspector-driven call, not just in the mocked unit tests.

## A gotcha worth recording

`npx @modelcontextprotocol/inspector --cli <command...> --method <method>` only
works with the target command **before** `--method` (and any other CLI-only
flags). Passing `uv run --no-sync studylife-mcp --method tools/list` (`--no-sync`
placed inside the target) breaks the Inspector CLI's argument parsing
(`{"error":{"code":"error","message":"Connection closed"}}`) - almost certainly
because Commander.js's `--no-*` convention (auto-generated negation flags)
misparses `--no-sync` as one of the Inspector's own options rather than part of
the forwarded target command. Using plain `uv run studylife-mcp` (letting `uv`
do its normal sync check) avoids the issue entirely.

## Full OAuth 2.1 flow (S4 HTTP transport)

Beyond the Inspector, the entire OAuth 2.1 authorization-code + PKCE flow was
exercised against a live `studylife-mcp-http` instance with a throwaway script:
dynamic client registration (`POST /register`) → `/authorize` (redirects to
this server's own login page) → login form submitted with a real StudyLife MCP
API key → `/token` exchange (access + refresh token) → an authenticated
`tools/call` over Streamable HTTP (`POST /mcp` with `Authorization: Bearer
...`), which returned the same real course data. See
[docs/decisions.md](decisions.md) for the full write-up and the earlier
Docker-based non-root/bind-address verification.
