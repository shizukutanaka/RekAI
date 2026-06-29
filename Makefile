# RekAI developer tasks. Run `make help` for the list.

API_DIR := apps/api
WEB_DIR := apps/web

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## --- setup ---

.PHONY: install
install: install-api install-web ## Install API and web dependencies

.PHONY: install-api
install-api: ## Install the API package with dev extras
	cd $(API_DIR) && pip install -e ".[dev]"

.PHONY: install-web
install-web: ## Install web dependencies
	cd $(WEB_DIR) && npm install

## --- quality ---

.PHONY: check
check: lint typecheck test web-build ## Run all checks (lint, types, tests, web build)

.PHONY: lint
lint: ## Lint the API (ruff) and web (eslint)
	cd $(API_DIR) && ruff check . && ruff format --check .
	cd $(WEB_DIR) && npm run lint

.PHONY: fmt
fmt: ## Auto-format the API (ruff)
	cd $(API_DIR) && ruff check --fix . && ruff format .

.PHONY: typecheck
typecheck: ## Type-check the API (mypy)
	cd $(API_DIR) && mypy rekai

.PHONY: test
test: ## Run the API test suite (pytest)
	cd $(API_DIR) && pytest -q

.PHONY: web-build
web-build: ## Build the web app
	cd $(WEB_DIR) && NEXT_TELEMETRY_DISABLED=1 npm run build

## --- run ---

.PHONY: run-api
run-api: ## Run the API with reload (http://localhost:8000)
	cd $(API_DIR) && uvicorn rekai.main:app --reload

.PHONY: run-web
run-web: ## Run the web dev server (http://localhost:3000)
	cd $(WEB_DIR) && npm run dev

## --- docker ---

.PHONY: up
up: ## Build and start the full stack (redis + api + web)
	docker compose up --build

.PHONY: down
down: ## Stop the stack
	docker compose down
