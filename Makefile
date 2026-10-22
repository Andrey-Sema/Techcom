.PHONY: bootstrap quality gates semgrep semgrep-test complexity deadcode \
        deps audit spelling yaml workflows pyproject-check docs \
        test test-fast test-integration mock db-up db-down

GATES = uv run --locked --group gates
# Semgrep conflicts with the project's dependency set, so it runs in an
# isolated, version-pinned tool environment.
SEMGREP = uvx --python 3.13 semgrep@1.182.1

bootstrap:
	uv sync --locked --all-packages --group gates --group docs
	uv run --locked pre-commit install

quality:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy
	uv run --locked bandit -c pyproject.toml -r src mocks/src
	uv run --locked lint-imports

# Tier B gates (M0b): project invariants, complexity, dead code,
# dependency hygiene and vulnerabilities, spelling, YAML, workflows.
gates: semgrep-test semgrep complexity deadcode deps audit spelling yaml \
       workflows pyproject-check docs

semgrep:
	$(SEMGREP) scan --error --metrics=off --quiet \
	  --config .semgrep/project-invariants.yml src mocks/src

semgrep-test:
	$(SEMGREP) --test --metrics=off \
	  --config .semgrep/project-invariants.yml .semgrep/project-invariants.py

complexity:
	$(GATES) xenon --max-absolute B --max-modules A --max-average A \
	  src mocks/src

deadcode:
	$(GATES) vulture

deps:
	$(GATES) deptry .
	$(GATES) deptry mocks --config mocks/pyproject.toml

audit:
	mkdir -p var
	uv export --locked --no-dev --no-emit-project --no-emit-workspace \
	  --format requirements-txt -o var/requirements.txt -q
	$(GATES) pip-audit --strict --require-hashes --disable-pip \
	  -r var/requirements.txt

spelling:
	$(GATES) codespell

yaml:
	$(GATES) yamllint --strict .

workflows:
	$(GATES) zizmor --offline .github/workflows
	$(GATES) check-jsonschema --builtin-schema vendor.github-workflows \
	  .github/workflows/*.yaml
	$(GATES) check-jsonschema --builtin-schema vendor.dependabot \
	  .github/dependabot.yml

pyproject-check:
	$(GATES) validate-pyproject pyproject.toml mocks/pyproject.toml

docs:
	uv run --locked --group docs mkdocs build --strict --quiet

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

# Create secrets/postgres_password locally before starting the database.
db-up:
	docker compose up -d --wait db

db-down:
	docker compose down
