# 17 — Tooling & Repository Hygiene (single source of truth)

This document supersedes the tooling fragments in `13` (§2, §4 ruff/mypy,
§12–14). Rule: **every quality claim in the README must be enforced by a
tool that fails the build**. A tool that only "reports" is not a gate.

## 1. Toolchain at a glance

| Concern | Tool | Gate | Where | Why this one (vs. alternatives) |
|---------|------|------|-------|---------------------------------|
| Lint + format (PEP 8, 79 cols) | **Ruff** (`select = ["ALL"]`, curated ignores) | fail on any finding | pre-commit, CI | One binary replaces Flake8 + plugins, isort, pyupgrade, Black. `ALL` + explicit ignores is stricter and self-documenting; version pinned because new rules land in minors. |
| Security SAST (Python) | **Bandit** (standalone) **and** Ruff `S` | fail on any finding ≥ low severity / ≥ medium confidence | pre-commit, CI (SARIF artefact) | Ruff `S` gives instant editor feedback; standalone Bandit is the authoritative, auditable gate with its own report and baseline semantics. |
| Security SAST (project invariants) | **Semgrep** with registry packs **+ custom rules** (§7) | fail on error | CI | Enforces *our* invariants (no `str.format` on drafts, no `verify=False`, no naive `datetime.now()` in domain). |
| Types | **mypy --strict** (+ pydantic plugin) | fail | pre-commit, CI | Mature; `ty` still beta. |
| Architecture rules | **import-linter** | fail | pre-commit, CI | Proves hexagonal dependency direction. |
| Complexity | Ruff `C90` (max 8) + **xenon** (max absolute B, module A, average A) | fail | CI | Objective "readable functions" gate; radon report as artefact. |
| Dead code | **vulture** (min confidence 80, whitelist file) | fail | pre-push, CI | Catches unused functions/params left by refactors. |
| Dependency hygiene | **deptry** | fail | CI | Missing / unused / transitive-only imports vs `pyproject.toml`. |
| Vulnerable deps | **pip-audit** (on `uv export` with hashes) | fail (strict) | CI, nightly | PyPA tool on the OSV database. |
| Secrets | **gitleaks** | fail | pre-commit, CI (full history) | Defence in depth with `detect-private-key`. |
| Spelling | **codespell** | fail | pre-commit | Cheap; catches typos in identifiers/docs that reviewers notice. |
| YAML | **yamllint** | fail | pre-commit | compose, workflows, Prometheus, Grafana provisioning. |
| Workflows | **zizmor** + `check-jsonschema` (workflows, dependabot) | fail | pre-commit, CI | GitHub Actions security (template injection, excessive permissions, unpinned uses). |
| Dockerfile | **hadolint** | fail ≥ warning | pre-commit, CI | Dockerfile best practices. |
| Image / config | **Trivy** (image + config), **pinned by digest** | fail HIGH/CRITICAL with fix | CI | Trivy itself was compromised in March 2026 (v0.69.4 and images 0.69.5–0.69.6): pin the binary/action by SHA/digest, never by tag. |
| Alert rules | `promtool check rules` | fail | CI | Prometheus rules are code. |
| pyproject validity | **validate-pyproject** | fail | pre-commit | PEP 621 schema. |
| Lock consistency | `uv lock --check` (uv-pre-commit) | fail | pre-commit, CI | `uv.lock` always matches `pyproject.toml`. |
| Commits | **commitizen** (Conventional Commits, `commit-msg` hook) | fail | commit-msg | Enables changelog/version bumps (`cz bump`). |
| Tests | pytest (+asyncio, cov, xdist, randomly), hypothesis, respx, syrupy, testcontainers, **schemathesis** | 100 % branch coverage | CI | Schemathesis = property-based API testing from our OpenAPI schema. |
| Mutation | mutmut (fallback: cosmic-ray) | ≥ 90 % killed on domain | nightly | Proves tests assert behaviour, not just execute lines. |
| Docs | **MkDocs Material**, `mkdocs build --strict` | fail on broken links/nav | CI | 18 docs + ADRs must stay consistent; strict build catches dead links. |
| SBOM | cyclonedx-py (`cyclonedx-bom`) | artefact | CI | ASVS 15.1.2. |
| Deps updates | Dependabot (`uv`, `github-actions`, `docker`) with cooldown | PRs | GitHub | Native, no extra service; known uv gaps documented (Renovate as alternative). |

