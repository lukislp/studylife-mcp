# studylife-mcp

An [MCP](https://modelcontextprotocol.io) server exposing [StudyLife](https://github.com/lukislp/studylife)
(a self-hosted Blazor WASM + ASP.NET Core study-management platform) to Claude and
other MCP clients.

Deliberately scoped narrower than its sister project
[studylife-ai](https://github.com/lukislp/studylife-ai): no RAG, no agent loop — the
MCP client (e.g. Claude Desktop) is the agent, this server just exposes cleanly
modeled tools and resources.

**Status:** S4 complete — five read-only tools plus two write tools (`create_note`,
`create_session`) over stdio *and* Streamable HTTP, with a per-tool-call audit
log and a self-contained OAuth 2.1 authorization server (dynamic client
registration, PKCE, multi-user). Verified end-to-end against a real StudyLife
instance, inside Claude Desktop, and via the official MCP Inspector — see
[docs/decisions.md](docs/decisions.md) and [docs/mcp-inspector.md](docs/mcp-inspector.md).

## Setup: Claude Desktop (stdio, single StudyLife account)

1. Copy `.env.example` to `.env` and fill in your StudyLife instance URL and API key
   (Setup page in StudyLife → "StudyLife MCP Server" card → generate a dedicated key).
2. Install dependencies: `uv sync`
3. Add to your Claude Desktop config (`claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "studylife": {
         "command": "uv",
         "args": ["run", "--directory", "/absolute/path/to/studylife-mcp", "studylife-mcp"]
       }
     }
   }
   ```

   Where to find that file depends on how Claude Desktop was installed:
   - Classic installer: `%APPDATA%\Claude\claude_desktop_config.json` (Windows) /
     `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS).
   - MSIX-packaged app (Microsoft Store-style install, package id starting `Claude_...`):
     `%APPDATA%` is redirected to
     `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`.
     In-app: Settings → Developer → "Local MCP servers" opens this same file. Note the
     app's "Benutzerdefinierten Connector hinzufügen" dialog is for **remote** MCP servers
     (URL-based, Streamable HTTP) only — it does not accept a local command; local stdio
     servers are configured exclusively via this JSON file.

4. Restart Claude Desktop (fully quit, not just close the window). The `list_courses`
   tool should appear.

## Setup: Streamable HTTP + OAuth 2.1 (remote, multi-user)

Run this behind your own reverse proxy (TLS terminates there) to add `studylife-mcp`
as a **remote** MCP connector — e.g. via a client's "Custom Connector" URL field.
Unlike stdio mode, this path supports multiple StudyLife users against one running
server: each person logs in with their own StudyLife MCP API key, and every access
token is bound to that one account (see
[docs/decisions.md](docs/decisions.md) — "Multi-user" and "self-built OAuth
Authorization Server").

1. In `.env`, in addition to `STUDYLIFE_BASE_URL`/`STUDYLIFE_API_KEY` (still needed
   as the stdio-mode fallback account), set:

   ```bash
   MCP_PUBLIC_URL=https://studylife-mcp.example.com   # externally reachable, behind your reverse proxy
   MCP_TOKEN_ENCRYPTION_KEY=...                        # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   `MCP_OAUTH_DB_PATH` (default `oauth.db`), `MCP_HTTP_HOST` (default `127.0.0.1`,
   `0.0.0.0` inside Docker), and `MCP_HTTP_PORT` (default `8000`) are optional.

2. Run it:

   ```bash
   uv run studylife-mcp-http
   # or, containerized:
   docker build -t studylife-mcp .
   docker run -p 8000:8000 --env-file .env -v studylife-mcp-data:/app/data studylife-mcp
   ```

3. Add `https://studylife-mcp.example.com` as a remote MCP connector in your
   client. The client registers itself automatically (dynamic client
   registration); on first connect you'll be sent to this server's own login
   page — enter your StudyLife MCP API key there once. Subsequent connections
   reuse the refresh token, no re-login needed.

## Configuration

| Variable | Description |
|---|---|
| `STUDYLIFE_BASE_URL` | Base URL of your StudyLife instance, e.g. `https://studylife.example.com/` |
| `STUDYLIFE_API_KEY` | API key from StudyLife's setup page, sent as the `X-Api-Key` header. The stdio-mode account; also the fallback if HTTP+OAuth mode is unconfigured. |
| `MCP_PUBLIC_URL` | *(HTTP mode only)* Externally reachable base URL of this server, behind your reverse proxy. |
| `MCP_TOKEN_ENCRYPTION_KEY` | *(HTTP mode only)* Fernet key encrypting each user's StudyLife API key at rest in the OAuth store. |
| `MCP_OAUTH_DB_PATH` | *(HTTP mode only)* SQLite file for OAuth clients/tokens/per-user keys. Default `oauth.db`. |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | *(HTTP mode only)* Bind address. Defaults `127.0.0.1:8000` (`0.0.0.0` inside Docker). |

## Tools

| Tool | Effect |
|---|---|
| `list_courses` | Read-only. Lists all courses of the active study program (semester, code, color, icon, topics, ECTS). Does not modify any data. |
| `list_notes` | Read-only. Lists all notes (title, content, course/session link, timestamps). Does not modify any data. |
| `search_notes` | Read-only. Full-text searches notes by title and content. Does not modify any data. |
| `list_sessions` | Read-only. Lists all study sessions/calendar entries (course, time range, topic, notes, completion status). Does not modify any data. |
| `list_course_goals` | Read-only. Lists per-course learning goals (target date, completion status, grade, completed topics, tag). No aggregate ECTS total. Does not modify any data. |
| `create_note` | Creates a new note (title, content, optional course/session link). Does not modify or delete existing data. |
| `create_session` | Creates a new study session/calendar entry for a course and time range. Does not modify or delete existing data. |

In HTTP+OAuth mode, every tool call runs against whichever StudyLife account the
caller's access token belongs to (see "Streamable HTTP + OAuth 2.1" above) — the
tool set itself is identical between transports.

## Development

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

See [docs/decisions.md](docs/decisions.md) for the reasoning behind notable design
decisions, and [docs/mcp-inspector.md](docs/mcp-inspector.md) for the MCP Inspector
verification run.
