.PHONY: bootstrap quality gates semgrep semgrep-test complexity deadcode \
        deps audit spelling yaml workflows pyproject-check docs \
        test test-fast test-integration mock db-up db-down \
        obs-secrets up-obs down-obs obs-check obs-smoke \
        images dockerfile-lint image-scan image-test mutation sbom \
        secrets-scan secrets-staged

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
	uv run --locked pytest -m "not integration and not live_llm" --no-cov

test-integration:
	uv run --locked pytest -m integration --no-cov

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

# Create secrets/postgres_password locally before starting the database.
db-up:
	docker compose up -d --wait db

db-down:
	docker compose down

# Observability stack (M10, ADR-0010). Validators run inside the same
# pinned images as the stack, taken from compose.observability.yaml.
OBS = docker compose -f compose.observability.yaml

obs-secrets:
	mkdir -p secrets
	test -s secrets/grafana_admin_user || printf 'admin' > secrets/grafana_admin_user
	test -s secrets/grafana_admin_password || \
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

# Container images (M8). Scanners run from images pinned by digest; Trivy
# releases 0.69.4-0.69.6 were compromised, so never use a floating tag.
# Behind a TLS-intercepting proxy pass, for example,
#   DOCKER_BUILD_FLAGS="--network host --build-arg HTTPS_PROXY
#     --secret id=ca_bundle,src=/path/to/ca.crt"
#   DOCKER_RUN_FLAGS="--network host -e HTTPS_PROXY -e SSL_CERT_FILE=/ca.crt
#     -v /path/to/ca.crt:/ca.crt:ro"
IMAGE ?= techcom-followup:dev
MOCK_IMAGE ?= techcom-mock:dev
DOCKER_BUILD_FLAGS ?=
DOCKER_RUN_FLAGS ?=
HADOLINT_IMAGE = hadolint/hadolint:v2.15.1@sha256:32dac94127fd60b7b7e3fbfc65e1383b9b5e25c9bfd7b8536de7a539fe68a12d
TRIVY_IMAGE = aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa
TRIVY = docker run --rm $(DOCKER_RUN_FLAGS) \
	  -v $(CURDIR)/Dockerfile:/scan/Dockerfile:ro \
	  -v $(CURDIR)/var/images:/scan/images:ro \
	  -v $(CURDIR)/var/sbom:/scan/sbom \
	  -v $(CURDIR)/var/trivy-cache:/root/.cache/trivy $(TRIVY_IMAGE)
TRIVY_IMAGE_SCAN = image --exit-code 1 --severity HIGH,CRITICAL \
	  --ignore-unfixed --scanners vuln,secret

images:
	docker build $(DOCKER_BUILD_FLAGS) --target runtime -t $(IMAGE) .
	docker build $(DOCKER_BUILD_FLAGS) --target mock -t $(MOCK_IMAGE) .

dockerfile-lint:
	docker run --rm -i \
	  -v $(CURDIR)/.hadolint.yaml:/.config/hadolint.yaml:ro \
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

# Supply chain (M9). CycloneDX SBOM of the locked runtime dependency set;
# image-scan adds the SBOM of the runtime image. CI keeps both as build
# artefacts (ASVS 15.1.2).
sbom:
	mkdir -p var/sbom
	uv export --locked --no-dev --package techcom-followup \
	  --format cyclonedx1.5 -q -o var/sbom/techcom-followup.cdx.json

# Secret scanning with gitleaks, pinned by digest: the whole history in
# CI, staged changes in the pre-commit hook.
GITLEAKS_IMAGE = zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
GITLEAKS = docker run --rm -v $(CURDIR):/repo:ro $(GITLEAKS_IMAGE)

secrets-scan:
	$(GITLEAKS) git /repo --redact --no-banner --exit-code 1

secrets-staged:
	$(GITLEAKS) git /repo --pre-commit --staged --redact --no-banner \
	  --exit-code 1
