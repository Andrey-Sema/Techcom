# 08 — Scope, Status and Next Steps

## 1. Scope

The brief asks for a small, reliable service. The work is split into the
part every reviewer needs and optional production extensions that can be
read independently.

| Layer | Contents | Needed for the demo |
|-------|----------|---------------------|
| **Core** | Ingestion and normalization, deterministic policy, LLM note interpretation, bounded LLM orchestrator, outbox with idempotent delivery and reconciliation, CLI, mock API, tests | Yes |
| **Production extensions** | Authenticated REST API, PostgreSQL-shared rate limits and recipient quota, retention, observability stack, container images, security gates, LLM evaluation harness, 10k benchmark | No |

The core runs with `make demo` alone: no Docker, database or API key.

## 2. Status

Security follow-up (ADR-0017): tracker-surface screening and contact
minimisation, conservative ETA/negation handling, source-state
notification slots (with an escalation stage), shared receiver message
bounds and preservation of canonical IDs during contact minimisation.

Orchestrator authority (ADR-0018): the model chooses message intents
(asks, tone, a verified mention of an earlier message) and may hold an
action for human review; code writes every word. The daily LLM budget
is shared by all workers through a PostgreSQL ledger, and saved runs
require PostgreSQL. Threats and tests per OWASP LLM risk:
`security/owasp-llm-top10.md`. Verified locally with PostgreSQL 18; CI
cannot start until the repository has Actions minutes.

| Area | State | Notes |
|------|-------|-------|
| Domain core (normalize, policy R1–R9, identity, messages, injection screen) | done | Unit, property, golden and mutation tests |
| Mock task/delivery API with fault injection and durable journal | done | One receiver process per journal |
| Persistence (PostgreSQL 18, outbox, fencing, SKIP LOCKED, migrations) | done | Memory and PostgreSQL share one contract suite |
| Processing, dispatch, reconciliation, retries, kill switch, drain | done | Scenarios S1–S8 |
| LLM: note interpretation with grounding and cache | done | Provider quality not measured on real traffic |
| LLM: bounded orchestrator (tools, envelope, guard, audit) | done | ADR-0016, ADR-0018 (message intents) |
| Human review of held actions (`followup review`) | done | CLI; a review UI is product work |
| Shared LLM spend ledger (PostgreSQL) | done | Migration `0008`; fails closed |
| CLI and offline demo with committed samples | done | `make demo-check` compares a fresh run with `samples/` |
| REST API, JWT, project authorization, shared limits | done | Optional; see `19` |
| Observability (Prometheus, Grafana, Loki, Alloy, OTLP) | done | Catalogued metrics emitted by the API and CLI (textfile); alerts and dashboard checked against the catalogue |
| Retention, backup/restore, DB roles | done | Runbook `18` |
| Images, SBOM, scanners, CI workflows | done | CI cannot start until the repository has Actions minutes |
| DAST (ZAP) and registry attestation | prepared | Run in a deployment environment |

## 3. Not implemented, and why

| Item | Why it was left out |
|------|---------------------|
| Real monday.com (or other tracker) adapter | The brief supplies a fixture and asks for a mock API. The `TaskSource` port is the seam. |
| Real e-mail/SMS delivery | The brief forbids real e-mails. The delivery port takes a stable operation id, so a provider with idempotency keys plugs in. |
| Re-reminders on an unchanged task | Needs a product decision (cadence, quiet hours). Today one follow-up is sent per task state; once a follow-up was sent, a changed but still overdue task lets the orchestrator escalate. |
| Review UI and API endpoint | Held actions are reviewed with `followup review`; a UI with role-based approval is product work. |
| Measured quality on a real LLM provider | No production traffic or labelled real notes. The evaluation harness replays synthetic recordings; the live test is opt-in with a key. |
| Real identity provider (OIDC) | Tokens are verified with configured Ed25519 keys and the same claims an IdP would issue. |
| Internal TLS and dynamic database credentials | Local Compose keeps them out; production requirements are listed in `15`. |
| Distributed tracing | Logs and metrics cover the workflow; OTLP logs are wired. |

## 4. What changes for production

1. Replace the mock adapters with the real tracker and messaging provider.
2. Run behind a TLS gateway; issue service tokens from the company IdP.
3. Calibrate the orchestrator on real, labelled notes; record provider
   responses into the evaluation harness and gate prompt changes on it.
4. Add a provider-side spend cap on top of the shared ledger.
5. Add re-reminder cadence and a review UI once the product owner
   defines them.
6. Run DAST, image attestation and the benchmark in the deployment
   pipeline.
