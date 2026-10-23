SHELL := /bin/sh
.SHELLFLAGS := -eu -c
.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

.PHONY: help db-secrets compose-check bootstrap quality gates semgrep semgrep-test complexity deadcode \
        deps audit spelling yaml workflows pyproject-check docs \
        test test-fast test-integration eval eval-test mock demo db-up db-down db-init \
        obs-secrets up-obs down-obs obs-check obs-smoke \
        images dockerfile-lint image-scan image-test mutation sbom \
        secrets-scan secrets-staged secrets-repo-check bench demo-check db-backup db-restore zap

GATES = uv run --locked --group gates
ZAP_IMAGE ?= ghcr.io/zaproxy/zaproxy:stable@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6
DAST_APP_IMAGE ?= $(IMAGE)
export ZAP_IMAGE DAST_APP_IMAGE
# Semgrep conflicts with the project's dependency set, so it runs in an
# isolated, version-pinned tool environment.
SEMGREP = uvx --python 3.13 semgrep@1.182.1

help:
	@printf '%s\n' \
	  'make demo-check       Offline workflow and committed sample check' \
	  'make test-fast        Offline tests; no Docker or API key' \
	  'make test             Full suite with PostgreSQL and coverage' \
	  'make quality          Types, lint, security and architecture' \
	  'make gates            Additional quality and dependency gates' \
	  'make db-init          Local database, migrations and service role' \
	  'make image-test       Build and exercise both container images' \
	  'make obs-smoke        Validate and exercise the monitoring stack' \
	  'make bootstrap        Install developer tools and Git hooks'

bootstrap:
	uv sync --locked --all-packages --group gates --group docs
	uv run --locked pre-commit install

# Opt-in DAST targets only the isolated synthetic service, never production.
zap:
	uv run --locked python scripts/prepare_dast.py --check-image
	docker pull "$(ZAP_IMAGE)"
	uv run --locked python scripts/prepare_dast.py
	@trap 'docker compose --env-file var/dast/scan.env -f compose.dast.yaml down; chmod 600 var/dast/*' EXIT HUP INT TERM; \
	  docker compose --env-file var/dast/scan.env -f compose.dast.yaml run --rm zap

quality:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy
	uv run --locked bandit -c pyproject.toml -r src mocks/src migrations scripts
	uv run --locked bandit -r tests/eval --exclude 'tests/eval/test_*.py'
	uv run --locked lint-imports

# Tier B gates: project invariants, complexity, dead code,
# dependency hygiene and vulnerabilities, spelling, YAML, workflows.
gates: semgrep-test semgrep complexity deadcode deps audit spelling yaml \
       workflows pyproject-check docs dockerfile-lint

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
	uv run --locked pytest --no-cov \
	  -m "not integration and not live_llm and not image and not bench"

test-integration:
	uv run --locked pytest -m integration --no-cov

eval:
	PYTHONPATH=tests uv run --locked python -m eval

eval-test:
	PYTHONPATH=tests COVERAGE_FILE=.coverage.eval uv run --locked pytest -o addopts='' tests/eval --strict-markers --strict-config --cov=eval --cov-branch --cov-fail-under=100

# 10,000-task benchmark against PostgreSQL 18 (docs/benchmarks.md).
bench:
	uv run --locked pytest -m bench --no-cov -p no:randomly tests/bench

# Mutation testing of the decision core (docs/06 s. 6); nightly in CI.
# Scope and test selection live in [tool.mutmut] in pyproject.toml.
mutation:
	rm -rf mutants
	uv run --locked --group mutation mutmut run --max-children 4
	uv run --locked --group mutation mutmut export-cicd-stats
	uv run --locked python scripts/mutation_gate.py

mock:
	mkdir -p var
	FOLLOWUP_MOCK_JOURNAL=var/delivery_log.jsonl uv run --locked uvicorn techcom_mocks.app:create_app --factory --host 127.0.0.1 --port 8081 --workers 1

demo:
	uv run --locked python scripts/demo.py

# CI smoke: a fresh demo must match samples/ apart from identifiers.
demo-check: demo
	uv run --locked python scripts/demo_check.py

# Only this project's generated secrets; never world-readable.
db-secrets:
	mkdir -p secrets
	chmod 700 secrets
	umask 077; test -s secrets/postgres_password || $(SECRET) > secrets/postgres_password
	umask 077; test -s secrets/service_password || $(SECRET) > secrets/service_password
	chmod 600 secrets/postgres_password secrets/service_password

