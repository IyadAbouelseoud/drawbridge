.PHONY: help up down logs ps build lint fmt type test check clean rls-bootstrap \
	token token-service pilot-seed pilot-run secrets-init secrets-show secrets-check \
	secrets-push vault-up vault-agent-up identity-up cross-ingest onprem-config \
	pin-images pin-check reembed backup backup-verify n8n-import images

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
	@echo "secrets-push  - promote the local file into the configured manager"
	@echo "vault-up      - dev-mode Vault, then configure it and verify the read path"
	@echo "vault-agent-up- the infra path + AppRole vault-agent renders from"
	@echo "reembed       - rewrite vectors whose stamp is not the current convention"
	@echo "backup        - one pg_dump into object-locked storage"
	@echo "backup-verify - recompute every tenant's ledger hash chain"
	@echo "pin-check     - fail if any on-prem image drifted from its digest"
	@echo "n8n-import    - import the workflows, activate them, restart n8n"
	@echo "images        - build and tag the seven images the on-prem stack names"
	@echo "identity-up   - Authentik, then provider + application + one RS256 round trip"
	@echo "cross-ingest  - fetch a CROSS ruling sample, load it and embed it"
	@echo "onprem-config - render and validate docker-compose.onprem.yml"

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
	uv run mypy services mcp_servers packages/schemas/src scripts infra

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

secrets-push:
	@uv run python scripts/manage_secrets.py push

# Dev-mode Vault, then the mount, the read-only policy, the AppRole and the secrets. The
# script verifies by reading them back through the provider the API itself uses, which is
# the only verification worth printing.
vault-up:
	docker compose --profile secrets up -d vault
	VAULT_ADDR=http://localhost:8200 VAULT_TOKEN=$${VAULT_DEV_ROOT_TOKEN_ID:-drawbridge-dev-root} \
		uv run python infra/vault_bootstrap.py

# Authentik, then the OIDC provider, application, tenant property mapping and a service
# account — ending in one real RS256 token verified through services/api/src/auth.py.
# TENANT is the tenant the round-trip service account acts for.
#
# The authentik database is created by infra/postgres/init.sql, which only runs when the
# Postgres volume is first initialised. On a cluster that predates it, this creates it.
identity-up:
	@test -n "$(TENANT)" || (echo 'usage: make identity-up TENANT=<uuid>' && exit 2)
	docker compose exec -T postgres psql -U drawbridge -d postgres -tAc \
		"SELECT 1 FROM pg_database WHERE datname='authentik'" | grep -q 1 || \
		docker compose exec -T postgres psql -U drawbridge -d postgres -c \
		"CREATE DATABASE authentik OWNER drawbridge"
	set -a; . <(uv run python scripts/manage_secrets.py env); set +a; \
		docker compose --profile identity up -d authentik-server authentik-worker
	uv run python infra/authentik_bootstrap.py --tenant $(TENANT) --api-url http://localhost:8000

# Build every image `docker-compose.onprem.yml` names, tagged with DRAWBRIDGE_VERSION.
#
# The deployment file has no `build:` sections on purpose — what is deployed should be an
# image with a digest rather than whatever a host happened to compile — but until week 15
# that left it naming seven images and nothing in the repository producing them. A
# reproducible deploy needs both halves.
#
#   make images VERSION=1.2.3
images:
	@test -n "$(VERSION)" || (echo 'usage: make images VERSION=<tag>' && exit 2)
	docker build -f services/api/Dockerfile -t drawbridge/api:$(VERSION) .
	docker build -f services/backup/Dockerfile -t drawbridge/backup:$(VERSION) .
	for s in ace hts docs claims ledger; do 		docker build -f mcp_servers/mcp_$$s/Dockerfile -t drawbridge/mcp-$$s:$(VERSION) . ; 	done
	@echo "built 7 images at $(VERSION); set DRAWBRIDGE_VERSION=$(VERSION) in .env.onprem"

# The credentials Postgres, MinIO and n8n read, and the role vault-agent uses to render
# them. A separate path and role from the API's on purpose — see secrets/README.md.
# Re-running is safe: it generates only what is missing and never replaces a value in
# force, because rotating postgres_password or n8n_encryption_key breaks things that a
# bootstrap command must not break by accident.
vault-agent-up:
	docker compose --profile secrets up -d vault
	VAULT_ADDR=http://localhost:8200 VAULT_TOKEN=$${VAULT_DEV_ROOT_TOKEN_ID:-drawbridge-dev-root} \
		uv run python infra/vault_bootstrap.py --infra

# Rewrite every vector whose embedding_model_id is not the current backend and text
# convention. Overwrites in place rather than nulling first, so search stays up while the
# corpus converges. Idempotent: a second run over a converged corpus does nothing.
reembed:
	uv run python scripts/embed_corpus.py --reembed
	uv run python scripts/embed_corpus.py --reembed --table rulings

# One dump into the object-locked bucket. COMPLIANCE mode means this cannot be undone by
# the operator, which is the point; see scripts/retention.py before pointing it anywhere
# you would mind keeping for five years.
backup:
	uv run python scripts/retention.py backup

backup-verify:
	uv run python scripts/retention.py verify
	uv run python scripts/retention.py catalogue

# Resolve every third-party on-prem image against the registry. `pin-check` is the CI
# form: it writes nothing and fails on drift, so a tag that moved under an unchanged
# version number is something a person sees rather than something a deploy inherits.
pin-images:
	uv run python infra/pin_images.py

pin-check:
	uv run python infra/pin_images.py --check

# Import, activate, restart. All three steps, because `n8n import:workflow` deactivates
# what it imports and n8n reads neither the workflows nor the activation until it
# restarts — which is how three un-importable files sat in the repo for six weeks.
n8n-import:
	docker compose exec -T n8n n8n import:workflow --separate --input=/workflows
	for id in drawbridgeClaim1 drawbridgeError1 drawbridgeHitl1; do \
		docker compose exec -T n8n n8n update:workflow --id=$$id --active=true >/dev/null; \
	done
	docker compose restart n8n
	@echo 'workflows imported and active'

# A term-drawn sample, not the corpus. CBP publishes no bulk export; see the script.
cross-ingest:
	uv run python -m scripts.ingest_cross fetch --limit $${LIMIT:-120} --load
	uv run python scripts/embed_corpus.py --table rulings

# Renders the on-prem stack with placeholder values and validates it. Catches a broken
# anchor or a missing required variable without needing a deployment to try it on.
onprem-config:
	DRAWBRIDGE_VERSION=0.0.0-check VAULT_ADDR=http://vault:8200 \
	DRAWBRIDGE_VAULT_ROLE_ID=check DRAWBRIDGE_VAULT_SECRET_ID=check \
	DRAWBRIDGE_OIDC_JWKS_URL=http://check/jwks DRAWBRIDGE_JWT_ISSUER=http://check/ \
	DRAWBRIDGE_PREPARER_NAME=check DRAWBRIDGE_PUBLIC_HOST=check.example \
	DRAWBRIDGE_VAULT_AGENT_ROLE_ID=check DRAWBRIDGE_VAULT_AGENT_SECRET_ID=check \
		docker compose -f docker-compose.onprem.yml config -q && echo 'onprem stack is valid'

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