Considered and deliberately **not** added (with reason): Black/isort/Flake8/
Pylint (subsumed by Ruff), SonarQube (solo repo; Ruff+Bandit+Semgrep cover
it), tox/nox (single app; CI matrix via `uv run --python`), Faker /
factory_boy (Hypothesis strategies + tiny builders suffice),
freezegun/time-machine (time is injected through the `Clock` port, so no
global patching is needed), Sentry (no external SaaS for this exercise;
Loki + alert rules cover error visibility — drop-in later), safety
(commercial DB; pip-audit suffices), Checkov (no Terraform here).

## 2. `pyproject.toml` — tool sections (replaces `13` §4 tool sections)

```toml
[dependency-groups]
dev = [
    "bandit[toml,sarif]>=1.8",
    "codespell>=2.3",
    "cyclonedx-bom>=5.0",
    "deptry>=0.21",
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
    "schemathesis>=3.38",
    "syrupy>=4.8",
    "techcom-mocks",
    "testcontainers[postgres]>=4.8",
    "vulture>=2.13",
    "xenon>=0.9",
]
docs = ["mkdocs-material>=9.5"]

# ---------------------------------------------------------------- ruff --
[tool.ruff]
line-length = 79                       # strict PEP 8
target-version = "py313"               # keep in sync with mypy
src = ["src", "mocks/src", "tests"]
extend-exclude = ["migrations/versions/*_initial.py"]

[tool.ruff.lint]
select = ["ALL"]
ignore = [
    "COM812", "ISC001",   # conflict with ruff format
    "D203", "D213",       # pick D211 / D212 (Google style)
    "TRY003",             # EM rules already force message variables
    "FIX002",             # TODOs allowed only with an issue link (TD003)
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "D", "PLR2004", "ANN", "ARG", "FBT", "INP001", "SLF001"]
"mocks/tests/**" = ["S101", "D", "PLR2004", "ANN", "ARG", "INP001"]
"migrations/**" = ["D", "ERA001", "INP001"]
"scripts/**" = ["INP001", "T201"]
"src/techcom_followup/adapters/inbound/cli.py" = ["FBT001", "FBT003"]

[tool.ruff.lint.pycodestyle]
max-doc-length = 72

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.mccabe]
max-complexity = 8

[tool.ruff.lint.pylint]
max-args = 6
max-branches = 10
max-returns = 4
max-statements = 40

[tool.ruff.lint.flake8-bugbear]
extend-immutable-calls = [
    "fastapi.Depends", "fastapi.Query", "fastapi.Security",
    "typer.Option", "typer.Argument",
]

[tool.ruff.lint.flake8-tidy-imports]
ban-relative-imports = "all"

[tool.ruff.lint.isort]
known-first-party = ["techcom_followup", "techcom_mocks"]

[tool.ruff.format]
docstring-code-format = true

# --------------------------------------------------------------- bandit --
[tool.bandit]
exclude_dirs = ["tests", "mocks/tests", ".venv"]
# No global skips. A suppression must be inline "# nosec BXXX" plus a
# justification comment; a pre-commit pygrep hook rejects bare "# nosec".

# ---------------------------------------------------------------- mypy --
[tool.mypy]
python_version = "3.13"
strict = true
warn_unreachable = true
enable_error_code = [
    "ignore-without-code", "redundant-expr", "truthy-bool",
    "possibly-undefined", "explicit-override",
]
plugins = ["pydantic.mypy"]
files = ["src", "mocks/src", "tests"]

# Allow-list of third-party modules without type information. Adding an
# entry requires a comment with the reason; no blanket ignores.
[[tool.mypy.overrides]]
module = ["testcontainers.*", "xenon.*"]
ignore_missing_imports = true

# ------------------------------------------------------------- vulture --
[tool.vulture]
paths = ["src", "mocks/src", "tests/vulture_whitelist.py"]
min_confidence = 80
sort_by_size = true

# --------------------------------------------------------------- deptry --
[tool.deptry]
known_first_party = ["techcom_followup", "techcom_mocks"]

# ------------------------------------------------------------- codespell --
[tool.codespell]
skip = "*.lock,fixtures/*,samples/*,*.svg"
ignore-words-list = "ot"

# ------------------------------------------------------------ commitizen --
[tool.commitizen]
name = "cz_conventional_commits"
version_provider = "uv"
tag_format = "v$version"
update_changelog_on_bump = true
```

