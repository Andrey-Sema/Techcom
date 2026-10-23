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

| Area | State | Notes |
|------|-------|-------|
| Domain core (normalize, policy R1–R9, identity, messages, injection screen) | done | Unit, property, golden and mutation tests |
| Mock task/delivery API with fault injection and durable journal | done | One receiver process per journal |
| Persistence (PostgreSQL 18, outbox, fencing, SKIP LOCKED, migrations) | done | Memory and PostgreSQL share one contract suite |
| Processing, dispatch, reconciliation, retries, kill switch, drain | done | Scenarios S1–S8 |
| LLM: note interpretation with grounding and cache | done | Provider quality not measured on real traffic |
| LLM: bounded orchestrator (tools, envelope, guard, audit) | done | ADR-0016 |
| CLI and offline demo with committed samples | done | `make demo-check` compares a fresh run with `samples/` |
| REST API, JWT, project authorization, shared limits | done | Optional; see `19` |
| Observability (Prometheus, Grafana, Loki, Alloy, OTLP) | done | Provisioned as code |
| Retention, backup/restore, DB roles | done | Runbook `18` |
| Images, SBOM, scanners, CI workflows | done | CI cannot start until the repository has Actions minutes |
| DAST (ZAP) and registry attestation | prepared | Run in a deployment environment |

## 3. Not implemented, and why

| Item | Why it was left out |
|------|---------------------|
| Real monday.com (or other tracker) adapter | The brief supplies a fixture and asks for a mock API. The `TaskSource` port is the seam. |
| Real e-mail/SMS delivery | The brief forbids real e-mails. The delivery port takes a stable operation id, so a provider with idempotency keys plugs in. |
| Re-reminders on an unchanged task | Needs a product decision (cadence, quiet hours). Today one follow-up is sent per task state; once a follow-up was sent, a changed but still overdue task lets the orchestrator escalate. |
| Human approval queue for agent escalations | The envelope already restricts escalations to unanswered, overdue tasks. An approval step is a UI/workflow feature outside the brief. |
| Measured quality on a real LLM provider | No production traffic or labelled real notes. The evaluation harness replays synthetic recordings; the live test is opt-in with a key. |
| Cross-process LLM spend ledger | The daily budget is per process. A shared ledger belongs to the same database work as the recipient quota and is the next step for multi-worker LLM use. |
| Real identity provider (OIDC) | Tokens are verified with configured Ed25519 keys and the same claims an IdP would issue. |
| Internal TLS and dynamic database credentials | Local Compose keeps them out; production requirements are listed in `15`. |
| Distributed tracing | Logs and metrics cover the workflow; OTLP logs are wired. |

## 4. What changes for production

1. Replace the mock adapters with the real tracker and messaging provider.
2. Run behind a TLS gateway; issue service tokens from the company IdP.
3. Calibrate the orchestrator on real, labelled notes; record provider
   responses into the evaluation harness and gate prompt changes on it.
4. Move the LLM spend budget into PostgreSQL next to the recipient quota.
5. Add re-reminder cadence and an approval queue once the product owner
   defines them.
6. Run DAST, image attestation and the benchmark in the deployment
   pipeline.
