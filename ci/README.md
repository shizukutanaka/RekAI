# CI workflow

This directory holds the GitHub Actions workflow for RekAI.

> **Why is it here and not in `.github/workflows/`?**
> The automation account that bootstrapped this repository does not have the
> `workflows` permission, so it could not push files under
> `.github/workflows/`. The workflow is staged here instead.

## Activate CI

A maintainer with write access needs to move the file into place once:

```bash
mkdir -p .github/workflows
git mv ci/ci.yml .github/workflows/ci.yml
git commit -m "ci: enable GitHub Actions workflow"
git push
```

After that, pushes and pull requests will run:

- **api** — `ruff check`, `ruff format --check`, `mypy`, `pytest`
- **web** — `npm ci`, `npm run lint`, `npm run build`
- **docker** — build the API and Web images