Pytest, coverage and import-linter sections stay as in `13` §4, with one
change: `filterwarnings = ["error"]` plus targeted, commented
`ignore:` entries for third-party deprecations (never a blanket ignore).

**Naming fix found by Ruff `A005`:** modules must not shadow the standard
library — `infrastructure/logging.py` → `log_config.py`,
`infrastructure/http.py` → `http_client.py`.

## 3. `.gitignore` (replaces `13` §2)

```gitignore
# ---------- Python build & bytecode ----------
__pycache__/
*.py[cod]
*$py.class
*.so
build/
dist/
*.egg-info/
.eggs/
wheels/
pip-wheel-metadata/

# ---------- Environments & tool caches ----------
.venv/
venv/
.python-version.local
.direnv/
.envrc
.cache/
.ruff_cache/
.mypy_cache/
.pytest_cache/
.hypothesis/
.import_linter_cache/
.tox/
.nox/

# ---------- Tests, coverage, mutation, benchmarks ----------
.coverage
.coverage.*
coverage.xml
htmlcov/
junit*.xml
mutants/
.mutmut-cache
.benchmarks/
schemathesis-report/

# ---------- Profiling & debugging artefacts ----------
*.prof
*.pstats
profile*.svg
memray-*.bin
*.trace

# ---------- Runtime output (samples/ IS committed) ----------
var/
*.log
*.pid
*.sqlite
*.db

# ---------- Secrets — never commit (ASVS 13.3.1) ----------
.env
.env.*
!.env.example
secrets/
*.pem
*.key
*.crt
*.csr
*.p12
*.pfx
*.jks
*.kdbx
id_rsa*
id_ed25519*

# ---------- Security tooling output ----------
*.sarif
bandit-report.*
semgrep-report.*
sbom*.json
trivy-*.json
.trivycache/

# ---------- Docs build ----------
site/

# ---------- Editors & OS ----------
.idea/
.vscode/*
!.vscode/extensions.json
!.vscode/settings.json
*.swp
*.swo
*~
.DS_Store
Thumbs.db
*.orig
*.rej
```

## 4. `.gitattributes`

```gitattributes
* text=auto eol=lf
*.py   text diff=python
*.md   text diff=markdown
*.sh   text eol=lf
*.png  binary
*.jpg  binary
*.pdf  binary
uv.lock         linguist-generated=true
samples/**      linguist-generated=true
docs/**         linguist-documentation
```

Line endings are normalised to LF for everyone (Windows contributors
included), so diffs and Docker builds are byte-identical.

## 5. `.editorconfig`

```ini
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4

[*.py]
max_line_length = 79

[*.{yml,yaml,json,toml}]
indent_size = 2

[*.md]
trim_trailing_whitespace = false

[Makefile]
indent_style = tab
```

## 6. `.pre-commit-config.yaml`

All `rev:` values are pinned to a release tag **and** frozen to the commit
SHA with `pre-commit autoupdate --freeze` (SLSA-style pinning).

