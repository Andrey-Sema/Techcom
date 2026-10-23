# TechCom AI Operations & Follow-up Service

> Status: **M0–M5, M6a, API core M6b and M11a/b are implemented**.
> Full tooling, containers, observability configuration and operations work
> from `main` are preserved. ASVS L2 is a design target; remaining deployment
> and security gaps are explicit in the [tracker](docs/08-implementation-plan.md)
> and [API/evaluation review](docs/22-api-evaluation-review.md).

A Python service that reads construction project tasks through a mock API,
decides **deterministically** which ones need attention, uses an **LLM only
for language** (ambiguous notes, message wording) under strict validation,
and emits follow-up actions that are **safe to run repeatedly** — no
duplicates, no invented facts, no unsafe automatic actions, even when APIs
throttle, fail or time out after a successful write.

## Design

1. **Hexagonal, functional core** — pure policy, every dependency behind a
   port, faults injectable.
2. **Rules decide, LLM phrases** — the LLM output schema has no owner, date,
   status or decision fields; evidence must be quoted verbatim from the note.
3. **Transactional outbox + idempotent receiver + reconciliation** —
   effectively-once delivery; a timeout means "unknown", never "failed".
4. **PostgreSQL 18** — `UNIQUE` dedupe, `SKIP LOCKED` claims, `uuidv7()`;
   no Redis/Celery needed.
5. **100 % branch coverage**, strict PEP 8 (79 cols), `mypy --strict`,
   property tests with Hypothesis. Mutation testing is planned in M8.
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
| [ADRs](docs/adr/) | Architecture decision records 0001–0014 |

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

## Running the implemented components

Python 3.13 or 3.14 and uv are required. Docker is required for PostgreSQL
contract/integration tests. Commands run from the repository root:

```sh
make bootstrap
make quality
make test-fast       # deterministic offline tests, no Docker or API key
make test            # includes PostgreSQL 18 tests and 100% branch gate
uv run --locked pre-commit install
make mock            # local-only receiver at 127.0.0.1:8081, one worker
```

`make mock` writes and fsyncs `var/delivery_log.jsonl` before acknowledging
delivery and restores committed receipts on restart. The in-memory mock
store is available for injected tests. The journal has no retention
pruning yet: it retains keys for the entire journal lifetime. Run exactly
one receiver process per journal. The mock has no production authentication
and must remain local; its package is excluded from the production wheel.

For a local PostgreSQL instance, create an ignored password file first:

```sh
mkdir -p secrets
uv run python -c 'import secrets; from pathlib import Path; p = Path("secrets/postgres_password"); p.touch(mode=0o600, exist_ok=False); p.write_text(secrets.token_urlsafe(32))'
make db-up
```

Set `FOLLOWUP_DATABASE_URL` through your local secret configuration before
`uv run alembic upgrade head`; no database URL or password is checked in.
The Compose database user is for local development. Production database
roles, JWT authorization, project/object authorization and the complete
ASVS L2 evidence are pending milestones M6b/M9.

CI requires PostgreSQL tests to run; a missing Docker daemon fails CI.
Local full-suite runs skip unavailable PostgreSQL tests and consequently
fail the 100% coverage gate. Use `make test-fast` for an explicit subset.
Live LLM calls are opt-in and excluded from all default verification.

## Workflow commands

```sh
make demo                 # owns a fresh receiver on 127.0.0.1:8081
uv run --locked followup run --fixture fixtures/tasks.json --as-of 2026-10-21 --dry-run
uv run --locked followup run --as-of 2026-10-21 --dispatch --output var/report
uv run --locked followup dispatch --limit 50
uv run --locked followup reconcile --limit 50
uv run --locked followup report --run-id <uuid>
```

`make demo` requires port 8081 to be free. It starts and stops its own
receiver, uses a fresh journal, sends seven messages, reconciles unknown
outcomes, and reruns the same snapshot: seven duplicate proposals create
zero extra sends. Reports and the delivery journal are in `var/demo/`;
[committed samples](samples/README.md) come from this command. Demo forces
memory storage, the stub, loopback endpoints and disabled environment
proxies, even when deployment settings select PostgreSQL or a real LLM.
The development supervisor requires a POSIX system.

`run` uses memory unless `FOLLOWUP_DATABASE_URL` is configured. Memory
state ends with the command; the response labels it explicitly. Separate
`dispatch`, `reconcile` and `report` invocations require PostgreSQL. A
fixture replaces source HTTP fetching. Dry-run computes decisions and
messages without persistence, delivery or LLM calls; source HTTP is still
used when no fixture is supplied. `FOLLOWUP_DISPATCH_ENABLED=false`
prevents new sends. SIGTERM closes delivery admission, including a lazily
created dispatcher, and allows up to 25 seconds for owned work to drain.
Reconciliation may complete lookups during that drain; cancellation keeps
uncertain sends in `unknown` for later recovery.

Default LLM mode is `stub`; templates remain available during model
failure. `FOLLOWUP_LLM_PROVIDER=router` enables the configured OpenAI
routes and requires credentials as described in [ADR-0013](docs/adr/0013-openai-model-router.md).
Schema validation, grounded evidence and code-owned wording constrain
model output; they do not establish real-provider classification quality.
The USD budget is process-local and includes a documented provider framing
allowance; see [ADR-0014](docs/adr/0014-owned-language-and-budget-bounds.md).

The original timebox and 2,500-line budget have been exceeded under the
expanded implementation scope. The tracker records the size deviation;
plan hour estimates are not a measured timesheet. CI is configured for
Python 3.13/3.14, but GitHub Actions currently cannot start because the
private repository's account has exhausted its minutes. Local verification
and independent review evidence are recorded separately.

## API and evaluation

The authenticated ASGI factory is
`techcom_followup.adapters.inbound.api.app.create_app`. It accepts an
explicit repository, run service, verifier, clock and observer. The real
processing use case is exercised through this factory in scenario tests.
`create_metrics_app(observer)` supplies a separate internal ASGI surface;
its deployment listener and the concrete OTLP exporter require wiring.
The public service has no `/metrics`, interactive docs or debug endpoint.
See the [review](docs/22-api-evaluation-review.md) for scopes, signed project
memberships, limits and fail-closed legacy run visibility.

```sh
make eval             # synthetic contract replay, no credentials or network
make eval-test        # evaluator tests, independent 100% branch gate
make gates            # static/dependency/configuration gates
make demo-check       # compare a real demo against committed samples
```

The current evaluation keeps 63 benign notes and 32 injection variants.
Macro-F1 is 0.990718 on synthetic replay; **real-provider quality remains
unverified**. Provider-origin claims cannot be authenticated offline.
Recorded contracts, negative controls and limits are described in
[the evaluation guide](tests/eval/README.md). Historical reports remain
unchanged when application code changes.

`make bench`, `make image-test`, `make image-scan`, `make mutation`,
`make up-obs`, `make obs-check`, `make db-backup` and `make db-restore`
come from the integrated hardening/operations work. Read the runbook before
using database restore or changing deployment state.
