# `.github/`

## `ci-workflow.yml` — pending installation as `workflows/ci.yml`

This file is the **single** staged GitHub Actions workflow for this repo, and
it runs every gate in [`CLAUDE.md`](../CLAUDE.md):

| job | gates |
| --- | --- |
| `api` | `ruff check`, `ruff format --check`, `mypy rekai`, `pytest` |
| `python-sdk` | `ruff check`, `ruff format --check`, `pytest` |
| `js-sdk` | `node --test` |
| `web` | `tsc --noEmit`, `npm run lint`, `vitest`, `next build` |
| `smoke` | live `uvicorn` + `scripts/smoke.sh` against `/health`, `/v1/*` |
| `e2e` | Playwright/Chromium against the port-8090 API |
| `docker` | build the API and Web images |

The CI badge in the root README already points at where it belongs.

It lives here rather than at `.github/workflows/ci.yml` because the GitHub App
token used by the agent sessions working on this repo lacks the `workflows`
permission — pushing any commit that touches `.github/workflows/` is rejected
outright:

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

Then delete the section above from this file. Granting the GitHub App the
`workflows` permission would remove the need for this staging step entirely.