```yaml
minimum_pre_commit_version: "4.0.0"
default_install_hook_types: [pre-commit, commit-msg, pre-push]
default_stages: [pre-commit]

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: <sha>  # frozen
    hooks:
      - id: check-added-large-files
        args: [--maxkb=500]
      - id: check-case-conflict
      - id: check-merge-conflict
      - id: check-json
      - id: check-toml
      - id: check-yaml
      - id: check-executables-have-shebangs
      - id: debug-statements
      - id: detect-private-key
      - id: end-of-file-fixer
      - id: mixed-line-ending
        args: [--fix=lf]
      - id: no-commit-to-branch
        args: [--branch, main]
      - id: trailing-whitespace

  - repo: https://github.com/astral-sh/uv-pre-commit
    rev: <sha>
    hooks:
      - id: uv-lock

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: <sha>
    hooks:
      - id: ruff-check
        args: [--fix, --exit-non-zero-on-fix]
      - id: ruff-format

  - repo: https://github.com/PyCQA/bandit
    rev: <sha>
    hooks:
      - id: bandit
        args: [-c, pyproject.toml, -r, src, mocks/src]
        additional_dependencies: ["bandit[toml]"]
        pass_filenames: false

  - repo: https://github.com/gitleaks/gitleaks
    rev: <sha>
    hooks:
      - id: gitleaks

  - repo: https://github.com/codespell-project/codespell
    rev: <sha>
    hooks:
      - id: codespell

  - repo: https://github.com/adrienverge/yamllint
    rev: <sha>
    hooks:
      - id: yamllint

  - repo: https://github.com/hadolint/hadolint
    rev: <sha>
    hooks:
      - id: hadolint-docker

  - repo: https://github.com/abravalheri/validate-pyproject
    rev: <sha>
    hooks:
      - id: validate-pyproject

  - repo: https://github.com/python-jsonschema/check-jsonschema
    rev: <sha>
    hooks:
      - id: check-github-workflows
      - id: check-dependabot

  - repo: https://github.com/woodruffw/zizmor-pre-commit
    rev: <sha>
    hooks:
      - id: zizmor

  - repo: https://github.com/commitizen-tools/commitizen
    rev: <sha>
    hooks:
      - id: commitizen
        stages: [commit-msg]

  - repo: local
    hooks:
      - id: mypy
        name: mypy --strict
        entry: uv run --frozen mypy
        language: system
        types: [python]
        pass_filenames: false
      - id: import-linter
        name: import-linter (hexagonal layers)
        entry: uv run --frozen lint-imports
        language: system
        pass_filenames: false
        types: [python]
      - id: forbid-pragma-no-cover
        name: forbid "pragma no cover"
        entry: 'pragma:\s*no\s*cover'
        language: pygrep
        types: [python]
      - id: forbid-bare-nosec
        name: nosec must name a Bandit test id
        entry: '#\s*nosec(?!\s+B\d{3})'
        language: pygrep
        types: [python]
      - id: forbid-blanket-type-ignore
        name: type ignore must name an error code
        entry: '#\s*type:\s*ignore(?!\[)'
        language: pygrep
        types: [python]
      - id: vulture
        name: vulture (dead code)
        entry: uv run --frozen vulture
        language: system
        pass_filenames: false
        stages: [pre-push]
      - id: pytest-fast
        name: unit + property tests
        entry: uv run --frozen pytest -m "not integration" -q -x -p no:randomly --no-cov
        language: system
        pass_filenames: false
        stages: [pre-push]
```

## 7. Semgrep custom rules (`.semgrep/project-invariants.yml`)

Each rule encodes a design decision so it cannot silently regress.

