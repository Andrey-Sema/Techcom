# TechCom AI Operations & Follow-up Service

> Status: **design complete; implemented — domain core (M1,
> `src/techcom_followup/domain/`) and LLM router
> (`src/techcom_followup/adapters/outbound/llm/`); 298 tests, 100 %
> branch coverage** (plan for the
> TechCom AI Engineer final assessment). Evaluation date: 2026-10-21 UTC.

A Python service that reads construction project tasks through a mock API,
decides **deterministically** which ones need attention, uses an **LLM only
for language** (ambiguous notes, message wording) under strict validation,
and emits follow-up actions that are **safe to run repeatedly** — no
duplicates, no invented facts, no unsafe automatic actions, even when APIs
throttle, fail or time out after a successful write.

## Design in five lines

1. **Hexagonal, functional core** — pure policy, every dependency behind a
   port, faults injectable.
2. **Rules decide, LLM phrases** — the LLM output schema has no owner, date,
   status or decision fields; evidence must be quoted verbatim from the note.
3. **Transactional outbox + idempotent receiver + reconciliation** —
   effectively-once delivery; a timeout means "unknown", never "failed".
4. **PostgreSQL 18** — `UNIQUE` dedupe, `SKIP LOCKED` claims, `uuidv7()`;
   no Redis/Celery needed.
5. **100 % branch coverage**, strict PEP 8 (79 cols), `mypy --strict`,
   property and mutation tests.
6. **OWASP ASVS 5.0 Level 2 design target** with per-requirement traceability,
   OWASP Cheat Sheets and LLM Top 10; observability via Prometheus,
   Grafana, Loki and Alloy provisioned as code.

## Expected decisions on the supplied fixture

| Task | Decision | Target |
|------|----------|--------|
| TC-1001 | follow_up_owner | John Smith |
| TC-1002 | request_eta | warehouse |
| TC-1003 | no_action | — |
| TC-1004 | clarify_missing_info (owner) | Wisconsin Build coordinator |
| TC-1005 | clarify_missing_info (due date) | Alex Brown |
| TC-1006 | follow_up_owner + `prompt_injection_suspected` | Jamie Lee |
| TC-1007 | no_action | — |
| TC-1008 | escalate (blocked by upstream splice, 6 days overdue) | SURF Illinois PM |
| TC-1009 | request_eta | warehouse |
| TC-1010 | no_action | — |

## Documentation map

| Doc | Content |
|-----|---------|
| [00 Requirements analysis](docs/00-requirements-analysis.md) | FR/NFR, scenarios, rubric map, fixture analysis, assumptions |
| [01 Interview traceability](docs/01-interview-traceability.md) | Interview questions → features, answers to sharpen |
| [02 Architecture](docs/02-architecture.md) | Style, C4, layout, data flow, schema, API, config, stack |
| [03 OOAD map](docs/03-ooad.md) | Actors, use cases, domain model, ports, CRC, state machine, sequences |
| [04 Decision policy & LLM](docs/04-decision-policy-and-llm.md) | Normalization, rule table, LLM contract, guardrails, templates |
| [05 Reliability](docs/05-reliability.md) | Identity, transactions, retries, fault injection, FMEA, observability |
| [06 Test strategy](docs/06-test-strategy.md) | Pyramid, scenario tests, properties, 100 % coverage policy |
| [07 Acceptance criteria](docs/07-acceptance-criteria.md) | Senior+ MUST/SHOULD criteria, Gherkin, Definition of Done |
| [08 Implementation plan](docs/08-implementation-plan.md) | Milestones, estimates, step-by-step, out of scope |
| [09 Operations at scale](docs/09-operations-at-scale.md) | 10k tasks/day, cost, monitoring, access control |
| [10 Live review playbook](docs/10-live-review-playbook.md) | Demo script, injected-bug drills, change requests |
| [11 Research sources](docs/11-research-sources.md) | Authoritative references |
| [12 Plan self-review](docs/12-plan-review.md) | Red-team findings and fixes |
| [13 Repository blueprint](docs/13-repository-blueprint.md) | `.gitignore`, `.dockerignore`, `pyproject.toml`, Dockerfile, compose, Prometheus, Grafana, Loki, Alloy, CI |
| [14 Logging & observability](docs/14-logging-and-observability.md) | Log inventory, schema, event catalogue, classification, metrics, SLOs |
| [15 Security baseline](docs/15-security-asvs-l2.md) | OWASP ASVS 5.0 L2 design target, cheat sheets, API Top 10, supply chain, threat model |
| [16 Senior audit](docs/16-senior-audit.md) | Strict 30-point audit: proving mechanisms, gaps, fixes, expected score |
| [17 Tooling & repo hygiene](docs/17-tooling-and-repo-hygiene.md) | Ruff `ALL`, **Bandit**, Semgrep rules, mypy, vulture, xenon, deptry, full `.gitignore`/`.gitattributes`/`.editorconfig`, pre-commit, Makefile, CI |
| [18 Runbook](docs/18-runbook.md) | Alert playbooks, global levers, diagnostics, backup/restore |
| [ADRs](docs/adr/) | Architecture decision records 0001–0013 |

## Planned stack

Python ≥ 3.13 (CI 3.13/3.14) · uv · Ruff (`ALL`) · **Bandit** · Semgrep ·
mypy · import-linter · xenon · vulture · deptry · Pydantic v2 · FastAPI ·
Typer · httpx + stamina · SQLAlchemy 2.0 + psycopg 3 + Alembic ·
PostgreSQL 18 · **OpenAI router** (Responses API, `gpt-6.1-sol` → `gpt-6-sol` →
`gpt-5.6-sol`) + deterministic stub · structlog ·
prometheus-client · OpenTelemetry (OTLP logs) · PyJWT (EdDSA) ·
Prometheus + Grafana + Loki + Alloy · pytest + hypothesis +
testcontainers + syrupy + mutmut · gitleaks, pip-audit, Trivy, Semgrep,
zizmor, ZAP.

## Planned commands

```text
make demo        # compose up, migrate, run, dispatch, rerun, report
make test        # full suite, fails below 100 % branch coverage
make test-fast   # unit + property tests, no Docker
make quality     # ruff, mypy, bandit, import-linter, xenon, vulture, deptry
make eval        # LLM eval harness on recorded responses
make bench       # 10k-task benchmark
make security    # gitleaks, pip-audit, SBOM, Trivy
make up-obs      # + Prometheus, Grafana, Loki, Alloy
```
