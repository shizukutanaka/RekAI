# RekAI developer tasks. Run `make help` for the list.

API_DIR := apps/api
WEB_DIR := apps/web
SDK_DIR := packages/python-sdk
JS_SDK_DIR := packages/js-sdk

# Prefer each package's own venv, so `make check` runs the same tool versions CI
# does. A globally-installed ruff is not equivalent: an older one checks fewer
# files (it skipped packages/python-sdk/README.md, whose code blocks have
# already broken CI once) and formatting rules change between minor releases.
# Falls back to PATH when there is no venv, e.g. inside an activated one.
# Paths are relative because every recipe below `cd`s into its package first.
API_TOOL := $(shell test -x $(API_DIR)/.venv/bin/ruff && echo .venv/bin/ || echo '')
SDK_TOOL := $(shell test -x $(SDK_DIR)/.venv/bin/ruff && echo .venv/bin/ || echo '')

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## --- setup ---

.PHONY: install
install: install-api install-web install-sdk ## Install all dependencies

.PHONY: install-api
install-api: ## Install the API package with dev extras
	cd $(API_DIR) && pip install -e ".[dev]"

.PHONY: install-web
install-web: ## Install web dependencies
	cd $(WEB_DIR) && npm install

.PHONY: install-sdk
install-sdk: ## Install the Python SDK with dev extras
	cd $(SDK_DIR) && pip install -e ".[dev]"

## --- quality ---

.PHONY: check
check: lint typecheck test web-build ## Run all checks (lint, types, tests, web build)

.PHONY: lint
lint: ## Lint the API + SDK (ruff) and web (eslint)
	cd $(API_DIR) && $(API_TOOL)ruff check . && $(API_TOOL)ruff format --check .
	cd $(SDK_DIR) && $(SDK_TOOL)ruff check . && $(SDK_TOOL)ruff format --check .
	cd $(WEB_DIR) && npm run lint

.PHONY: fmt
fmt: ## Auto-format the API (ruff)
	cd $(API_DIR) && $(API_TOOL)ruff check --fix . && $(API_TOOL)ruff format .

.PHONY: typecheck
typecheck: ## Type-check the API (mypy)
	cd $(API_DIR) && $(API_TOOL)mypy rekai

.PHONY: test
test: ## Run the API, Python SDK, JS SDK, and web test suites
	cd $(API_DIR) && $(API_TOOL)pytest -q
	cd $(SDK_DIR) && $(SDK_TOOL)pytest -q
	cd $(JS_SDK_DIR) && npm test
	cd $(WEB_DIR) && npm test

.PHONY: web-build
web-build: ## Build the web app
	cd $(WEB_DIR) && NEXT_TELEMETRY_DISABLED=1 npm run build

## --- run ---

.PHONY: run-api
run-api: ## Run the API with reload (http://localhost:8000)
	cd $(API_DIR) && $(API_TOOL)uvicorn rekai.main:app --reload

.PHONY: run-web
run-web: ## Run the web dev server (http://localhost:3000)
	cd $(WEB_DIR) && npm run dev

## --- docker ---

.PHONY: smoke
smoke: ## Smoke-test a running API (needs jq; BASE_URL=http://localhost:8000)
	scripts/smoke.sh

.PHONY: up
up: ## Build and start the full stack (redis + api + web)
	docker compose up --build

.PHONY: down
down: ## Stop the stack
	docker compose down
