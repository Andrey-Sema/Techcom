# 00 — Requirements Analysis

> Source: *TechCom — Final Technical Assessment: AI Operations & Follow-up
> Service* (PDF) and `fixtures/tasks.json`. Evaluation date is fixed at
> **2026-10-21 (UTC)**.

## 1. Problem statement (one paragraph)

Field-construction tasks live in a work-management system (today: a JSON
fixture behind a mock API; tomorrow: monday.com). Many tasks silently go
stale, overdue or blocked. We need a **Python service** that reads tasks,
decides deterministically which ones need attention, uses an **LLM only where
language understanding adds value** (ambiguous notes, message drafting), and
emits **follow-up actions that are safe to run repeatedly**: no duplicates,
no invented facts, no unsafe automatic actions — even when APIs throttle,
fail, or time out after a successful write.

## 2. Functional requirements (traceable IDs)

| ID | Requirement (from brief) | Interpretation / design consequence |
|----|--------------------------|-------------------------------------|
| FR-1 | Load tasks from the JSON fixture **through a local/mock API adapter** | Fixture is served by a mock HTTP API; the service consumes it through a `TaskSource` port + HTTP adapter (never reads the file directly in the main path). |
| FR-2 | Normalize task IDs, status, owner, due date, last update, notes | Dedicated `Normalizer` producing an immutable `Task` value object or a `RejectedRecord` with reasons. |
| FR-3 | Classify into **no action / follow-up owner / clarify missing info / escalate** | Closed enum `DecisionKind`. The example output also uses `request_eta`, so it is modelled as a first-class decision (specialised routed follow-up, scenario 2). |
| FR-4 | **Deterministic rules** for dates/statuses | Pure `DecisionPolicy` (no I/O, injected clock), ordered rule table, versioned. |
| FR-5 | LLM for ambiguous notes **or** message drafting; LLM must **not invent owners, dates or status** | LLM output schema has *no fields* for owner/date/status (capability removal). Drafts use placeholders filled by code. Post-validation rejects any unknown names/dates. |
| FR-6 | Output JSON record: `task_id, decision, recipient/target, reason, proposed message` | `ActionRecord` schema (Pydantic, JSON Schema exported) + run report. |
| FR-7 | Simulate sending via **mock delivery log**; no real emails | Mock Delivery API writes an append-only `delivery_log.jsonl`; no SMTP/SMS code exists. |
| FR-8 | Persist state to avoid duplicate follow-ups for the same **task/action/state** | Idempotency key = hash(task_id, decision, target, state fingerprint) with a `UNIQUE` constraint in PostgreSQL. |
| FR-9 | Handle API errors, invalid LLM output, timeout where the op may have succeeded | Bounded retries with jitter + `Retry-After`; schema validation + safe fallback; stable operation identity (`Idempotency-Key`) + reconciliation (`GET /messages/{operation_id}`). |

## 3. Required scenarios (acceptance anchors)

| # | Scenario | Expected behaviour | Fixture example |
|---|----------|--------------------|-----------------|
| S1 | Overdue task with no explanation | `follow_up_owner` → owner | TC-1001 |
| S2 | Overdue, blocked by materials | `request_eta` → warehouse / procurement, **not** a generic completion reminder | TC-1002 (TC-1009 is due today) |
| S3 | Completed task | `no_action` | TC-1003, TC-1010 |
| S3b | Missing owner or due date | `clarify_missing_info`, never guess | TC-1004 (owner), TC-1005 (due date) |
| S4 | Second run, unchanged data | Zero new outbound actions | all |
| S5 | Mock API returns 429 / transient 5xx | Bounded retries + backoff, honours `Retry-After`, no flooding | fault profile |
| S6 | Timeout **after** the mock write committed | No duplicate write: same `Idempotency-Key` and/or reconciliation | fault profile |
| S7 | Malformed / unexpected LLM JSON | Validation failure → safe deterministic fallback, no unsafe action | fault profile |
| S8 | Prompt injection in notes | Treated as untrusted data; flagged; cannot change state | TC-1006 |

## 4. Non-functional requirements

| ID | Category | Requirement |
|----|----------|-------------|
| NFR-1 | Determinism | Same input + same `as_of` date ⇒ identical decisions (LLM responses cached by content hash; stub in tests). |
| NFR-2 | Safety | No code path exists that changes task status/owner/dates in the source system. The only side effect is a *message proposal* delivered to a mock channel. |
| NFR-3 | Reliability | At-least-once dispatch + idempotent receiver ⇒ effectively-once delivery. Every action ends in a terminal or explicitly retryable state. |
| NFR-4 | Observability | Structured JSON logs with `run_id`, `task_id`, `operation_id`; audit trail; run report; Prometheus metrics. |
| NFR-5 | Security | **OWASP ASVS 5.0 Level 2** baseline + OWASP Cheat Sheets + OWASP LLM Top 10 (`15`); secrets via Docker secrets / secret manager; `.env.example` without credentials; short-lived service JWTs; LLM has no tools; minimal data sent to LLM. |
| NFR-11 | Observability | Log inventory, structured security events, Prometheus metrics + alerts, Grafana dashboards, Loki log shipping (`14`). |
| NFR-6 | Quality | Python ≥ 3.13, strict PEP 8 (ruff, 79 cols), `mypy --strict`, **100 % line + branch coverage** gate. |
| NFR-7 | Reproducibility | `uv.lock`, one-command bootstrap (`make demo`), Docker Compose with PostgreSQL 18. |
| NFR-8 | Cost | LLM is called only when deterministic rules cannot decide; per-run token/cost metrics. |
| NFR-9 | Explainability | Each decision carries `reason_code`, human `reason`, `rule_id`, `policy_version` and evidence. |
| NFR-10 | Modifiability | Thresholds, routing and status synonyms are configuration; rules are a single ordered table (live-review friendly). |

