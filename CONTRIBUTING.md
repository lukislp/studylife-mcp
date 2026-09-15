# Contributing to StudyLife MCP

Thanks for taking the time. StudyLife MCP is a single-maintainer project, so the process is
deliberately small - but it is the same for every change, including the maintainer's own.

## How changes get in

1. Open an issue first for anything bigger than a typo or an obvious bug fix, so the direction can
   be agreed before you spend time on it. Use the templates under `.github/ISSUE_TEMPLATE/`.
2. Fork the repository (or branch, if you have write access) and make your change on a branch.
3. Open a pull request against `main`. The pull-request template asks for what changed and why.
4. `main` is protected: a PR merges only after the test stage of
   [`.github/workflows/ci.yml`](.github/workflows/ci.yml) is green and the branch is up to date
   with `main` (enable auto-merge and it lands on its own once that is the case). Nobody pushes to
   `main` directly, not even the maintainer.

## What a pull request needs

- **Conventional Commits.** The version and the changelog are generated from the commit messages
  (`feat:` = minor release, `fix:` = patch release, `build:`/`ci:`/`docs:`/`test:` = no release).
  Squash-merge keeps the PR title as the commit message, so give the PR a Conventional Commit
  title.
- **Green required checks.** `lint`, `test`, `contract-test` and `review / dependency-review` are
  required; a red one blocks the merge.
- **Tests for new functionality.** New features and bug fixes come with tests in `tests/`. A PR
  that adds behaviour without a test is asked to add one.
- **Lint, formatting and types.** The `lint` job runs three separate gates - `ruff check .`,
  `ruff format --check .` and `mypy src`. mypy is in **strict** mode here, so new code needs real
  annotations rather than `Any`. Run all three before pushing; `ruff format --check` in particular
  is easy to miss, since `ruff check` passing does not mean the formatting is clean.
- **API contract.** `contract-test` is its own required check and runs
  `tests/test_openapi_contract.py` against the main `studylife` repository's committed OpenAPI
  spec. This server exposes StudyLife data to MCP clients, so a drifting contract breaks them -
  a contract change has to land on both sides.
- **Lockfile.** CI installs with `uv sync --frozen`, so a dependency change means committing the
  updated `uv.lock` alongside `pyproject.toml`.
- **OAuth and tokens.** The OAuth provider and token store are security-relevant surfaces
  (`tests/test_oauth_provider.py`, `tests/test_oauth_store.py`). Changes there need tests and an
  explanation in the PR body.

## Running things locally

Python 3.12 or newer, with [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

The fuzz target is Linux-only (`atheris` publishes no Windows wheel):

```bash
uv sync --frozen --group fuzz
uv run python fuzz/fuzz_models_and_keys.py -max_total_time=30 -rss_limit_mb=1024
```

## Security issues

Please do not open a public issue for a vulnerability - use the private reporting path described
in [SECURITY.md](SECURITY.md). The [Code of Conduct](CODE_OF_CONDUCT.md) applies to every
interaction in this repository.
