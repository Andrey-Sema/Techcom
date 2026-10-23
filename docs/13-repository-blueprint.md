# 13 — Repository Blueprint (files and their exact shape)

This is the target content of every infrastructure and tooling file. Milestone
M0 materialises them verbatim; deviations need an ADR. Image tags and tool
versions marked `<pin>` are resolved **once** at bootstrap (latest stable,
never `latest`/`rc`), pinned by digest for images, and updated by Dependabot.

## 1. Repository tree (infrastructure view)

```text
.
├── .github/
│   ├── workflows/ci.yml            # lint, type, test, security, image, smoke
│   ├── dependabot.yml              # pip (uv), docker, github-actions
│   └── CODEOWNERS
├── .gitignore
├── .dockerignore                   # allow-list
├── .editorconfig
├── .env.example                    # no credentials
├── .pre-commit-config.yaml
├── .python-version                 # 3.14 (local dev default)
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── Dockerfile
├── compose.yaml                    # core services
├── compose.observability.yaml      # Prometheus, Grafana, Loki, Alloy
├── Makefile
├── SECURITY.md                     # disclosure policy, supported versions
├── config/
│   ├── policy.toml
│   └── routing.toml
├── deploy/
│   ├── postgres/initdb/01-roles.sql
│   ├── prometheus/prometheus.yml
│   ├── prometheus/rules/followup.rules.yml
│   ├── grafana/provisioning/datasources/datasources.yml
│   ├── grafana/provisioning/dashboards/dashboards.yml
│   ├── grafana/dashboards/followup-overview.json
│   ├── loki/loki.yml
│   └── alloy/config.alloy
├── secrets/                        # git-ignored; created by `make secrets`
├── migrations/
├── src/techcom_followup/          # the product (only this ships to prod)
├── mocks/                          # uv workspace member, test/demo only
│   ├── pyproject.toml              # name = "techcom-mocks"
│   └── src/techcom_mocks/
├── tests/ …  docs/ …  samples/ …  fixtures/ …
```

The mock API is a separate **uv workspace member** so that the production
image provably contains no test doubles (ASVS 15.2.3); the mock image is a
separate Dockerfile target.

## 2. `.gitignore`

Superseded by `17-tooling-and-repo-hygiene.md` §3 (expanded version).

## 3. `.dockerignore` (allow-list: ASVS 13.4.1, 15.2.3)

Deny everything, then allow only what the image needs. `.git`, tests,
docs, secrets, caches and samples can never leak into the image.

```dockerignore
*
!pyproject.toml
!uv.lock
!README.md
!alembic.ini
!src/
!mocks/
!migrations/
!config/
!fixtures/
**/__pycache__
**/*.py[cod]
```

## 4. `pyproject.toml`

> The `[dependency-groups]`, `ruff`, `mypy` sections below are superseded by
> `17` §2 (Ruff `ALL`, Bandit, vulture, deptry, codespell, commitizen).
> Project metadata, pytest, coverage and import-linter stay as here.

