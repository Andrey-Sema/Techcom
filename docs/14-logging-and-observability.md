# 14 — Logging & Observability (ASVS V16, OWASP Logging Cheat Sheet)

## Implemented runtime boundary

The API runtime now owns bounded OTLP/HTTP JSON export and an isolated
metrics listener on port 9464. Only the closed projection in
`infrastructure/log_config.py` reaches stderr and OTLP; request UUIDs,
hashed actors and bounded counters survive, while text/credentials do not.
Export/drop metrics describe the collector boundary. The CLI retains its
sanitized stderr sink and does not start an OTLP worker. See
[API and deployment](19-api-and-deployment.md).

The sections below describe the **target operating model**. They do not
establish that every event, metric, trace field, retention/access policy or
dashboard family is implemented or deployed. In particular, there is no
current trace/span emitter. Final runtime changes received manual review
only; collector connectivity and deployment policies remain unverified.

Three signals, one correlation model:

| Signal | Tool | Question it answers |
|--------|------|---------------------|
| **Logs** (application + security + audit) | structlog → stdout JSON, plus OTLP → Alloy → Loki | *What exactly happened to task X / operation Y, and who triggered it?* |
| **Metrics** | prometheus-client → Prometheus → Grafana, alert rules | *Is the system healthy, within budget, and safe right now?* |
| **Audit trail** (business record) | `audit_events` table (append-only) | *Why did we (not) send this message?* — survives log retention |

Traces (OpenTelemetry) are a production follow-up; log lines already carry
`trace_id`/`span_id` fields (empty until OTel is enabled) so that switching
it on needs no log-schema change.

## 1. Log inventory (ASVS 16.1.1)

| Layer | Source | Events | Format | Destination | Access | Retention |
|-------|--------|--------|--------|-------------|--------|-----------|
| API (FastAPI) | `techcom_followup.api` | request summary, authn/authz outcome, validation rejections, rate limiting, unhandled errors | JSON (§3) | stdout + OTLP → Loki | ops + security roles in Grafana | 30 days |
| Use cases | `techcom_followup.app` | run lifecycle, decisions, dedupe, dispatch, reconcile | JSON | stdout + OTLP → Loki | ops | 30 days |
| LLM adapter | `techcom_followup.llm` | request result, tokens, cost, validation stage failures, breaker state (never prompt/response text) | JSON | stdout + OTLP → Loki | ops | 30 days |
| Security events | `techcom_followup.security` | §4 catalogue | JSON, `event.category=security` | stdout + OTLP → Loki (separate stream label) | security role only | 90 days |
| Audit trail | DB `audit_events` | business decisions & deliveries | JSONB rows | PostgreSQL (app has INSERT/SELECT only, no UPDATE/DELETE) | ops via API (`actions:read`) | 1 year |
| Mock API | `techcom_mocks` | requests, injected faults, replays | JSON | stdout + OTLP → Loki | dev | 7 days |
| PostgreSQL | server log | connections, errors, slow queries (`log_min_duration_statement=500ms`) | csvlog/jsonlog | stdout + OTLP → Loki | DBA | 30 days |
| Infrastructure | Prometheus/Grafana/Loki/Alloy | own logs | native | stdout | ops | 7 days |

ASVS 16.2.3: logs go **only** to the destinations in this table. No log
files inside containers (read-only FS), no third-party log SaaS.

## 2. Logging principles

1. **Structured, not formatted.** `log.info("action.enqueued",
   task_id=…, decision=…)` — the event name is a stable, dot-separated
   identifier; data travel as fields. No f-strings in log calls (ruff `G`).
2. **One event name per meaningful state change**, from a closed catalogue
   (§4) — dashboards and alerts query event names, not free text.
3. **Correlation everywhere** (`contextvars`): `request_id` (inbound),
   `run_id`, `task_id`, `operation_id`, `idempotency_key_short`, `actor`.
4. **UTC timestamps** in RFC 3339 with `Z` (ASVS 16.2.2); container clocks
   rely on host NTP.
5. **Levels have contracts** (§5); production default `INFO`.
6. **Classified data only** (§6): untrusted text and secrets are never
   logged; personal data is minimised.
7. **Injection-safe** (ASVS 16.4.1): JSON renderer escapes control
   characters and newlines; user-controlled strings are length-capped
   (256 chars) and passed only as field values — never as the message key.
8. **Logging never breaks the business flow**: logger errors are swallowed
   and counted (`followup_logging_errors_total`); sinks are non-blocking
   (stdout).
9. **Errors to clients are generic** (ASVS 16.5.1): RFC 9457
   `application/problem+json` with `type`, `title`, `status`, and the
   `request_id`; the stack trace goes only to the log.

