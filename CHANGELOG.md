# 1.0.0 (2026-08-13)


### Features

* **audit:** add [@audited](https://github.com/audited) decorator for structured per-tool-call logging ([49aa57b](https://github.com/lukislp/studylife-mcp/commit/49aa57b9d9f43dd3f311841061fdbc37838ae856))
* **ci:** add semantic-release + Docker publish to GHCR + Trivy scan ([73b18e2](https://github.com/lukislp/studylife-mcp/commit/73b18e273e29e2726eeb7716aa4a896ad7b72988))
* **client:** add create_note, create_session; improve error messages ([f920fea](https://github.com/lukislp/studylife-mcp/commit/f920fea941032317d97903a33c44fb4767f4f15f))
* **client:** add notes, sessions, course goals methods ([466c365](https://github.com/lukislp/studylife-mcp/commit/466c365f7cae190afc0228ad6a1ab725118a07a2))
* **config:** add S4 settings for Streamable HTTP + OAuth 2.1 ([0244350](https://github.com/lukislp/studylife-mcp/commit/0244350f01f3ce96106585d47c2c76e783bf3f01))
* **docker:** add non-root Dockerfile for the HTTP+OAuth server ([8ab558e](https://github.com/lukislp/studylife-mcp/commit/8ab558e91d89ce78fcc3ff89615d74997e1fa341))
* **models:** add Note, Session, CourseGoal with camelCase alias support ([2fb78d0](https://github.com/lukislp/studylife-mcp/commit/2fb78d0d1a1c106544569edb9187bc77705929cc))
* **oauth:** add OAuth 2.1 authorization server provider + login route ([33f5f17](https://github.com/lukislp/studylife-mcp/commit/33f5f176c9cb1dc5ae8b72017a60e087cb5260b5))
* **oauth:** add SQLite-backed OAuth store ([68afa4e](https://github.com/lukislp/studylife-mcp/commit/68afa4e9a2d868fcab6295b1d19a40f655b2c53b))
* scaffold MCP server with list_courses read tool (S1) ([d9c3246](https://github.com/lukislp/studylife-mcp/commit/d9c32464230b5ec336a01c12e99ec773ccfbdeca))
* **server:** add create_note, create_session tools; wire up audit logging ([d4510a3](https://github.com/lukislp/studylife-mcp/commit/d4510a393056466fba3fe6f6f5fef5c0c77111d3))
* **server:** add list_notes, search_notes, list_sessions, list_course_goals tools ([c14a64a](https://github.com/lukislp/studylife-mcp/commit/c14a64a76596bf0ae6238654f594b49300adbed3))
* **server:** wire up Streamable HTTP + OAuth, add main_http entrypoint ([a128934](https://github.com/lukislp/studylife-mcp/commit/a1289341880ac87ac17f38cc89bb07985f8ac2e3))