```toml
[project]
name = "techcom-followup"
version = "0.1.0"
description = "AI operations & follow-up service with safe, idempotent actions."
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
    # Lower bounds only; exact versions are frozen in uv.lock.
    "alembic>=1.13",
    "fastapi>=0.115",
    "opentelemetry-exporter-otlp-proto-http>=1.28",
    "opentelemetry-sdk>=1.28",
    "openai>=3.36,<4",
    "httpx>=0.28",
    "prometheus-client>=0.21",
    "psycopg[binary,pool]>=3.2",
    "pydantic>=2.10",
    "pydantic-settings>=2.6",
    "pyjwt[crypto]>=2.10",
    "sqlalchemy[asyncio]>=2.0.36,<2.1",
    "stamina>=24.3",
    "structlog>=24.4",
    "typer>=0.15",
    "uvicorn>=0.32",
]

[project.scripts]
followup = "techcom_followup.adapters.inbound.cli:app"

[dependency-groups]
dev = [
    "hypothesis>=6.120",
    "import-linter>=2.1",
    "mutmut>=3.2",
    "mypy>=1.13",
    "pip-audit>=2.7",
    "pre-commit>=4.0",
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-cov>=6.0",
    "pytest-randomly>=3.16",
    "pytest-xdist>=3.6",
    "respx>=0.21",
    "ruff>=0.8",
    "syrupy>=4.8",
    "techcom-mocks",
    "testcontainers[postgres]>=4.8",
]

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/techcom_followup"]

[tool.uv]
default-groups = ["dev"]

[tool.uv.workspace]
members = ["mocks"]

[tool.uv.sources]
techcom-mocks = { workspace = true }

# ---------------------------------------------------------------- ruff --
[tool.ruff]
line-length = 79                 # strict PEP 8
target-version = "py313"
src = ["src", "tests"]

[tool.ruff.lint]
select = [
    "E", "W", "F", "I", "N", "D", "UP", "B", "S", "A", "C4", "DTZ",
    "T20", "RET", "SIM", "PTH", "ERA", "PL", "RUF", "ASYNC", "FBT",
    "TRY", "BLE", "G", "LOG", "PT", "TID", "ARG", "PERF", "FURB",
]
ignore = ["COM812", "ISC001"]    # conflict with the formatter

[tool.ruff.lint.pycodestyle]
max-doc-length = 72              # PEP 8: comments/docstrings 72

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.flake8-tidy-imports]
ban-relative-imports = "all"

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "D", "PLR2004", "ARG", "FBT"]
"migrations/**" = ["D", "ERA"]

[tool.ruff.format]
docstring-code-format = true

# ---------------------------------------------------------------- mypy --
[tool.mypy]
python_version = "3.13"
strict = true
warn_unreachable = true
enable_error_code = ["ignore-without-code", "redundant-expr", "truthy-bool"]
plugins = ["pydantic.mypy"]
files = ["src", "mocks/src", "tests"]

[tool.pydantic-mypy]
init_forbid_extra = true
init_typed = true
warn_required_dynamic_aliases = true

# -------------------------------------------------------------- pytest --
[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
addopts = [
    "-ra", "--strict-markers", "--strict-config",
    "--cov", "--cov-branch", "--cov-report=term-missing",
    "-m", "not live_llm",
]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
xfail_strict = true
filterwarnings = ["error"]
markers = [
    "integration: requires Docker (testcontainers)",
    "live_llm: calls the real LLM provider (opt-in)",
]

# ------------------------------------------------------------ coverage --
[tool.coverage.run]
branch = true
source = ["techcom_followup", "techcom_mocks"]   # mocks are tested too

[tool.coverage.report]
fail_under = 100
show_missing = true
skip_covered = true
exclude_also = [
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
    "@overload",
]

# -------------------------------------------------------- import-linter --
[tool.importlinter]
root_packages = ["techcom_followup"]

[[tool.importlinter.contracts]]
name = "Hexagonal layers"
type = "layers"
layers = [
    "techcom_followup.bootstrap",          # composition root only
    "techcom_followup.adapters",
    "techcom_followup.infrastructure",     # settings, resilience, logging
    "techcom_followup.application",
    "techcom_followup.domain",
]
```

Notes: `sqlalchemy<2.1` keeps us off the 2.1 beta line; `pyjwt[crypto]` is
for short-lived service tokens (see `15-security-asvs-l2.md`);
OpenTelemetry ships logs over OTLP (see §10). `mocks/pyproject.toml`
declares `techcom-mocks` with `fastapi`, `uvicorn` and the script
`followup-mock = "techcom_mocks.app:main"`; it is a dev dependency only.

## 5. `Dockerfile` (multi-stage, uv, non-root, minimal)

```dockerfile
# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:<pin>

# ---------- builder: resolve and install locked dependencies ----------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:<pin> /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first: this layer is cached until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=mocks/pyproject.toml,target=mocks/pyproject.toml \
    uv sync --locked --no-dev --no-install-workspace
COPY . .
# Install ONLY the product package: no mocks, no dev tools (ASVS 15.2.3).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package techcom-followup

# ---------- runtime: no compiler, no uv, no source tree ----------
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app \
        --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY config /app/config
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONBREAKPOINT=0
WORKDIR /app
USER 10001:10001
EXPOSE 8080 9464
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["followup"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]

# ---------- mock: demo/test only, never deployed ----------
FROM builder AS mock-builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package techcom-mocks

FROM ${PYTHON_IMAGE} AS mock
RUN useradd --system --uid 10002 --no-create-home \
        --shell /usr/sbin/nologin mock
COPY --from=mock-builder /opt/venv /opt/venv
COPY fixtures /app/fixtures
ENV PATH="/opt/venv/bin:${PATH}" PYTHONUNBUFFERED=1
WORKDIR /app
USER 10002
ENTRYPOINT ["followup-mock"]
```

