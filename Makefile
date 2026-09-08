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
	@echo "pilot-run   - push both pilot corpora through the deployed pipeline"
	@echo "secrets-init  - generate .secrets.json; additive, add --force to rotate"
	@echo "secrets-show  - secret names, sources and redactions; never values"
	@echo "secrets-check - what a production start would refuse on"

# n8n, Authentik and MinIO read credentials from the environment and cannot be taught to
# read .secrets.json, so the file is bridged into this one invocation's environment. The
# values never reach .env, and `set -a` scopes them to the compose process and its
# children rather than the shell you ran make from.
up:
	@test -f .secrets.json || (echo 'no .secrets.json — run: make secrets-init' && exit 2)
	set -a; . <(uv run python scripts/manage_secrets.py env); set +a; 		docker compose up -d --build

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
# Reads the role password from .secrets.json via the same loader the services use, so
# the role's password and the one the API connects with cannot drift apart.
rls-bootstrap:
	set -a; . <(uv run python scripts/manage_secrets.py env); set +a; 		uv run python scripts/rls_bootstrap.py

secrets-init:
	@uv run python scripts/manage_secrets.py init $(ARGS)

secrets-show:
	@uv run python scripts/manage_secrets.py show

secrets-check:
	@uv run python scripts/manage_secrets.py check

token:
	@test -n "$(TENANT)" || (echo 'usage: make token TENANT=<uuid>' && exit 2)
	@uv run python scripts/mint_token.py --tenant $(TENANT)

# Cross-tenant by design: one workflow runs whichever tenant its trigger names.
# Put the output in DRAWBRIDGE_SERVICE_TOKEN and treat it accordingly.
token-service:
	@uv run python scripts/mint_token.py --service --save --ttl 86400

pilot-seed:
	uv run python scripts/pilot_us.py --write-payload pilot/
	uv run python scripts/pilot_ksa.py --write-payload pilot/
	uv run python scripts/pilot_ksa.py --time-barred --write-payload pilot/

# The US corpus expects a packaged CBP 7551; the KSA time-barred corpus expects a refusal
# under Art. 174. Both assertions are in the script — a pilot that only prints is a demo.
pilot-run:
	uv run python scripts/pilot_run.py --all

clean:
	docker compose down -v
