# Contributing to RekAI

Thanks for your interest in improving RekAI! This document explains how to get a
development environment running and how to submit changes.

## Development setup

Clone the repo and start the stack:

```bash
git clone https://github.com/shizukutanaka/RekAI.git
cd RekAI
docker compose up --build
```

Or run each app directly — see [`apps/api/README.md`](./apps/api/README.md) and
[`apps/web/README.md`](./apps/web/README.md).

A `Makefile` wraps the common tasks — run `make help` to list them:

```bash
make install   # install API + web deps
make check     # lint, type-check, test, web build
make run-api   # / make run-web
```

Optionally enable the git pre-commit hooks (ruff + file hygiene):

```bash
pip install pre-commit && pre-commit install
pre-commit run --all-files   # run them once across the repo
```

## Backend

```bash
cd apps/api
pip install -e ".[dev]"

ruff check .        # lint
ruff format .       # format
mypy rekai          # type check
pytest              # tests
```

All four must pass before a PR is merged — CI enforces this once activated
(see [`.github/README.md`](./.github/README.md) to move the workflow into
`.github/workflows/`).

## Frontend

```bash
cd apps/web
npm install
npm run lint
npm run build
```

## Branching & commits

- Branch off `main` using a descriptive name, e.g. `feat/anthropic-provider`.
- Keep commits small and focused. Write imperative, present-tense messages
  (`add Redis cache`, not `added`).
- Open issues are tagged with milestones (M1–M5); pick something small and
  self-contained for your first contribution.

## Pull requests

1. Make sure tests, lint, and type checks pass (`make check`).
2. Add or update tests for behavior changes.
3. Update docs (`README.md`, `docs/`) and add a `CHANGELOG.md` entry under
   **Unreleased** when you change public behavior.
4. Fill out the PR template.

## Reporting bugs / requesting features

Use the issue templates under **New issue**. Include reproduction steps for
bugs and a clear use case for features.

## Code of Conduct

By participating you agree to abide by our
[Code of Conduct](./CODE_OF_CONDUCT.md).