`docker build --target runtime` is the default and the only image CI
publishes; `--target mock` is built for compose and the smoke job only. The
dependency layer uses `--no-install-workspace` so it stays cached until
`uv.lock` changes. `uv sync --package` installs one workspace member with
its locked dependencies.

Rules: files are owned by root and read-only for UID 10001; the container
runs with `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`
(compose). Port 8080 = API, 9464 = metrics (internal network only).
Trivy scan in CI fails on HIGH/CRITICAL with a fix available.

**As built (M8).** The committed `Dockerfile` follows this sketch with
these differences, each for a reason found while building it:

* `runtime` is the **last** stage, so a plain `docker build` produces it.
* uv comes from `astral/uv` on Docker Hub (same Astral release, pinned
  by digest); the first Trivy run also showed that the base image's
  `pip` vendors vulnerable `urllib3`/`msgpack`, so both images uninstall
  `pip` (`tests/image` asserts it is gone).
* An optional build secret `ca_bundle` supports TLS-intercepting
  proxies without writing the CA into a layer.
* The mock runs `uvicorn techcom_mocks.app:create_app --factory` with
  its journal on a `/var/lib/mock` volume, so it also works read-only.
* `ENTRYPOINT ["followup"]` with `CMD ["--help"]`: the CLI runs one
  command per container; migrations use `--entrypoint alembic`. The
  `HEALTHCHECK` belongs to the long-running API (M6b). An image test runs
  migrate → `run --dispatch` → rerun against PostgreSQL 18 and the mock.

## 6. `compose.yaml` (core)

```yaml
name: techcom

x-hardening: &hardening
  read_only: true
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  tmpfs: ["/tmp"]
  init: true                  # tini as PID 1: correct SIGTERM forwarding
  stop_grace_period: 30s      # dispatcher finishes in-flight sends
  pids_limit: 256
  mem_limit: 512m
  cpus: 1.0
  restart: unless-stopped

services:
  db:
    image: postgres:18@sha256:<pin>
    environment:
      POSTGRES_DB: followup
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_superuser_password
    secrets: [pg_superuser_password, pg_app_password, pg_migrator_password]
    volumes:
      # PostgreSQL 18 images: mount the parent dir (PGDATA=/var/lib/postgresql/18/docker)
      - pgdata:/var/lib/postgresql
      - ./deploy/postgres/initdb:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d followup"]
      interval: 5s
      timeout: 3s
      retries: 20
    networks: [backend]

  migrate:
    image: techcom-followup:local
    build: { context: ., target: runtime }
    <<: *hardening
    restart: "no"             # one-shot job: never restart after success
    entrypoint: ["alembic", "upgrade", "head"]
    environment:
      FOLLOWUP_DATABASE_URL_FILE: /run/secrets/migrator_dsn
    secrets: [migrator_dsn]
    depends_on:
      db: { condition: service_healthy }
    networks: [backend]

  mock-api:
    image: techcom-mock:local
    build: { context: ., target: mock }
    <<: *hardening
    command: ["--host", "0.0.0.0", "--port", "8081"]
    environment:
      FOLLOWUP_MOCK_FIXTURE: /app/fixtures/tasks.json
      FOLLOWUP_MOCK_FAULTS: ${FOLLOWUP_MOCK_FAULTS:-}
    volumes: ["mockdata:/var/lib/mock"]   # delivery_log.jsonl
    networks: [backend]

  app:
    image: techcom-followup:local
    <<: *hardening
    env_file: [.env]
    secrets:
      - app_dsn
      - jwt_public_key
      - source: openai_api_key
        target: followup_openai_api_key   # name read by pydantic-settings
    depends_on:
      migrate: { condition: service_completed_successfully }
      mock-api: { condition: service_started }
    ports: ["127.0.0.1:8080:8080"]        # never 0.0.0.0 on the host
    networks: [backend, observability]

networks:
  backend: { internal: true }
  observability: {}

volumes:
  pgdata: {}
  mockdata: {}

secrets:
  pg_superuser_password: { file: ./secrets/pg_superuser_password }
  pg_app_password:       { file: ./secrets/pg_app_password }
  pg_migrator_password:  { file: ./secrets/pg_migrator_password }
  app_dsn:               { file: ./secrets/app_dsn }
  migrator_dsn:          { file: ./secrets/migrator_dsn }
  jwt_public_key:        { file: ./secrets/jwt_public_key.pem }
  openai_api_key:        { file: ./secrets/openai_api_key }
```

