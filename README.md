# studylife-mcp

An [MCP](https://modelcontextprotocol.io) server exposing [StudyLife](https://github.com/lukislp/studylife)
(a self-hosted Blazor WASM + ASP.NET Core study-management platform) to Claude and
other MCP clients.

Deliberately scoped narrower than its sister project
[studylife-ai](https://github.com/lukislp/studylife-ai): no RAG, no agent loop — the
MCP client (e.g. Claude Desktop) is the agent, this server just exposes cleanly
modeled tools and resources.

**Status:** S2 in progress — five read-only tools over stdio, verified end-to-end
against a real StudyLife instance and (S1) inside Claude Desktop.

## Setup (Claude Desktop, stdio)

1. Copy `.env.example` to `.env` and fill in your StudyLife instance URL and API key
   (Setup page in StudyLife → generate an API key; S1 reuses the existing "Home
   Assistant" key slot for convenience, see [docs/decisions.md](docs/decisions.md)).
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

## Configuration

| Variable | Description |
|---|---|
| `STUDYLIFE_BASE_URL` | Base URL of your StudyLife instance, e.g. `https://studylife.example.com/` |
| `STUDYLIFE_API_KEY` | API key from StudyLife's setup page, sent as the `X-Api-Key` header |

## Tools

| Tool | Effect |
|---|---|
| `list_courses` | Read-only. Lists all courses of the active study program (semester, code, color, icon, topics, ECTS). Does not modify any data. |
| `list_notes` | Read-only. Lists all notes (title, content, course/session link, timestamps). Does not modify any data. |
| `search_notes` | Read-only. Full-text searches notes by title and content. Does not modify any data. |
| `list_sessions` | Read-only. Lists all study sessions/calendar entries (course, time range, topic, notes, completion status). Does not modify any data. |
| `list_course_goals` | Read-only. Lists per-course learning goals (target date, completion status, grade, completed topics, tag). No aggregate ECTS total. Does not modify any data. |

## Development

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

See [docs/decisions.md](docs/decisions.md) for the reasoning behind notable design decisions.