db-up: db-secrets
	docker compose up -d --wait db

db-down:
	docker compose down

# Validate all deployment combinations with synthetic settings only.
compose-check:
	docker compose config --quiet
	FOLLOWUP_API_ISSUER=https://issuer.example.invalid \
	  FOLLOWUP_API_AUDIENCE=followup \
	  FOLLOWUP_TASK_API_URL=https://tasks.example.invalid \
	  FOLLOWUP_DELIVERY_API_URL=https://delivery.example.invalid \
	  FOLLOWUP_ALLOWED_HOSTS='["tasks.example.invalid","delivery.example.invalid"]' \
	  docker compose -f compose.yaml -f compose.observability.yaml \
	    -f compose.api.yaml config --quiet
	DAST_UID=1000 DAST_GID=1000 docker compose -f compose.dast.yaml config --quiet

# Local PostgreSQL for repeated runs: generated secrets, the schema
# (as owner) and a least-privilege login role for the service.
SECRET = uv run --locked python -c "import secrets; print(secrets.token_urlsafe(24), end='')"
db-init: db-secrets
	docker compose up -d --wait db
	FOLLOWUP_DATABASE_URL="postgresql+psycopg://followup:$$(cat secrets/postgres_password)@127.0.0.1:5432/followup" \
	  uv run --locked alembic upgrade head
	docker compose exec -T db psql -v ON_ERROR_STOP=1 -U followup -d followup \
	  -v "service_password=$$(cat secrets/service_password)" \
	  < scripts/db_service_role.sql

# Backup and restore of the compose database (docs/18). Dumps contain
# personal data: owner-only files under var/backups (git-ignored).
DB = docker compose exec -T db
db-backup:
	mkdir -p var/backups
	chmod 700 var/backups
	umask 077 && $(DB) pg_dump -U followup --format=custom followup \
	  > var/backups/followup-$$(date -u +%Y%m%dT%H%M%SZ).dump

db-restore:
	@test -n "$(FILE)" || { echo "usage: make db-restore FILE=var/backups/..."; exit 2; }
	$(DB) pg_restore -U followup --dbname=followup --clean --if-exists \
	  --exit-on-error --single-transaction < "$(FILE)"

# Observability stack (ADR-0010). Validators run inside the same
# pinned images as the stack, taken from compose.observability.yaml.
OBS = docker compose -f compose.observability.yaml

obs-secrets:
	mkdir -p secrets
	test -s secrets/grafana_admin_user || printf 'admin' > secrets/grafana_admin_user
	umask 077; test -s secrets/grafana_admin_password || \
	  uv run --locked python -c "import secrets; print(secrets.token_urlsafe(24), end='')" \
	  > secrets/grafana_admin_password
	# The directory keeps other host users out. The files must be readable
	# by the Grafana user (uid 472): an unreadable secret makes Grafana
	# fall back to admin/admin, which obs-smoke checks for.
	chmod 700 secrets
	chmod 644 secrets/grafana_admin_user secrets/grafana_admin_password

up-obs: obs-secrets
	$(OBS) up -d --wait prometheus grafana
	$(OBS) up -d loki alloy

down-obs:
	$(OBS) down

obs-check: obs-secrets
	$(OBS) config --quiet
	$(OBS) run --rm --no-deps --entrypoint promtool prometheus \
	  check config /etc/prometheus/prometheus.yml
	$(OBS) run --rm --no-deps --entrypoint promtool -w /etc/prometheus/rules \
	  prometheus test rules followup.rules.test.yml
	$(OBS) run --rm --no-deps --entrypoint /usr/bin/loki loki \
	  -config.file=/etc/loki/loki.yml -verify-config
	$(OBS) run --rm --no-deps alloy fmt --test /etc/alloy/config.alloy
	$(OBS) run --rm --no-deps alloy validate /etc/alloy/config.alloy

obs-smoke: up-obs
	uv run --locked python scripts/obs_smoke.py