`make secrets` generates random passwords (`secrets.token_urlsafe(32)`),
the DSNs and an Ed25519 key pair for service tokens into `secrets/`
(git-ignored, mode 0600). `deploy/postgres/initdb/01-roles.sql` creates two
least-privilege roles: `followup_migrator` (owns schema, DDL) and
`followup_app` (DML on its tables only, no DDL, no superuser) — ASVS
13.2.2.

Settings support `*_FILE` variables: `pydantic-settings` reads secrets from
`/run/secrets` (`secrets_dir`), so secrets never appear in `docker inspect`
environment output.

## 7. `compose.observability.yaml` (profile: `make up-obs`)

```yaml
services:
  prometheus:
    image: prom/prometheus:<pin>
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=15d
    volumes:
      - ./deploy/prometheus:/etc/prometheus:ro
      - promdata:/prometheus
    ports: ["127.0.0.1:9090:9090"]
    networks: [observability]

  loki:
    image: grafana/loki:<pin>
    command: ["-config.file=/etc/loki/loki.yml"]
    volumes:
      - ./deploy/loki/loki.yml:/etc/loki/loki.yml:ro
      - lokidata:/loki
    networks: [observability]

  alloy:
    image: grafana/alloy:<pin>          # Promtail is EOL since 2026-03-02
    command: ["run", "/etc/alloy/config.alloy"]
    volumes:
      - ./deploy/alloy/config.alloy:/etc/alloy/config.alloy:ro
    # No Docker socket mount (OWASP Docker Security Cheat Sheet, rule #1):
    # the app pushes logs over OTLP instead.
    networks: [observability]

  grafana:
    image: grafana/grafana:<pin>
    environment:
      GF_SECURITY_ADMIN_USER__FILE: /run/secrets/grafana_admin_user
      GF_SECURITY_ADMIN_PASSWORD__FILE: /run/secrets/grafana_admin_password
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_AUTH_ANONYMOUS_ENABLED: "false"
      GF_ANALYTICS_REPORTING_ENABLED: "false"
    secrets: [grafana_admin_user, grafana_admin_password]
    volumes:
      - ./deploy/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro
    ports: ["127.0.0.1:3000:3000"]
    networks: [observability]

volumes: { promdata: {}, lokidata: {} }
secrets:
  grafana_admin_user:     { file: ./secrets/grafana_admin_user }
  grafana_admin_password: { file: ./secrets/grafana_admin_password }
```

Log path: the app always writes JSON to stdout (`docker logs`, platform
collectors) **and**, when `FOLLOWUP_OTLP_ENDPOINT` is set, exports the same
records through the OpenTelemetry logs SDK to Alloy, which forwards them to
Loki's native OTLP endpoint. No container ever gets the Docker socket.

## 8. Prometheus

`deploy/prometheus/prometheus.yml`:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
rule_files: ["/etc/prometheus/rules/*.yml"]
scrape_configs:
  - job_name: followup-app
    metrics_path: /metrics
    static_configs: [{ targets: ["app:9464"] }]
```

`deploy/prometheus/rules/followup.rules.yml` (alerts mirror `09` §3):

```yaml
groups:
  - name: followup
    rules:
      - alert: FollowupDeadLetters
        expr: increase(followup_actions_total{outcome="dead"}[15m]) > 0
        labels: { severity: page }
        annotations:
          summary: "Action(s) moved to dead letter"
      - alert: FollowupOutboxStuck
        expr: followup_outbox_oldest_age_seconds{status=~"pending|unknown"} > 1800
        for: 5m
        labels: { severity: page }
      - alert: FollowupLLMFallbackRateHigh
        expr: |
          sum(rate(followup_llm_requests_total{result!="ok"}[15m]))
            / clamp_min(sum(rate(followup_llm_requests_total[15m])), 1e-9)
            > 0.05
        for: 15m
        labels: { severity: ticket }
      - alert: FollowupLLMCostBudget
        expr: increase(followup_llm_cost_usd_total[24h]) > 5
        labels: { severity: ticket }
      - alert: FollowupCircuitOpen
        expr: followup_circuit_state{state="open"} == 1
        for: 5m
        labels: { severity: ticket }
      - alert: FollowupInjectionSpike
        expr: increase(followup_security_events_total{kind="note_flagged"}[1h]) > 10
        labels: { severity: ticket }
      - alert: FollowupAuthFailures
        expr: increase(followup_security_events_total{kind="authn_failed"}[5m]) > 20
        labels: { severity: page }