| Rule id | Forbids | Protects |
|---------|---------|----------|
| `no-str-format-in-messages` | `$X.format(...)`, `$X.format_map(...)`, `%` formatting in `domain/messages.py` and `application/drafter.py` | ASVS 1.3.7/1.3.10 (D5) |
| `no-tls-verify-false` | `verify=False` in any httpx/ssl call | ASVS 12.3.2 |
| `no-follow-redirects` | `follow_redirects=True` | ASVS 15.3.2 |
| `no-naive-clock-in-core` | `datetime.now(...)`, `datetime.utcnow()`, `date.today()`, `time.time()` under `domain/` and `application/` | determinism, injected `Clock` |
| `no-raw-sql-interpolation` | `sqlalchemy.text(f"...")` / `text("..." + $X)` | ASVS 1.2.4 |
| `no-untrusted-text-in-logs` | logging calls with `notes=`, `prompt=`, `raw_output=` keyword fields | ASVS 16.2.5 |
| `no-llm-output-without-validation` | returning `LLMResult.text` from `application/` without passing through `validate_*` | LLM05 |
| `no-direct-env-access` | `os.environ[...]`, `os.getenv(...)` outside `infrastructure/settings.py` | config single source |

Every rule ships with a positive and a negative example under
`.semgrep/tests/` and is run with `semgrep --test` in CI.

## 8. Other repository files

| File | Content (summary) |
|------|-------------------|
| `.yamllint.yaml` | `extends: default`; `line-length: {max: 120, level: warning}`; `truthy: {allowed-values: ["true", "false", "on"]}` (Actions `on:`); `document-start: disable`. |
| `.hadolint.yaml` | `failure-threshold: warning`; `trustedRegistries: [docker.io, ghcr.io]`; no ignores. |
| `.gitleaks.toml` | `[extend] useDefault = true`; no allow-lists (fixtures contain no secrets). |
| `.semgrep/` | custom rules + tests (§7). |
| `.github/CODEOWNERS` | `* @Andrey-Sema` |
| `.github/dependabot.yml` | `uv` (weekly, `cooldown: {default-days: 7}`, minor/patch grouped), `github-actions` (weekly), `docker` (weekly). |
| `.github/pull_request_template.md` | What / Why / How tested / ASVS ids touched / Screenshots of run report / Checklist (DoD from `07` §8). |
| `.github/ISSUE_TEMPLATE/{bug,feature}.yml` | structured forms. |
| `SECURITY.md` | private disclosure contact, supported versions, remediation SLAs (ASVS 15.1.1). |
| `CONTRIBUTING.md` | setup (`make bootstrap`), commit convention, DoD, how to add an ASVS marker, how to regenerate samples. |
| `CHANGELOG.md` | Keep a Changelog format, generated by `cz bump`. |
| `LICENSE` | Proprietary "All rights reserved" (private assessment code) — decision for the repo owner. |
| `mkdocs.yml` | Material theme, nav = `docs/` + ADRs, `pymdownx.superfences` Mermaid fence, `strict: true`. |
| `scripts/` | Developer-only tools **outside the shipped package** (ASVS 15.2.3): `make_secrets.py`, `issue_token.py` (local stand-in for the IdP), `eval_llm.py`, `asvs_traceability.py`. |
| `tests/vulture_whitelist.py` | FastAPI route functions, Pydantic validators, Typer commands (framework-invoked). |
| `src/techcom_followup/py.typed` | PEP 561 marker. |
| `.vscode/extensions.json` | recommends Ruff, mypy, EditorConfig, Even Better TOML. |

## 9. `Makefile` (replaces `13` §13)

