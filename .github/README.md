# `.github/`

| File | Purpose |
| --- | --- |
| `workflows/ci.yml` | The verification workflow. Runs every gate in [`CLAUDE.md`](../CLAUDE.md) on pushes to `main` and `claude/**`, and on pull requests. |
| `dependabot.yml` | Dependency update schedule. |
| `ISSUE_TEMPLATE/` | Bug report and feature request forms. |
| `pull_request_template.md` | PR description scaffold. |

## What CI covers

| job | gates |
| --- | --- |
| `api` | `ruff check`, `ruff format --check`, `mypy rekai`, `pytest` — on Python 3.10 (the `requires-python` floor) and 3.12 (the version the shipped image runs) |
| `python-sdk` | `ruff check`, `ruff format --check`, `pytest` |
| `js-sdk` | `node --test` |
| `web` | `tsc --noEmit`, `npm run lint`, `vitest`, `next build` |
| `smoke` | live `uvicorn` + `scripts/smoke.sh` against `/health` and `/v1/*` |
| `e2e` | Playwright/Chromium against a real API |
| `docker` | builds the API and Web images — the only build verification they get, since agent sessions on this repo have no Docker daemon |

Note that the `api` and `python-sdk` jobs run `ruff format --check .`, which is
broader than the `ruff format --check rekai tests` in `CLAUDE.md`: it also checks
Python code blocks inside the packages' `README.md`. Match the CI command, not
the narrower one, before pushing.
