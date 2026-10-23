# 05 — Reliability Design

Target guarantee: **effectively-once delivery** = at-least-once dispatch
(transactional outbox) × idempotent receiver (`Idempotency-Key`) ×
reconciliation for unknown outcomes. No silent duplicates, no silent losses.

## 1. Identity scheme

| Identity | Formula | Purpose |
|----------|---------|---------|
| `state_fingerprint` | `sha256(canonical_json({status, due_date, owner, notes_sanitized}))` | "Same state" in FR-8. Changes when anything decision-relevant changes. |
| `idempotency_key` | `sha256(canonical_json({task_id, decision, target, state_fingerprint}))` | Dedupe across runs (`UNIQUE` in DB). |
| `operation_id` | `uuid5(NAMESPACE_FOLLOWUP, idempotency_key)` | Sent as `Idempotency-Key` header; stable even after DB loss. |
| `run_id`, row ids | PostgreSQL 18 `uuidv7()` | Time-ordered, index-friendly, sortable in logs. |

`canonical_json` = `json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` over already-normalized values, so whitespace/case
variations in the source do not create new keys (property-tested).

Not in the key: `last_update`, message text, run id, policy version. Message
wording may change (LLM, template edits) without re-sending; touching a task
without changing its state does not re-notify.

## 2. Transaction boundaries

| Step | Transaction | Crash here means |
|------|-------------|------------------|
| Fetch + normalize | none (read-only) | Run marked failed; rerun is safe. |
| Per-task persist: decision + action(`pending`) | **one TX per task** (`INSERT … ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`) | Either both rows exist or neither; rerun recreates the same key. |
| Claim batch | short TX (`FOR UPDATE SKIP LOCKED`, set `in_flight`, lease and fencing token) | Lease expiry or cancellation → `unknown`; lookup before resend. |
| HTTP send | **outside** any DB TX | Never hold DB locks during network I/O. |
| Apply outcome | short TX | Lost update → lease expiry → resend with same key → receiver replays. |

## 3. Retry policy

| Operation | Layer | Retry on | Attempts / deadline | Backoff |
|-----------|-------|----------|---------------------|---------|
| `GET /v1/tasks` | HTTP adapter (stamina) | 429, 502, 503, 504, connect/read timeout | 4 / 20 s | exp. base 0.5 s, cap 8 s, full jitter; `Retry-After` honoured (capped 10 s) |
| `POST /v1/messages` | Dispatcher (durable, `next_attempt_at`) | 429, 502, 503, 504, connect error | 6 per action | exp. base 2 s, cap 5 min, full jitter; `Retry-After` honoured |
| POST read-timeout | — | **not retried** → `unknown` → reconcile | — | — |
| `GET /v1/messages/{op}` | Reconciler | 5xx / timeout | next cycle | same as dispatcher |
| LLM call | Router only | timeout, 429, 5xx | One attempt per configured route within the language-call total deadline | Next route; then validated fallback |

**No flooding** (S5):

* Per-operation **retry allocation**: `RetryBudget(requests)` permits at
  most `floor(requests / 5)` retry admissions. The CLI allocates 15 request
  units to source fetching, allowing its three retries. This is a fixed
  allocation, not a claim that retries remain below 20% of requests
  actually observed. When delivery retry admission is exhausted, work is
  durably deferred before network I/O.
* **Circuit breaker** per dependency (LLM, delivery): opens after 5
  consecutive failures, half-open after 30 s with a single probe. Open LLM
  breaker ⇒ template drafting + keyword-only interpretation (degraded but
  safe). Open delivery breaker ⇒ dispatcher stops claiming.
* **Concurrency limits**: the LLM router uses a semaphore of 4. The
  dispatcher currently sends serially; multiple workers claim disjoint
  rows with PostgreSQL `SKIP LOCKED` and fenced ownership.
* Never retry 400/401/403/404; **422** on idempotency-key payload mismatch
  is a **bug signal** → `dead` + error log + alert metric. **409** (a request
  with the same key is still being processed) is retryable.

## 4. Mock API fault injection (deterministic)

`techcom_mocks` accepts a scripted fault plan so every scenario is
reproducible in tests and in the live demo:

