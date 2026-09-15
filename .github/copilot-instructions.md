# Copilot Instructions — studylife-mcp

> **The single source of truth is `CLAUDE.md` in the repository root — if the two disagree, `CLAUDE.md` wins.**
> This file is a compact summary of the binding rules so that Copilot reliably has them in context.

## Project

MCP (Model Context Protocol) server exposing the self-hosted StudyLife platform
(ASP.NET Core, custom `X-Api-Key` auth, keys stored hash-only) AND selected
read-mostly parts of a Home Assistant instance (long-lived access token) to Claude
and other MCP clients.
Python 3.12, official MCP SDK (FastMCP), stdio + Streamable HTTP transports,
httpx clients, Pydantic everywhere, uv, src-layout. Sister project: studylife-ai
(separate repo — no RAG and no agent loop in THIS repo; the MCP client is the agent).

## Hard rules

- **Milestones S1–S5 (defined in CLAUDE.md) are worked strictly one at a time.**
  Never build ahead. S1 = scaffold + one read tool (`list_courses`) verified
  end-to-end in Claude Desktop via stdio.
- **Write tools are a strict whitelist:** StudyLife create-session and create-note;
  Home Assistant: only 2–3 explicitly approved safe actions. Never build
  update/delete tools or non-whitelisted HA actions — not even scaffolded or
  commented out.
- **Never put secrets/API keys/tokens** in code, tests, examples, or docs.
  Env vars only; ship `.env.example`, gitignore `.env`.
- **No invented metrics or benchmarks** in README/docs. Only measured numbers; otherwise TODO.
- **No new dependencies** without a one-line justification and explicit approval.
- **No assumptions about StudyLife or HA endpoints/DTOs** beyond the verified
  knowledge listed in CLAUDE.md — ask instead (there is no Swagger in StudyLife).
- Decision areas owned by the maintainer (assist only: present trade-offs first, never
  decide silently): tool/resource modeling and description texts, pagination
  handling, auth design (dedicated `McpApiKeyHash` slot vs. key reuse; HA token
  scope; HTTP-transport client auth), the exact HA action whitelist, write
  confirmation semantics (server-side step vs. client tool approval),
  data-vs-instruction boundaries for free-text content (note the `</notes>`
  escaping lesson from studylife-ai), single- vs. multi-user scope.

## Conventions

- Full type hints; ruff + mypy clean; Pydantic models for all DTOs and tool schemas.
- Every tool gets pytest contract tests (happy path, error cases, timeout) with
  HTTP mocked (respx); MCP Inspector run documented in S5.
- Tool descriptions are product surface: precise, English, state write effects
  explicitly ("Creates …. Does not modify existing data.").
- Structured audit log line for every tool call (tool, args digest, outcome, duration).
- Maintain `docs/decisions.md` (ADR-style: date, decision, alternatives, why,
  `[owner: user]` / `[owner: assistant]`). Committed and public.
- Conventional Commits, English, small commits. Code, comments and docs in English.

## Definition of done (per milestone)

CI green (ruff, mypy, pytest) · README updated · decisions.md updated ·
verified end-to-end against the real StudyLife / HA instance (not just mocks)
where applicable.
