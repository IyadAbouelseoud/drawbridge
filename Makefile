.PHONY: help up down logs ps build lint fmt type test check clean

help:
	@echo "up      - bring the stack up"
	@echo "down    - tear the stack down"
	@echo "logs    - follow all logs"
	@echo "lint    - ruff check"
	@echo "fmt     - ruff format + fix"
	@echo "type    - mypy --strict"
	@echo "test    - pytest"
	@echo "check   - lint + type + test"

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
	uv run mypy services mcp packages/schemas/src

test:
	uv run pytest

check: lint type test

clean:
	docker compose down -v