```text
FOLLOWUP_MOCK_FAULTS="tasks:429@1,tasks:503@2,messages:timeout_after_commit@1"
```

| Fault | Behaviour |
|-------|-----------|
| `429` | Respond 429 with `Retry-After: 1`. |
| `503` / `500` | Transient error, nothing committed. |
| `timeout_before_commit` | Sleep past client timeout, **nothing** committed. |
| `timeout_after_commit` | Commit message + log line, then sleep past client timeout. |
| `malformed` | 200 with a non-JSON body. |
| `reset` | Close connection without response. |
| `non_idempotent` | Receiver **ignores** `Idempotency-Key` (models a provider without idempotency support) — used to prove that reconcile-before-resend alone prevents duplicates (`16` #4). |

Receiver semantics (mirrors Stripe and the IETF `Idempotency-Key` draft):
first request with a key stores status + body; repeats with the same key and
same payload hash return the stored response with header
`Idempotent-Replayed: true`; same key + different payload → **422**; same
key while the first request is still in progress → **409**; keys retained
≥ 24 h.

## 4a. Concurrency and lifecycle controls (added after `16`)

| Control | Mechanism | Test |
|---------|-----------|------|
| Single active run | `SELECT pg_try_advisory_lock(hashtext('followup.run'))` at run start; if false → `run.skipped_locked`, exit 0 | two concurrent runs → one processes, one skips |
| Reconcile-before-resend | An `unknown` action can only leave that state through `lookup()`; the dispatcher's claim query excludes `unknown` | S6 in `non_idempotent` mode → exactly one message |
| Kill switch | `FOLLOWUP_DISPATCH_ENABLED` read per claim cycle (hot-reload via config file watch or restart) | switch off mid-run → no new attempts |
| Graceful shutdown | SIGTERM → stop claiming → wait ≤ 25 s for in-flight sends → release leases of unfinished rows (`pending`, attempts unchanged) → exit 0; `init: true`, `stop_grace_period: 30s` | send SIGTERM during a slow fake send |
| Request correlation | inbound `X-Request-ID` (validated UUID, else generated) bound to logs and forwarded on outbound calls | log capture test |
| Retention | `followup maintenance prune` deletes LLM cache > 30 d and terminal actions/audit beyond policy, in batches with `LIMIT` | integration test (as built, M12: `config/retention.toml`, maintenance role from migration `0003`, contract tests on both repositories, runbook `18`) |
| Dry run | `followup run --dry-run` computes and reports decisions inside a rolled-back transaction | asserts zero rows persisted |
| Saved runs need a database | `followup run` without `FOLLOWUP_DATABASE_URL` accepts only `--dry-run`; memory storage is limited to `followup demo`, which reruns inside one process | `tests/unit/cli/test_cli.py::test_saved_run_without_a_database_is_refused` |
| Human review hold | A planned action marked `hold_for_review` is stored as `held`; claims select only `pending`/`unknown`, so no automated path sends it; `followup review --approve` moves it to `pending` with the same operation identity, `--reject` to `dead`, each with an audit event (ADR-0018, migration `0007`) | `tests/contract/persistence/test_review_hold.py`, `tests/unit/processing/test_review_flow.py` |
| One escalation per unchanged state | Notification slots carry a stage (`notify`/`escalate`) from the policy baseline: an untouched overdue task still escalates once after the threshold (ADR-0017) | `tests/unit/processing/test_llm_hardening.py::test_unchanged_overdue_task_still_escalates_after_threshold` |

## 5. Failure-mode & effects analysis (FMEA)

| # | Failure | Detection | Effect without control | Control | Test |
|---|---------|-----------|------------------------|---------|------|
| F1 | Task API 429 | status code | run fails / hammering | backoff + `Retry-After` + budget | `test_s5_rate_limited_fetch_recovers` |
| F2 | Task API 5xx persistent | attempts exhausted | partial run | run marked `failed`, zero actions written, exit code 2 | `test_fetch_exhausted_fails_cleanly` |
| F3 | Malformed task payload | JSON/shape validation | crash | record-level rejection; run continues | `test_malformed_record_rejected` |
| F4 | LLM timeout / 5xx | exception | stuck run | 1 retry → fallback; breaker | `test_llm_timeout_falls_back` |
| F5 | LLM malformed / schema-invalid JSON | parse/validation | unsafe action | fallback `UNCLEAR` / template | `test_s7_malformed_llm_output_is_safe` |
| F6 | LLM ungrounded evidence (hallucination) | substring check | wrong routing | treat as `UNCLEAR` | `test_ungrounded_evidence_rejected` |
| F7 | Prompt injection in note | screen + structure | mass "complete" | no capability + flag | `test_s8_prompt_injection_is_data` |
| F8 | Duplicate run / concurrent runs | `UNIQUE` conflict | duplicate messages | `ON CONFLICT DO NOTHING` | `test_s4_second_run_no_duplicates`, `test_concurrent_runs_single_action` |
| F9 | Delivery timeout after commit | read timeout | duplicate message | `unknown` → reconcile; key replay | `test_s6_timeout_after_commit_no_duplicate` |
| F10 | Delivery timeout before commit | read timeout + lookup 404 | lost message | reconcile 404 → `pending` → resend same key | `test_timeout_before_commit_is_resent_once` |
| F11 | Dispatcher crash after send, before DB update | lease expiry | duplicate | resend same key → replay | `test_crash_after_send_replayed` |
| F12 | Two dispatchers | `SKIP LOCKED` | double send | row-level claim | `test_parallel_dispatchers_disjoint` |
| F13 | Permanent 4xx | status code | infinite retry | `dead` + audit + metric | `test_permanent_error_goes_dead` |
| F14 | Key reused with different payload | 422 | silent overwrite | `dead` + alert (bug) | `test_key_payload_mismatch_flagged` |
| F15 | Clock / timezone drift | injected clock | off-by-one deadlines | UTC dates, `as_of` explicit | `test_due_today_not_overdue` |
| F16 | DB unavailable | connection error | partial state | fail fast, readiness probe red, no sends without persisted action | `test_db_down_no_side_effects` |
| F17 | Secrets in logs | log scanning test | leak | `SecretStr`, redaction processor | `test_logs_redact_secrets` |
| F18 | Many workers exceed the LLM budget together | shared ledger refuses | cost blow-up | worst-case reservation in a PostgreSQL ledger before each call; fail closed when unreachable | `test_concurrent_workers_cannot_overshoot_the_limit` |
| F19 | Second CLI process without a database | refused before processing | re-enqueued actions | saved runs require PostgreSQL | `test_saved_run_without_a_database_is_refused` |

## 6. Observability for diagnosis

* **Structured logs** (structlog JSON): every line has `run_id`, and where
  relevant `task_id`, `rule_id`, `idempotency_key` (short), `operation_id`,
  `attempt`, `outcome`. Notes are never logged raw — only length + hash.
* **Audit trail**: `audit_events` table + `samples/audit_log.jsonl` export:
  `run.started`, `task.rejected`, `note.flagged`, `llm.fallback`,
  `decision.made`, `action.enqueued`, `action.duplicate_suppressed`,
  `delivery.attempt`, `delivery.unknown`, `delivery.reconciled`,
  `delivery.dead`, `run.finished`.
* **Metrics**: `followup_decisions_total{decision,rule}`,
  `followup_actions_total{outcome}`, `followup_delivery_attempts_total{result}`,
  `followup_retries_total{dependency}`, `followup_llm_requests_total{result}`,
  `followup_llm_tokens_total{direction}`, `followup_llm_cost_usd_total`,
  `followup_circuit_state{dependency}`, `followup_outbox_backlog{status}`.
* **Run report** (JSON): counts per decision, duplicates suppressed, LLM
  calls/fallbacks/tokens/estimated cost, rejected records with reasons,
  durations.

Debugging recipe for "a message was sent twice" (live-review drill):
`grep operation_id samples/delivery_log.jsonl` → compare keys → if keys
differ, the fingerprint changed (diff the two `decisions` rows) → if keys are
equal, the receiver dedupe failed (mock bug) → audit trail shows each
attempt and outcome in order.