```makefile
.DEFAULT_GOAL := help
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
UV := uv run --frozen

.PHONY: help bootstrap secrets lint format type bandit semgrep complexity \
        deadcode deps quality test test-fast test-integration mutation \
        security sbom docs up up-obs down demo demo-fault samples ci-token \
        db-backup db-restore eval bench clean

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*## "}; {printf "  %-18s %s\n", $$1, $$2}'

bootstrap: ## Install deps, git hooks, .env and local secrets
	uv sync --locked --all-groups
	$(UV) pre-commit install --install-hooks
	cp -n .env.example .env || true
	$(MAKE) secrets

secrets: ## Generate local secrets into ./secrets (idempotent)
	$(UV) python scripts/make_secrets.py

format: ## Apply formatter and safe fixes
	$(UV) ruff format .
	$(UV) ruff check . --fix

lint: ## Ruff (lint + format check) and import-linter
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) lint-imports

type: ## mypy --strict
	$(UV) mypy

bandit: ## Bandit security scan (authoritative gate)
	$(UV) bandit -c pyproject.toml -r src mocks/src -l -ii

semgrep: ## Semgrep registry packs + project invariants
	semgrep scan --error --config p/python --config p/owasp-top-ten \
	  --config .semgrep/ src mocks/src

complexity: ## Cyclomatic complexity gate
	$(UV) xenon --max-absolute B --max-modules A --max-average A src mocks/src

deadcode: ## Dead code
	$(UV) vulture

deps: ## Dependency hygiene and known vulnerabilities
	$(UV) deptry .
	uv export --frozen --no-dev --no-emit-project --format requirements-txt \
	  -o var/requirements.txt
	$(UV) pip-audit --strict --require-hashes -r var/requirements.txt

quality: lint type bandit complexity deadcode deps ## All static gates

test: ## Full suite, 100 % branch coverage gate
	$(UV) pytest -n auto

test-fast: ## Unit + property tests, no Docker
	$(UV) pytest -m "not integration" -n auto --no-cov

test-integration: ## Integration tests (Docker)
	$(UV) pytest -m integration

mutation: ## Mutation testing on the domain
	$(UV) mutmut run

security: bandit semgrep deps ## Local security gates (+ Trivy in CI)
	gitleaks detect --redact

sbom: ## CycloneDX SBOM
	$(UV) cyclonedx-py environment -o var/sbom.cdx.json

docs: ## Build docs site strictly
	uv run --frozen --group docs mkdocs build --strict

up: ## Core stack
	docker compose up -d --build

up-obs: ## Core + observability stack
	docker compose -f compose.yaml -f compose.observability.yaml up -d --build

down: ## Stop everything
	docker compose -f compose.yaml -f compose.observability.yaml down

demo: up ## Scripted demo: run, dispatch, rerun, report
	docker compose exec app followup demo

demo-fault: ## Demo with timeout-after-commit fault
	FOLLOWUP_MOCK_FAULTS="messages:timeout_after_commit@1" $(MAKE) demo

samples: ## Regenerate committed samples/
	docker compose exec app followup demo --output samples/

ci-token: ## Print a short-lived service token for CI/local API calls
	@$(UV) python scripts/issue_token.py --sub ci --scope runs:trigger \
	  --scope runs:read --scope actions:read --ttl 10m

db-backup: ## pg_dump (custom format) into var/backups
	mkdir -p var/backups
	docker compose exec -T db pg_dump -U postgres -Fc followup \
	  > var/backups/followup-$$(date -u +%Y%m%dT%H%M%SZ).dump

db-restore: ## Restore FILE=... into the database
	docker compose exec -T db pg_restore -U postgres -d followup \
	  --clean --if-exists < $(FILE)

eval: ## LLM eval harness on recorded responses
	$(UV) python scripts/eval_llm.py --dataset tests/eval/notes.jsonl

bench: ## 10k-task benchmark (stub LLM, real Postgres)
	$(UV) pytest tests/bench -m bench --no-cov -q

clean: ## Remove caches and build output
	rm -rf .ruff_cache .mypy_cache .pytest_cache .hypothesis htmlcov \
	  coverage.xml var/*.json var/*.txt site dist
```

## 10. CI workflow (`.github/workflows/ci.yml`, replaces `13` §14)

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: "17 3 * * *"        # nightly: mutation, full-history secrets
permissions:
  contents: read
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
env:
  UV_FROZEN: "1"

