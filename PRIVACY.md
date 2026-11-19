# Privacy

This document describes what StudyAtrium MCP stores and where data goes. StudyAtrium MCP
is a self-hosted add-on: the operator of the StudyAtrium instance it is connected to runs it
and is the contact for privacy questions and requests.

## What this server stores

In HTTP mode (the remote, multi-user setup) the server keeps a SQLite database with:

- **Your StudyAtrium MCP API key**, encrypted at rest (Fernet), so the server can call
  StudyAtrium on your behalf. One entry per account.
- **OAuth clients** that registered themselves (client id, name, redirect URIs). These are
  not tied to a person.
- **OAuth tokens and codes**: access tokens, refresh tokens (stored as hashes) and
  authorization codes, each bound to your StudyAtrium user id. An authorization code is
  short-lived (valid for five minutes) and single-use: it is deleted the moment it is
  exchanged for tokens. Codes are stored in plain text for that short time, not hashed.
- **Management sessions** for the `/connected-apps` page (a random session id, your user
  id, a CSRF token), valid for ten minutes.

The server does **not** store your notes, courses or sessions. They are fetched from
StudyAtrium for each tool call and passed on. The audit log records the tool name, a hash
of the arguments, the outcome and the duration, not the content.

In stdio mode (Claude Desktop) nothing is stored by this server; the API key lives in your
own `.env`.

## Fonts and other resources

The sign-in, consent and connected-apps pages use the DM Sans font (SIL Open Font License 1.1),
which this server serves itself. Your browser loads nothing from Google or any other font host,
CDN or third party when it shows these pages. Earlier versions loaded the font from Google Fonts,
which sent your IP address to Google; that is no longer the case. The pages set only the
cookies they need for the sign-in and management flow (browser binding and
management session), no tracking cookies.

## Where tool results go

When your MCP client calls a tool, the result (for example your notes) is sent to that
client. For a client such as Claude, that means the provider of the client (for example
Anthropic) receives it under your own account and terms with that service. This server does
not control what the client does with it. Connect only clients you trust, and revoke access
at `/connected-apps` when you no longer want a client to have it.

## Retention and deletion

- Access tokens expire after 1 hour, refresh tokens after 90 days; expired tokens and
  unredeemed codes are removed by an hourly cleanup (after a one-day grace period).
- Unused client registrations are removed after one day.
- Revoking a client on `/connected-apps` removes its tokens; once no client is connected for
  your account, your stored encrypted key is removed too. The hourly cleanup also removes
  stored keys that have no token left.
- When your StudyAtrium account is deleted, StudyAtrium calls this server's purge endpoint
  and everything stored for you (key, tokens, codes, management sessions) is removed. See
  the README section "Account deletion / purge".

## Controller and publisher

Controller: the operator of your instance. Publisher: see the imprint at
<https://lktec.org/impressum>.

## Contact

Questions, access or deletion requests: contact the operator of the StudyAtrium instance you
use. Security issues: see [SECURITY.md](SECURITY.md).
