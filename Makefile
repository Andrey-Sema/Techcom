.PHONY: bootstrap quality test test-fast test-integration eval eval-test mock demo db-up db-down

bootstrap:
	uv sync --locked --all-packages

quality:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy
	uv run --locked bandit -c pyproject.toml -r src mocks/src migrations scripts
	uv run --locked bandit -r tests/eval --exclude 'tests/eval/test_*.py'
	uv run --locked lint-imports

test:
	uv run --locked pytest

# Coverage is enforced by the complete suite; this subset cannot cover PG.
test-fast:
	uv run --locked pytest -m "not integration and not live_llm" --no-cov

test-integration:
	uv run --locked pytest -m integration --no-cov

eval:
	PYTHONPATH=tests uv run --locked python -m eval

eval-test:
	PYTHONPATH=tests COVERAGE_FILE=.coverage.eval uv run --locked pytest -o addopts='' tests/eval --strict-markers --strict-config --cov=eval --cov-branch --cov-fail-under=100

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