# Container images. Scanners run from images pinned by digest; Trivy
# releases 0.69.4-0.69.6 were compromised, so never use a floating tag.
# Behind a TLS-intercepting proxy pass, for example,
#   DOCKER_BUILD_FLAGS="--secret id=ca_bundle,src=/path/to/ca.crt"
#   DOCKER_RUN_FLAGS="-e HTTPS_PROXY -e SSL_CERT_FILE=/ca.crt
#     -v /path/to/ca.crt:/ca.crt:ro"
IMAGE ?= techcom-followup:dev
MOCK_IMAGE ?= techcom-mock:dev
DOCKER_BUILD_FLAGS ?=
DOCKER_RUN_FLAGS ?=
HADOLINT_IMAGE = hadolint/hadolint:v2.15.1@sha256:32dac94127fd60b7b7e3fbfc65e1383b9b5e25c9bfd7b8536de7a539fe68a12d
TRIVY_IMAGE = aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa
TRIVY = docker run --rm $(DOCKER_RUN_FLAGS) \
	  -v "$(CURDIR)/Dockerfile:/scan/Dockerfile:ro" \
	  -v "$(CURDIR)/var/images:/scan/images:ro" \
	  -v "$(CURDIR)/var/sbom:/scan/sbom" \
	  -v "$(CURDIR)/var/trivy-cache:/root/.cache/trivy" $(TRIVY_IMAGE)
TRIVY_IMAGE_SCAN = image --exit-code 1 --severity HIGH,CRITICAL \
	  --ignore-unfixed --scanners vuln,secret

images:
	docker build $(DOCKER_BUILD_FLAGS) --target runtime -t $(IMAGE) .
	docker build $(DOCKER_BUILD_FLAGS) --target mock -t $(MOCK_IMAGE) .

dockerfile-lint:
	docker run --rm -i \
	  -v "$(CURDIR)/.hadolint.yaml:/.config/hadolint.yaml:ro" \
	  $(HADOLINT_IMAGE) hadolint --config /.config/hadolint.yaml - < Dockerfile

image-scan: images
	mkdir -p var/images var/trivy-cache var/sbom
	docker save $(IMAGE) -o var/images/runtime.tar
	docker save $(MOCK_IMAGE) -o var/images/mock.tar
	$(TRIVY) config --exit-code 1 --severity MEDIUM,HIGH,CRITICAL \
	  /scan/Dockerfile
	$(TRIVY) $(TRIVY_IMAGE_SCAN) --input /scan/images/runtime.tar
	$(TRIVY) $(TRIVY_IMAGE_SCAN) --input /scan/images/mock.tar
	$(TRIVY) image --format cyclonedx --output /scan/sbom/runtime-image.cdx.json \
	  --input /scan/images/runtime.tar

image-test: images
	FOLLOWUP_TEST_IMAGE=$(IMAGE) FOLLOWUP_TEST_MOCK_IMAGE=$(MOCK_IMAGE) \
	  uv run --locked pytest -m image --no-cov -p no:randomly tests/image

# Supply chain. CycloneDX SBOM of the locked runtime dependency set;
# image-scan adds the SBOM of the runtime image. CI keeps both as build
# artefacts (ASVS 15.1.2).
sbom:
	mkdir -p var/sbom
	uv export --locked --no-dev --package techcom-followup \
	  --format cyclonedx1.5 -q -o var/sbom/techcom-followup.cdx.json

# Secret scanning with gitleaks, pinned by digest: the whole history in
# CI, staged changes in the pre-commit hook.
GITLEAKS_IMAGE = zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
GIT_COMMON_DIR = $(shell git rev-parse --path-format=absolute --git-common-dir)
# Worktree .git files point outside the checkout; mount their metadata too.
GITLEAKS_RUN = docker run --rm -v "$(CURDIR):/repo:ro" \
	-v "$(GIT_COMMON_DIR):$(GIT_COMMON_DIR):ro" \
	-e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory \
	-e GIT_CONFIG_VALUE_0=/repo
GITLEAKS = $(GITLEAKS_RUN) $(GITLEAKS_IMAGE)

# Gitleaks can return success after Git failed and scanned zero bytes.
# Require a valid repository independently before accepting that result.
secrets-repo-check:
	$(GITLEAKS_RUN) --entrypoint git $(GITLEAKS_IMAGE) \
	  -C /repo rev-parse --verify HEAD

secrets-scan: secrets-repo-check
	$(GITLEAKS) git /repo --redact --no-banner --exit-code 1

secrets-staged: secrets-repo-check
	$(GITLEAKS) git /repo --pre-commit --staged --redact --no-banner \
	  --exit-code 1