## 3. Log record schema (ASVS 16.2.1 — when, where, who, what)

```json
{
  "ts": "2026-10-21T09:00:03.412Z",
  "level": "info",
  "event": "delivery.reconciled",
  "service": "followup",
  "env": "local",
  "version": "0.1.0+3f2a9c1",
  "logger": "techcom_followup.app.reconcile",
  "host": "app-1",
  "request_id": "01929b4e-7c1e-7d3a-9f00-3b1d2a4c5e6f",
  "trace_id": "",
  "span_id": "",
  "actor": {"type": "service", "id": "scheduler"},
  "run_id": "01929b4e-…",
  "task_id": "TC-1001",
  "operation_id": "7c1e…",
  "outcome": "success",
  "duration_ms": 41,
  "attempt": 2,
  "detail": {"previous_status": "unknown", "lookup_status": 200}
}
```

Field names follow the OWASP Logging Vocabulary / ECS spirit (`event`,
`outcome`, `actor`) so a SIEM can parse them without custom mapping
(ASVS 16.2.4).

## 4. Event catalogue

### Business / operational

| Event | Level | Key fields |
|-------|-------|-----------|
| `run.started` / `run.finished` / `run.failed` | info / info / error | `run_id`, `as_of`, counts, duration |
| `task.rejected` | warning | `task_ref` (index), `reasons` |
| `task.data_quality` | info | `task_id`, `issue` (`future_last_update`, `done_note_conflict`) |
| `decision.made` | info | `task_id`, `decision`, `rule_id`, `reason_code`, `flags` |
| `action.enqueued` / `action.duplicate_suppressed` | info | `task_id`, `idempotency_key_short` |
| `delivery.attempt` | debug | `operation_id`, `attempt` |
| `delivery.sent` / `delivery.retry_scheduled` | info / warning | `operation_id`, `status_code`, `next_attempt_at` |
| `delivery.unknown` / `delivery.reconciled` | warning / info | `operation_id`, `reason` |
| `delivery.dead` | error | `operation_id`, `status_code`, `attempts` |
| `llm.call` | info | `purpose`, `model`, `prompt_version`, `result`, `tokens_in/out`, `cost_usd`, `latency_ms` |
| `llm.route_failover` | warning | `purpose`, `model`, `kind` (`transient`/`rejected`), `detail` (status code / error class) |
| `llm.unavailable` / `llm.budget_exhausted` | error / warning | `purpose`, `attempts` |
| `llm.fallback` | warning | `purpose`, `stage` (`transport`/`json`/`schema`/`grounding`/`confidence`/`breaker`), `output_sha256` |
| `dependency.circuit_opened` / `circuit_closed` | warning / info | `dependency` |
| `http.retry` | warning | `dependency`, `status_code`, `delay_s`, `attempt` |

### Security (ASVS 16.3.x; OWASP Logging Vocabulary name in brackets, `custom` where the vocabulary has no equivalent)

| Event | Level | When |
|-------|-------|------|
| `security.authn_success` [custom] | info | Valid service token accepted (16.3.1) |
| `security.authn_failed` [custom; `authn_token_reuse` when a revoked/replayed `jti` is seen] | warning | Missing/expired/bad-signature/wrong-audience token (reason enum, never the token) |
| `security.authz_denied` [`authz_fail`] | warning | Valid token lacks scope (16.3.2) |
| `security.input_rejected` [`input_validation_fail`] | warning | Request/body validation failed at the trust boundary (16.3.3) |
| `security.rate_limited` [`excess_rate_limit_exceeded`] | warning | Client exceeded rate limit (16.3.3, 2.4.1) |
| `security.note_flagged` [`mcp_prompt_injection`] | warning | Prompt-injection screen matched (pattern ids only) |
| `security.llm_output_rejected` | warning | Output violated schema/grounding/message rules |
| `security.idempotency_mismatch` | error | Same key, different payload (possible bug or tampering) |
| `security.config_loaded` [custom] | info | Policy/routing file hash changed at startup |
| `security.startup` / `shutdown` [`sys_startup` / `sys_shutdown`] | info | Process lifecycle with version + config hashes |
| `security.tls_failure` | error | Outbound TLS verification failure (16.3.4) |
| `security.unexpected_error` | error | Unhandled exception (16.3.4), with `error_id` returned to the client |

## 5. Level contracts

| Level | Meaning | Paging? |
|-------|---------|---------|
| `debug` | Diagnostic detail (attempt-level); disabled in prod by default | no |
| `info` | Normal state changes worth keeping | no |
| `warning` | Degraded but handled (retry, fallback, rejection, security signal) | no — aggregated into alerts |
| `error` | An operation failed and needs human attention (dead letter, run failed) | via alert rules |
| `critical` | Process cannot continue (config invalid, DB unreachable at startup) | yes |