jobs:
  quality:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@<sha>        # pinned by commit SHA
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha>
        with: { enable-cache: true }
      - run: uv sync --locked --all-groups
      # Every pre-commit hook (ruff, bandit, mypy, import-linter, codespell,
      # yamllint, hadolint, zizmor, validate-pyproject, uv-lock, pygrep
      # guards) on the whole tree: local and CI gates are identical.
      - run: uv run --frozen pre-commit run --all-files --show-diff-on-failure
      - run: make complexity deadcode deps

  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    strategy:
      fail-fast: false
      matrix:
        python: ["3.13", "3.14"]
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha>
        with: { enable-cache: true, python-version: "${{ matrix.python }}" }
      - run: uv sync --locked --all-groups
      - run: make test               # testcontainers uses the runner's Docker
      - run: uv run --frozen python scripts/asvs_traceability.py --check
      - uses: actions/upload-artifact@<sha>
        if: always()
        with:
          name: test-reports-${{ matrix.python }}
          path: |
            coverage.xml
            junit*.xml
            docs/asvs-traceability.md

  security:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false, fetch-depth: 0 }
      - uses: astral-sh/setup-uv@<sha>
      - run: uv sync --locked --all-groups
      - run: uv run --frozen bandit -c pyproject.toml -r src mocks/src
             -f sarif -o bandit.sarif || true   # report artefact
      - run: make bandit                          # the actual gate
      - run: pipx run semgrep==<pinned> scan --error --config p/python
             --config p/owasp-top-ten --config .semgrep/ --sarif
             -o semgrep.sarif src mocks/src
      - run: pipx run semgrep==<pinned> --test .semgrep/
      - uses: gitleaks/gitleaks-action@<sha>
        env: { GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }
      - run: pipx run zizmor==<pinned> .github/workflows/
      - run: make sbom
      - uses: actions/upload-artifact@<sha>
        with:
          name: security-reports
          path: |
            *.sarif
            var/sbom.cdx.json

  image:
    needs: [quality, test]
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - run: docker run --rm -i hadolint/hadolint@sha256:<digest> < Dockerfile
      - run: docker build --target runtime -t techcom-followup:ci .
      - run: docker build --target mock -t techcom-mock:ci .
      - name: Image must not contain test doubles or VCS data
        run: |
          ! docker run --rm --entrypoint python techcom-followup:ci \
              -c "import techcom_mocks"
          docker run --rm --entrypoint sh techcom-followup:ci \
              -c '! test -e /app/.git && ! test -e /app/tests'
      - name: Trivy image + config (pinned by digest, see 2026 incident)
        run: |
          docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
            aquasec/trivy@sha256:<digest> image --exit-code 1 \
            --severity HIGH,CRITICAL --ignore-unfixed techcom-followup:ci
      - run: docker run --rm -v "$PWD/deploy/prometheus:/p:ro"
             --entrypoint promtool prom/prometheus@sha256:<digest>
             check rules /p/rules/followup.rules.yml

  smoke:
    needs: [image]
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha>
      - run: uv sync --locked --all-groups
      - run: make bootstrap up demo
      - run: diff <(jq -S . samples/decisions.json) <(jq -S . var/decisions.json)
      - run: uv run --frozen schemathesis run http://127.0.0.1:8080/openapi.json
             --checks all --header "Authorization: Bearer $(make -s ci-token)"
      - run: docker run --rm --network host
             ghcr.io/zaproxy/zaproxy@sha256:<digest> zap-api-scan.py
             -t http://127.0.0.1:8080/openapi.json -f openapi -I

  docs:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha>
      - run: make docs

  mutation:
    if: github.event_name == 'schedule'
    runs-on: ubuntu-24.04
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@<sha>
      - run: uv sync --locked --all-groups
      - run: make mutation
```

Notes:

* The Trivy step mounts the Docker socket **on the CI runner only** (an
  ephemeral VM); no long-running container gets it.
* **SARIF upload to GitHub code scanning is not used**: for a private
  personal repository it requires GitHub Advanced Security. SARIF files are
  kept as build artefacts instead; switching to `upload-sarif` is one step
  once GHAS is available.
* `persist-credentials: false` on checkout and read-only default
  permissions follow the zizmor / OWASP CI-CD guidance.
* The OpenAPI document is enabled in CI (`FOLLOWUP_DOCS_ENABLED=true`)
  and disabled in production (ASVS 13.4.5).
