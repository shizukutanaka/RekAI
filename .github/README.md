# `.github/`

## `ci-workflow.yml` — pending installation as `workflows/ci.yml`

The complete GitHub Actions workflow for RekAI. It runs every gate in
[`CLAUDE.md`](../CLAUDE.md) and one that no agent session can run at all:

| Job | Covers |
|---|---|
| `api` | `ruff check`, `ruff format --check`, `mypy`, `pytest` — on Python 3.10 (the `requires-python` floor) and 3.12 (what the shipped image runs) |
| `python-sdk` | `ruff`, `pytest` |
| `js-sdk` | `node --test` |
| `web` | `lint`, `vitest`, `build` |
| `e2e` | Playwright/Chromium against a real API |
| `stack` | `docker compose config -q`, `compose up --build --wait`, then `scripts/smoke.sh` against the running containers |

The `stack` job matters most: agent sessions working on this repo have **no
Docker daemon**, so CI is the only place the compose file and both Dockerfiles
are ever build-verified. The CI badge in the root README already points at where
this file belongs.

It lives here rather than at `.github/workflows/ci.yml` because the GitHub App
token used by those sessions lacks the `workflows` permission — pushing any
commit that touches `.github/workflows/` is rejected outright:

```
! [remote rejected] ... refusing to allow a GitHub App to create or update
  workflow `.github/workflows/ci.yml` without `workflows` permission
```

**To install it**, a maintainer (or any push with the `workflows` scope) runs:

```bash
mkdir -p .github/workflows
git mv .github/ci-workflow.yml .github/workflows/ci.yml
git commit -m "ci: install the verification-gate workflow"
git push
```

Then delete this section. Granting the GitHub App the `workflows` permission
would remove the staging step entirely.