```

Metric naming follows Prometheus conventions (`_total` counters, base units,
`_seconds` histograms); label values are bounded enums only (no task ids or
user input as labels → no cardinality explosion, no data leakage).

## 9. Grafana (provisioned as code, no click-ops)

`deploy/grafana/provisioning/datasources/datasources.yml`:

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus
    url: http://prometheus:9090
    isDefault: true
    editable: false
  - name: Loki
    type: loki
    uid: loki
    url: http://loki:3100
    editable: false
```

`deploy/grafana/provisioning/dashboards/dashboards.yml`:

```yaml
apiVersion: 1
providers:
  - name: followup
    folder: TechCom
    type: file
    allowUiUpdates: false
    options: { path: /var/lib/grafana/dashboards }
```

Dashboard `followup-overview.json` — rows and panels:

| Row | Panels |
|-----|--------|
| Runs | runs/hour, run duration p50/p95, tasks processed, rejected records |
| Decisions | decisions by kind (stacked), by rule id, duplicates suppressed |
| Delivery | outbox backlog by status, oldest pending age, attempts by result, dead letters, reconciled count, replays |
| LLM | requests by result, fallback rate, tokens in/out, cost/day vs budget, latency p95, circuit state |
| Security | auth failures, authz denials, notes flagged, rate-limited requests, validation rejections |
| Logs | Loki panel `{service_name="followup"} \| severity_text=~"(?i)warn.*\|error\|critical"`, linked by `run_id` |

## 10. Loki and Alloy

`deploy/loki/loki.yml`: single-binary mode, filesystem storage, TSDB
schema, `retention_period: 720h` (30 days, see log inventory), compactor
with retention enabled, `auth_enabled: false` only because it sits on the
isolated `observability` network (documented local deviation).

`deploy/alloy/config.alloy`:

```alloy
// Receive OTLP logs from the app, batch them, push to Loki's OTLP endpoint.
otelcol.receiver.otlp "default" {
  http {
    endpoint = "0.0.0.0:4318"
  }
  output {
    logs = [otelcol.processor.batch.default.input]
  }
}

otelcol.processor.batch "default" {
  output {
    logs = [otelcol.exporter.otlphttp.loki.input]
  }
}

otelcol.exporter.otlphttp "loki" {
  client {
    endpoint = "http://loki:3100/otlp"
  }
}
```

Production replaces `http://` with TLS endpoints and adds authentication
between collector and backend (ASVS 12.3.1, 16.4.3).

## 11. `.env.example` (no credentials — ASVS 13.3.1)

```dotenv
# Non-secret configuration only. Secrets live in ./secrets (make secrets).
FOLLOWUP_ENV=local                 # local | ci | prod
FOLLOWUP_LOG_LEVEL=INFO
FOLLOWUP_LOG_FORMAT=json           # json | console
FOLLOWUP_TASK_API_URL=http://mock-api:8081
FOLLOWUP_DELIVERY_API_URL=http://mock-api:8081
FOLLOWUP_LLM_PROVIDER=stub         # stub | router (OpenAI, config/llm.toml)
FOLLOWUP_OPENAI_AUTH_MODE=api_key  # api_key | workload_identity
FOLLOWUP_OPENAI_PROJECT=           # proj_... (non-secret)
FOLLOWUP_LLM_TIMEOUT_S=10
FOLLOWUP_LLM_DAILY_BUDGET_USD=5
FOLLOWUP_HTTP_TIMEOUT_S=5
FOLLOWUP_JWT_AUDIENCE=techcom-followup-api
FOLLOWUP_JWT_ISSUER=techcom-local-issuer
FOLLOWUP_OTLP_ENDPOINT=            # e.g. http://alloy:4318 with make up-obs
FOLLOWUP_DOCS_ENABLED=true         # OpenAPI UI; forced false when ENV=prod
FOLLOWUP_AS_OF=2026-10-21
FOLLOWUP_MOCK_FAULTS=
```

## 12. `.pre-commit-config.yaml` and 13. `Makefile`

Superseded by `17` §6 and §9 (full content).

## 14. CI

Superseded by `17` §10 (full workflow, SHA-pinned actions, digest-pinned
scanners).
