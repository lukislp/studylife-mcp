## [1.10.3](https://github.com/lukislp/studylife-mcp/compare/v1.10.2...v1.10.3) (2026-08-24)


### Bug Fixes

* **k8s:** opt the data volume into the nightly Velero backup ([e884be7](https://github.com/lukislp/studylife-mcp/commit/e884be7ac5b8df1b1657138f7e2e2cad108bc347))

## [1.10.2](https://github.com/lukislp/studylife-mcp/compare/v1.10.1...v1.10.2) (2026-08-24)


### Bug Fixes

* **k8s:** acknowledge the funnel proxy's accepted autodoc findings ([a2dd5f3](https://github.com/lukislp/studylife-mcp/commit/a2dd5f38ea8d3b02dbc0248ae4dc2638f0bc4cd2))

## [1.10.1](https://github.com/lukislp/studylife-mcp/compare/v1.10.0...v1.10.1) (2026-08-24)


### Bug Fixes

* **k8s:** harden the tailscale operator and Funnel proxy ([d3867ef](https://github.com/lukislp/studylife-mcp/commit/d3867ef0c41e6113db839a7d34f748a7abe66650))

# [1.10.0](https://github.com/lukislp/studylife-mcp/compare/v1.9.0...v1.10.0) (2026-08-22)


### Features

* own this repo's Flux GitOps wiring ([77463c7](https://github.com/lukislp/studylife-mcp/commit/77463c7c189397c891589f181994f90eca8eda26))

# [1.9.0](https://github.com/lukislp/studylife-mcp/compare/v1.8.0...v1.9.0) (2026-08-21)


### Features

* add source_url to Note model ([7299337](https://github.com/lukislp/studylife-mcp/commit/7299337f4fdcfca3b020bdc50e9fc28bc271a525))

# [1.8.0](https://github.com/lukislp/studylife-mcp/compare/v1.7.1...v1.8.0) (2026-08-16)


### Features

* sync Note model and create_note with StudyLife's Markdown flag ([0c78141](https://github.com/lukislp/studylife-mcp/commit/0c7814159663cf4555b2ce8b1d097786372e220b))

## [1.7.1](https://github.com/lukislp/studylife-mcp/compare/v1.7.0...v1.7.1) (2026-08-14)


### Bug Fixes

* **oauth:** add periodic cleanup for expired client registrations ([444b3fc](https://github.com/lukislp/studylife-mcp/commit/444b3fcc9ebb7bcf44243bdd49d717e937d1cafe))

# [1.7.0](https://github.com/lukislp/studylife-mcp/compare/v1.6.0...v1.7.0) (2026-08-13)


### Features

* registered-clients gauge, showing activated vs. pending DCR clients ([e16838b](https://github.com/lukislp/studylife-mcp/commit/e16838be51087340df5c0e32166ceeb30c35ee54))

# [1.6.0](https://github.com/lukislp/studylife-mcp/compare/v1.5.0...v1.6.0) (2026-08-13)


### Features

* Prometheus metrics for tool calls and rate-limit rejections ([2d99d11](https://github.com/lukislp/studylife-mcp/commit/2d99d112175712394d48875be25ffcf93e75e0e9))

# [1.5.0](https://github.com/lukislp/studylife-mcp/compare/v1.4.1...v1.5.0) (2026-08-13)


### Bug Fixes

* **k8s:** add the studylife-mcp-public Gateway listener too ([37becd8](https://github.com/lukislp/studylife-mcp/commit/37becd8b0c0b61569a1301b5474e26ce452af922))


### Features

* rate-limit /mcp calls per-token, not just /register per-IP ([310e7ac](https://github.com/lukislp/studylife-mcp/commit/310e7ac79b6831f4fec849cab39e30d8e6e34d6f))

## [1.4.1](https://github.com/lukislp/studylife-mcp/compare/v1.4.0...v1.4.1) (2026-08-13)


### Bug Fixes

* **k8s:** correct internal LAN hostname from home.lan to heim.lan ([4dc543f](https://github.com/lukislp/studylife-mcp/commit/4dc543fdbf2014dd01675d63cd1057ffbeee4edf))

# [1.4.0](https://github.com/lukislp/studylife-mcp/compare/v1.3.0...v1.4.0) (2026-08-13)


### Features

* connected-apps self-service page, deliberately internal-only ([0c91720](https://github.com/lukislp/studylife-mcp/commit/0c91720d4b6df8408ab163be889cff2205fb298f))

# [1.3.0](https://github.com/lukislp/studylife-mcp/compare/v1.2.1...v1.3.0) (2026-08-13)


### Features

* match the OAuth login page to StudyLife's own design system ([a364483](https://github.com/lukislp/studylife-mcp/commit/a364483265d1fe684fca7cf7c9492ac313543d42))

## [1.2.1](https://github.com/lukislp/studylife-mcp/compare/v1.2.0...v1.2.1) (2026-08-13)


### Bug Fixes

* **k8s:** move MCP_PUBLIC_URL into the Secret, point it at the public Funnel URL ([ded8558](https://github.com/lukislp/studylife-mcp/commit/ded8558d2e6c9c9ecc2e7d59d8b286ec635ff19c))

# [1.2.0](https://github.com/lukislp/studylife-mcp/compare/v1.1.1...v1.2.0) (2026-08-13)


### Features

* **k8s:** expose studylife-mcp publicly via Tailscale Funnel ([1f3c2d0](https://github.com/lukislp/studylife-mcp/commit/1f3c2d0d25e336cab1ac8c9c56ae4a332abf0567))

## [1.1.1](https://github.com/lukislp/studylife-mcp/compare/v1.1.0...v1.1.1) (2026-08-13)


### Bug Fixes

* **ci:** tag images by semantic-release's real output, not the dry run ([58858ee](https://github.com/lukislp/studylife-mcp/commit/58858eee2986da630f53b76d4c4b5545a5dcecc2))

# [1.1.0](https://github.com/lukislp/studylife-mcp/compare/v1.0.0...v1.1.0) (2026-08-13)


### Features

* rate-limit and TTL-clean unauthenticated OAuth client registration ([ba600eb](https://github.com/lukislp/studylife-mcp/commit/ba600eb235fe322810952571cd58900dd5908abd))
* support a private CA cert for StudyLife TLS trust, add /health route ([755ffb1](https://github.com/lukislp/studylife-mcp/commit/755ffb1de395882a8534464dc0bd411a50d1a351))

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