## 5. Rubric → evidence map

| Rubric criterion (points) | Where we earn it |
|---------------------------|------------------|
| Python, API & code structure (20) | Hexagonal layout, typed ports, FastAPI + Typer adapters, pydantic-settings config, resilient HTTP client — `02-architecture.md` |
| AI integration & validation (20) | Gemini structured output, Pydantic strict schemas, grounding checks, placeholder drafting, cache, circuit breaker — `04-decision-policy-and-llm.md` |
| Business logic & automation (20) | Rule table, routing table, dedupe, scenario tests — `04`, `07` |
| Production reliability (20) | Outbox state machine, idempotency keys, reconciliation, retries, budgets, security — `05-reliability.md` |
| Tests & documentation (10) | 100 % branch coverage, scenario tests, property tests, golden files, README — `06-test-strategy.md` |
| Live technical review (10) | Demo script, fault drills, 10k/day talk track — `10-live-review-playbook.md`, `09-operations-at-scale.md` |

> **Hard gate from the brief:** *"a submission that can silently duplicate or
> perform unsafe actions will not be considered production-ready regardless of
> its total score."* Every architectural decision below is checked against
> this sentence first.

## 6. Fixture analysis (as_of = 2026-10-21)

| Task | Status | Due | Δ days | Owner | Note signal | Expected decision | Target | Rule |
|------|--------|-----|--------|-------|-------------|-------------------|--------|------|
| TC-1001 | Working on it | 10-18 | −3 | John Smith | "No update since Monday." → no blocker | `follow_up_owner` | John Smith | R7 |
| TC-1002 | Blocked | 10-18 | −3 | Maria Cruz | materials, ETA unknown | `request_eta` (priority high) | warehouse | R5 |
| TC-1003 | Done | 10-19 | −2 | Lee Nguyen | — | `no_action` | — | R1 |
| TC-1004 | Not Started | 10-25 | +4 | **null** | owner not assigned | `clarify_missing_info` (owner) | Wisconsin Build coordinator | R3 |
| TC-1005 | Working on it | **null** | — | Alex Brown | due date unconfirmed | `clarify_missing_info` (due_date) | Alex Brown | R3 |
| TC-1006 | Working on it | 10-20 | −1 | Jamie Lee | **prompt injection** | `follow_up_owner` + flag `prompt_injection_suspected`; template message; note not sent to LLM | Jamie Lee | R2 → R7 |
| TC-1007 | Not Started | 10-27 | +6 | Taylor Reed | scheduled next week | `no_action` | — | R9 |
| TC-1008 | Working on it | 10-15 | −6 | Chris Patel | "unable to proceed until upstream splice confirmed" → upstream dependency (status/notes mismatch) | `escalate` | SURF Illinois project manager | R6 |
| TC-1009 | Blocked | 10-21 | 0 | Dana Fox | backorder, no ETA → materials | `request_eta` | warehouse | R5 |
| TC-1010 | Done | 10-17 | −4 | Morgan Bell | — | `no_action` | — | R1 |

Observations a reviewer will look for:

* **TC-1008** is the trap for naive status-only logic: status says *Working on
  it* but the note says it cannot proceed. This is exactly where the LLM
  (or the deterministic keyword pre-classifier) earns its place.
* **TC-1009** is due *today* — not overdue. Off-by-one in date math is the
  most likely "injected bug" during live review; tests pin `Δ = 0`.
* **TC-1006** must still be followed up (it *is* overdue); the injection only
  changes *how* we treat the note, not whether the task needs attention.
* **TC-1002** `last_update` (10-20) is after `due_date` (10-18): the update
  does not "un-overdue" the task.

## 7. Ambiguities and explicit assumptions

| # | Ambiguity | Assumption (documented in README) |
|---|-----------|-----------------------------------|
| A1 | Who is the target when owner is missing? | Project coordinator from `config/routing.toml`; fallback `operations_desk`. Never an LLM guess. |
| A2 | Who handles material ETAs? | Routing table `blocker → team` (`materials → warehouse`). |
| A3 | Is `request_eta` a decision or an action? | First-class `decision` value (matches the brief's example output); category = routed follow-up. |
| A4 | Deadline semantics | `due_date` is inclusive: overdue iff `due_date < as_of`. All dates are UTC calendar dates. |
| A5 | "Same state" for dedupe | Fingerprint of decision-relevant fields: normalized `status`, `due_date`, `owner`, `notes`. `last_update` is excluded so that merely touching a task does not re-spam (re-reminder cadence is a documented production follow-up). |
| A6 | Escalation threshold | `escalate_after_days_overdue = 5` (config). |
| A7 | Unknown status values in unseen fixtures | Normalize synonyms (case/whitespace/aliases); truly unknown → `clarify_missing_info` (status) to owner, never guessed. |
| A8 | Duplicate `task_id` in a fixture | Deterministic resolution: keep the record with the latest `last_update`; others rejected with reason `duplicate_task_id`. |
| A9 | Done but notes say otherwise | `Done` wins (status is source of truth); note contradiction is logged as a data-quality warning, no outbound message. |
| A10 | LLM provider availability | Gemini adapter is the real integration; a deterministic stub is default for tests/offline demo; selection by `LLM_PROVIDER`. |
