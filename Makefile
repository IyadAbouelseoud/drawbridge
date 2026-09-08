.PHONY: help up down logs ps build lint fmt type test check clean rls-bootstrap \n	token token-service pilot-seed

help:
	@echo "up      - bring the stack up"
	@echo "down    - tear the stack down"
	@echo "logs    - follow all logs"
	@echo "lint    - ruff check"
	@echo "fmt     - ruff format + fix"
	@echo "type    - mypy --strict"
	@echo "test    - pytest"
	@echo "check   - lint + type + test"
	@echo "rls-bootstrap - create drawbridge_app and grant it; run before up"
	@echo "token TENANT=<uuid> - mint a local user token"
	@echo "token-service - mint the cross-tenant token n8n carries"
	@echo "pilot-seed  - seed both pilot tenants and write their trigger payloads"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

build:
	docker compose build

lint:
	uv run ruff check .

fmt:
	uv run ruff format . && uv run ruff check --fix .

type:
	uv run mypy services mcp_servers packages/schemas/src scripts

test:
	uv run pytest

check: lint type test

# Creates the unprivileged role the services connect as, and grants it what it needs.
# Run once on a fresh database and again after any migration that adds a table — the
# grants are per-table, so a new table is unreadable by the app role until this reruns.
# Row-level security is installed by the migration; without this it enforces nothing,
# because the owner role bypasses every policy.
rls-bootstrap:
	uv run python scripts/rls_bootstrap.py

token:
	@test -n "$(TENANT)" || (echo 'usage: make token TENANT=<uuid>' && exit 2)
	@uv run python scripts/mint_token.py --tenant $(TENANT)

# Cross-tenant by design: one workflow runs whichever tenant its trigger names.
# Put the output in DRAWBRIDGE_SERVICE_TOKEN and treat it accordingly.
token-service:
	@uv run python scripts/mint_token.py --service

pilot-seed:
	uv run python scripts/pilot_us.py --write-payload pilot/
	uv run python scripts/pilot_ksa.py --write-payload pilot/

clean:
	docker compose down -v