## 6. Data classification for logs (ASVS 14.1.1, 16.2.5)

| Class | Examples | Logging rule |
|-------|----------|--------------|
| **Secret** | API keys, JWTs, DB passwords, private keys | Never logged. `SecretStr` + redaction processor that drops keys matching `(?i)(authorization|token|secret|password|api[_-]?key|dsn)` and values matching JWT/PEM patterns. Unit-tested. |
| **Untrusted content** | task notes, LLM prompts and raw outputs | Never logged raw: `sha256` + length only. Raw note text stays in the source system. |
| **Personal** | owner names, recipient addresses | Logged only as task-scoped identifiers (`task_id`, `target.kind`); names appear in the audit table, not in logs. |
| **Internal** | task ids, rule ids, decisions, operation ids | Logged freely. |

## 7. Log protection (ASVS 16.4.2, 16.4.3)

* The application cannot read or alter shipped logs (stdout only).
* Loki sits on a separate network/host in production; Grafana access is by
  role (ops vs security); security stream has its own label and retention.
* The audit table is append-only for the app role (`REVOKE UPDATE,
  DELETE`), verified by an integration test.

## 8. Metrics catalogue

Every family below is created by `infrastructure/telemetry.py` (or the
API observer) at start-up, so an export is complete even before the
first event; `tests/meta/test_observability_config.py` keeps this table,
the code, the alert rules and the dashboard in sync. Label values come
from closed enums or rule ids, never from task data.

| Metric | Type | Labels |
|--------|------|--------|
| `followup_runs_total` | counter | `result` (`completed`, `failed`, `skipped_locked`, `dry_run`) |
| `followup_run_duration_seconds` | histogram | — |
| `followup_tasks_total` | counter | `outcome` (`processed`, `rejected`) |
| `followup_decisions_total` | counter | `decision`, `rule` (`R1`–`R9`) |
| `followup_actions_total` | counter | `outcome` (`enqueued`, `duplicate_suppressed`, `sent`, `unknown`, `dead`, `reconciled`, `approved`, `rejected`) |
| `followup_delivery_attempts_total` | counter | `result` (`accepted`, `retryable`, `permanent`, `unknown`) |
| `followup_outbox_backlog` | gauge | `status`, including `held` for actions awaiting human review (refreshed from the database before each export) |
| `followup_outbox_oldest_age_seconds` | gauge | `status` (how long the earliest due action has waited) |
| `followup_circuit_state` | gauge | `dependency` (`delivery`, `llm:<model>`), `state` |
| `followup_llm_requests_total` | counter | `purpose`, `result` (`ok`, `invalid`, `error`, `unavailable`) |
| `followup_llm_latency_seconds` | histogram | `purpose` |
| `followup_llm_tokens_total` | counter | `purpose`, `direction` (`input`, `output`) |
| `followup_llm_cost_usd_total` | counter | `purpose` |
| `followup_agent_plans_total` | counter | `outcome` (`accepted`, `held`, `model_failure`, `step_budget`, `plan_timeout`) |
| `followup_security_events_total` | counter | `kind` (`note_flagged`, `authn_failed`, `authz_denied`, `rate_limited`, `request_rejected`, …) |
| `followup_api_requests_total` | counter | `status` |
| `followup_api_logging_errors_total` | counter | — |
| `followup_otlp_logs_exported_total` | counter | — |
| `followup_otlp_logs_dropped_total` | counter | — |

**Where they are exported.** The API process serves all families on its
private metrics listener (`:9464`, Prometheus job `followup-app`). A CLI
command writes them to `FOLLOWUP_METRICS_FILE` (node_exporter textfile
collector format) when that variable is set, including outbox gauges
read from PostgreSQL.

## 9. Service level objectives (for the 10k/day discussion)

| SLO | Target | Measured by |
|-----|--------|-------------|
| Follow-up latency: task becomes actionable → message accepted | 99 % < 15 min | outbox `created_at` → `sent_at` |
| Duplicate messages | 0 | receiver replays never create log lines; audit check |
| Dead letters | < 0.1 % of actions / week | `followup_actions_total{outcome="dead"}` |
| LLM fallback rate | < 5 % / day | LLM metrics |
| LLM spend | ≤ daily budget | cost counter |

## 10. Tests for logging (part of the 100 % coverage)

* Every catalogue event is emitted at least once in the scenario suite
  (asserted via `structlog.testing.capture_logs`).
* Redaction: secrets/JWT/PEM never appear in any captured log line (property
  test with generated secrets).
* Log injection: a note containing `\n{"level":"critical"}` produces a
  single, escaped JSON line.
* Error responses contain no stack traces or SQL (API tests on forced
  exceptions).
