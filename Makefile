.PHONY: bootstrap quality test test-fast test-integration mock demo db-up db-down

bootstrap:
	uv sync --locked --all-packages

quality:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy
	uv run --locked bandit -c pyproject.toml -r src mocks/src migrations scripts
	uv run --locked lint-imports

test:
	uv run --locked pytest

# Coverage is enforced by the complete suite; this subset cannot cover PG.
test-fast:
	uv run --locked pytest -m "not integration and not live_llm" --no-cov

test-integration:
	uv run --locked pytest -m integration --no-cov

mock:
	mkdir -p var
	FOLLOWUP_MOCK_JOURNAL=var/delivery_log.jsonl uv run --locked uvicorn techcom_mocks.app:create_app --factory --host 127.0.0.1 --port 8081 --workers 1

demo:
	uv run --locked python scripts/demo.py

# Create secrets/postgres_password locally before starting the database.
db-up:
	docker compose up -d --wait db

db-down:
	docker compose down
