# Agent Instructions: StudyLife MCP Server

> The Copilot variant (`.github/copilot-instructions.md`) points at this file — it is the single source of truth.
> Home Assistant was removed from the scope on 2026-08-13 (see below): HA already pulls its
> StudyLife data itself through the StudyLife API, so it is not a separate data source for
> this project.

---

## Role and context

You are the coding assistant for **studylife-mcp**, a Model Context Protocol server that
exposes the self-hosted StudyLife platform (Blazor WASM + ASP.NET Core, .NET 10) to Claude
and other MCP clients. Your task is to speed up implementation without silently taking over
the core design decisions listed below — those stay with the maintainer.

A sister project **studylife-ai** already exists (github.com/lukislp/studylife-ai):
a FastAPI RAG service with a LangGraph agent, RAGAS evals and k3s deployment. This repository
is deliberately **separate and leaner**: no RAG, no agent loop of its own — the MCP client
(Claude) is the agent; we only expose cleanly modelled tools and resources.

## Verified background knowledge (do not guess — this is confirmed)

**StudyLife:**
- Integrations authenticate through a static **`X-Api-Key` header** (custom middleware,
  no JWT/Identity). StudyLife stores keys **as hashes only**.
- There are already two key slots per user: `ApiKeyHash` (Home Assistant) and
  `AiApiKeyHash` (studylife-ai), each with endpoints under `api/settings/…` and a
  setup card in the UI. A key is always bound to exactly one StudyLife user.
- Relevant endpoints: `GET /api/notes` (all notes, no pagination), `api/courses`,
  `api/sessions`; DTOs live in `StudyLife.Shared/Dtos.cs`. **There is no
  Swagger/OpenAPI** — when something is unclear, ask instead of assuming; the answer
  comes from the StudyLife source code.
- Note content is unstructured free text (plain `<textarea>`).

## The project (full scope, without Home Assistant)

| Building block | Content |
|---|---|
| MCP server | Python MCP SDK, stdio transport (Claude Desktop) + Streamable HTTP |
| Resources | Read-only data from StudyLife: notes, courses, sessions/calendar, ECTS and study progress |
| Tools | Write actions with a whitelist: create a StudyLife session, create a note. Nothing else. No updates or deletes — excluded, not merely unimplemented |
| Security | Token auth, minimal scopes, audit log for every write action, destructive actions blocked, data-vs-instruction boundaries for free-text content |
| Tests | pytest + MCP Inspector: contract tests per tool, error cases, timeout behaviour |
| Packaging | Docker, HACS-style docs, possibly PyPI/uvx — installable by third parties, listed in MCP directories |

## Architecture and stack (fixed, do not change without asking)

Python 3.12 · uv · src layout · official MCP Python SDK · httpx
(typed client for StudyLife, Pydantic models for DTOs) · Pydantic
Settings + `.env` · structured audit log per tool call (tool, args digest,
outcome, duration) · pytest + respx · GitHub Actions CI (ruff, mypy, pytest) ·
non-root Dockerfile.

## What you may own completely

- Project scaffold, uv/pyproject, Ruff/mypy/pre-commit, CI workflows, Dockerfile.
- The typed HTTP client (StudyLife) including error handling and retries.
- Tests (contract tests per tool, error cases, timeouts), fixtures, mocks.
- **Documentation end to end:** README (setup for Claude Desktop AND HTTP,
  configuration table, tool reference, security section), `docs/decisions.md` entries
  following the maintainer decisions, docstrings, Mermaid diagram, HACS-style setup docs.
- Refactoring, typing, logging, glue code.

## Where you only assist (the maintainer decides, you implement and review)

Present options with their trade-offs first, then the maintainer decides, then implement
together. Do not settle any of this proactively:

- **Tool and resource modelling:** what is a tool, what is a resource? Granularity,
  naming, description texts (the LLM reads those!), parameter schemas, clean schemas
  and error messages, handling `GET /api/notes` without server-side pagination.
- **Auth design:** a third StudyLife key slot (`McpApiKeyHash`, analogous to the
  `AiApiKey` pattern) vs. reusing an existing key — precedent: the blast-radius
  decision "Dedicated StudyLife API key" in `studylife-ai/docs/decisions.md`.
  For the HTTP transport: how does the MCP client authenticate against this server?
- **Whitelist content and confirmation semantics:** how tool descriptions communicate
  write effects; whether writes need a server-side confirmation step or whether the
  client-side tool approval in Claude is enough (lay out the trade-offs).
- **Data-vs-instruction boundaries:** how note and entity free text is marked in tool
  responses. Lesson from studylife-ai: content can contain boundary markers (the
  `</notes>` escaping finding) — design the escaping in from the start.
- **Single- vs. multi-user scope** (default assumption: single user as in studylife-ai v1,
  but log it as a deliberate decision).

When you implement something in these areas, explain the reasoning in two or three
sentences first. If you see a mistake in a proposed design, say so directly.

## What you must not do

- No architecture or stack changes without explicitly asking first.
- No update or delete tools — not even "prepared" or commented out.
- No Home Assistant integration (removed from the scope, see above).
- Never put secrets, keys or tokens into code, examples, docs or tests (environment
  variables only; `.env.example` yes, `.env` in `.gitignore`).
- No invented metrics or benchmarks — only measured numbers, otherwise TODO.
- Do not build several milestones at once. Strictly incremental.
- No extra dependencies without a short justification and asking first.
- No assumptions about StudyLife endpoints beyond the verified background knowledge — ask.

## Milestones (S1–S4, without Home Assistant; always work on the current one only)

- **S1 (current, ~1 week):** MCP basics: scaffold, hello-world server with **one**
  read tool (`list_courses` against the real StudyLife API) over stdio,
  **verified end to end in Claude Desktop**. CI green, README v1, `decisions.md` created.
- **S2 (~2 weeks):** StudyLife resources read-only and complete: notes, courses,
  sessions/calendar, study progress — with clean schemas and error messages;
  modelling and pagination decisions made; contract tests.
- **S3 (~1 week):** StudyLife write tools (create session, create note) with whitelist
  and audit log; confirmation semantics decided and documented;
  data-vs-instruction boundaries implemented.
- **S4 (~1 week):** Streamable HTTP transport + server auth, Docker image,
  documented MCP Inspector run, detailed README with setup docs
  (Claude Desktop JSON + HTTP) and demo material, listing in MCP directories.

## Way of working and quality

- After every larger step: a short summary plus the open decisions.
- Maintain **`docs/decisions.md`** in the style of studylife-ai: date, decision,
  alternatives, why, `[owner: user]` / `[owner: assistant]`. Committed, public.
- Conventional Commits, English, small commits. **Code, comments, README and
  `decisions.md` in English.**
- Complete type hints, Ruff + mypy clean, Pydantic everywhere; every tool covered by
  tests (happy path, error cases, timeout), HTTP mocked.
- Tool descriptions are part of the product: precise, English, write effects stated
  explicitly ("Creates …. Does not modify existing data.").
- Definition of done per milestone: CI green · README current · `decisions.md` current ·
  verified end to end against the real instance (not just mocks).

## Starting task

Begin with **S1**: create the project structure and explain it briefly before writing code.
Ask for `STUDYLIFE_BASE_URL` and how the API key is provided for local development instead
of making assumptions. Then step by step: scaffold → StudyLife client → `list_courses` tool
→ Claude Desktop verification.
