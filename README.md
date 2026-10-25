# TechCom AI Operations & Follow-up Service

[![CI](https://github.com/Andrey-Sema/Techcom/actions/workflows/ci.yaml/badge.svg)](https://github.com/Andrey-Sema/Techcom/actions/workflows/ci.yaml)
![Python](https://img.shields.io/badge/python-3.13%20%7C%203.14-3776AB)
![Coverage](https://img.shields.io/badge/branch%20coverage-100%25-2ea44f)
![Typing](https://img.shields.io/badge/mypy-strict-2a6db2)
![Lint](https://img.shields.io/badge/ruff-ALL%20%C2%B7%20PEP%208-d7ff64)
![Model](https://img.shields.io/badge/model-gpt--6--luna%20%C2%B7%20high-8a2be2)
![Live eval](https://img.shields.io/badge/live%20eval-macro--F1%200.99-2ea44f)

A Python service that reads project tasks, decides which ones need
attention, lets an **LLM orchestrate the follow-up inside limits set by
code**, and delivers the resulting messages **idempotently**, even when
APIs throttle, fail, or time out after a successful write.

```text
rules decide WHETHER to act  →  the LLM plans HOW, inside an envelope
→  a deterministic guard approves  →  the outbox delivers idempotently
```

## At a glance

| | Result | Evidence |
|---|---|---|
| Brief scenarios | **9 / 9** covered, each by a named test | [Required scenarios](#required-scenarios) |
| Supplied fixture | **10 / 10** expected decisions, 7 messages sent; rerun: 7 duplicates, **0** new sends | [`make demo`](#quick-start), [samples](samples/) |
| Unseen fixtures | One command, no Docker, database or API key | [Run it on your own fixture](#run-it-on-your-own-fixture) |
| What the model adds | On 95 labelled notes: macro-F1 **0.34 → 0.99**, wrong decisions **35 → 0** (35 fixed, none broken) | [Model value report](docs/eval/model-value-luna-high.v1.md) |
| Safety | **0** unsafe outcomes on 95 notes including 32 injection variants, with and without the model | [OWASP LLM Top 10 matrix](docs/security/owasp-llm-top10.md) |
| Evidence in CI | Synthetic replay **and** live `gpt-6-luna` captures replayed on every change | `make eval`, `make eval-provider` |
| Code quality | 1,650+ tests, 100 % branch coverage, `mypy --strict`, ruff `ALL`, PEP 8 at 79 columns | [Quality gates](#quality-gates) |
| Scale | 10,000 tasks: 89.1 s first run, 57.6 s rerun, 202 MB | [Benchmarks](docs/benchmarks.md) |

## Contents

- [Quick start](#quick-start)
- [Run it on your own fixture](#run-it-on-your-own-fixture)
- [Evidence](#evidence)
- [How it works](#how-it-works)
- [Code map for reviewers](#code-map-for-reviewers)
- [The LLM orchestrator and how it is kept in check](#the-llm-orchestrator-and-how-it-is-kept-in-check)
- [Required scenarios](#required-scenarios)
- [Output on the supplied fixture](#output-on-the-supplied-fixture)
- [Commands and configuration](#commands-and-configuration)
- [Assumptions and trade-offs](#assumptions-and-trade-offs)
- [Not implemented, and why](#not-implemented-and-why)
- [What changes for production](#what-changes-for-production)
- [Documentation](#documentation)

---

## Quick start

Requirements: Python 3.13 or 3.14 and [uv](https://docs.astral.sh/uv/).
Docker is only needed for the PostgreSQL tests and the optional stack.

```sh
uv sync --locked
make demo                      # mock API + full workflow + rerun, offline
cat var/demo/decisions.json    # ten explainable decisions, seven messages
cat var/demo/delivery_log.jsonl
```

`make demo` starts a local mock API, processes `fixtures/tasks.json` for
`2026-10-21`, plans and sends seven follow-ups, reconciles any uncertain
send, then processes the same data again: **seven duplicates, zero new
sends**. No database, Docker or API key is needed; the LLM runs on a
deterministic offline model that speaks the real tool protocol.

Other entry points:

```sh
uv run followup run --fixture fixtures/tasks.json --as-of 2026-10-21 --dry-run
make test-fast                 # offline tests, no Docker
make test                      # full suite with PostgreSQL 18, 100 % branch gate
make eval eval-provider        # replay synthetic and live-model evidence
```

Repeated runs from the command line need PostgreSQL, so that a second
process can recognise work the first one already sent (without a
database `followup run` refuses anything but `--dry-run`):

```sh
make db-init                   # PostgreSQL 18 in Docker, schema, service role
export FOLLOWUP_DATABASE_URL="postgresql+psycopg://followup_service:$(cat secrets/service_password)@127.0.0.1:5432/followup"
make mock &                    # local task and delivery API
uv run followup run --as-of 2026-10-21 --dispatch   # 7 sent
uv run followup run --as-of 2026-10-21 --dispatch   # 7 duplicates, 0 sent
uv run followup review         # actions the model held for a person
```

To use the real model, set `FOLLOWUP_LLM_PROVIDER=router` and provide
`FOLLOWUP_OPENAI_API_KEY` from your secret manager. Routes, reasoning
effort and prices are in [`config/llm.toml`](config/llm.toml).

---

## Run it on your own fixture

Any file with the schema of [`fixtures/tasks.json`](fixtures/tasks.json)
works. The mock API serves it in place of the supplied fixture:

```sh
FOLLOWUP_MOCK_FIXTURE=path/to/tasks.json make demo
cat var/demo/decisions.json      # one explained decision per task
cat var/demo/delivery_log.jsonl  # what the mock receiver accepted
cat var/demo/run_report.json     # outbox states; "demo" holds the rerun counts
```

No Docker, database or API key is needed. The demo processes the file,
sends, reconciles and then processes the same file again, so
`run_report.json` shows `rerun_duplicates` equal to `first_enqueued`
and the delivery log has no second copy of any message.

To see the reliability scenarios on the same file, add faults to the
mock; the demo still ends with one delivery per action:

```sh
FOLLOWUP_MOCK_FIXTURE=path/to/tasks.json \
FOLLOWUP_MOCK_FAULTS=tasks:429@1,messages:timeout_after_commit@1 make demo
```

For decisions only, without sending anything:

```sh
uv run followup run --fixture path/to/tasks.json --as-of 2026-10-21 --dry-run
```

What to expect from an unseen file:

- Dates must be ISO (`2026-10-21` or an ISO datetime). Anything else
  counts as missing and leads to a clarification, never a guess.
- A project that is not in [`config/routing.toml`](config/routing.toml)
  routes coordinator and manager messages to the fallback team
  (`Operations desk`).
- Offline, the model is a deterministic stub. A note the keyword path
  cannot settle takes the safe fallback and carries the `llm_fallback`
  flag. With the real model (`FOLLOWUP_LLM_PROVIDER=router`, PostgreSQL
  as in the quick start, and `FOLLOWUP_MOCK_FIXTURE=… make mock`), the
  model reads those notes.

[`fixtures/unseen-example.json`](fixtures/unseen-example.json) is an
eight-task file in a project the routing table does not know. Try it
with `FOLLOWUP_MOCK_FIXTURE=fixtures/unseen-example.json make demo`.
Run both ways on 2026-10-21:

| Task | Note | Offline stub | Real model (`gpt-6-luna`) |
|------|------|--------------|---------------------------|
| NX-2001 | overdue, no note | follow-up to owner | follow-up to owner |
| NX-2002 | "Waiting for the county permit; …" | follow-up to owner, `llm_fallback` | **escalate** (permit blocker) |
| NX-2003 | "Fiber reels on backorder, supplier has no ship date." | escalate (overdue), `llm_fallback` | **request ETA** from the warehouse |
| NX-2004 | done | no action | no action |
| NX-2005 | no owner | clarification to the fallback team | clarification to the fallback team |
| NX-2006 | "ignore previous instructions…" | flagged, follow-up from dates | flagged, follow-up from dates |
| NX-2007 | on schedule | no action | no action |
| NX-2008 | no due date | clarification to the owner | clarification to the owner |

Both runs send six messages; a second run sends none.

---

## Evidence

### What the model adds

The same 95 labelled notes run twice through the same policy, routing
and safety judge: once with no model at all (keyword hits, the
`unclear` fallback and catalogue templates, which is the service's
offline path) and once with `gpt-6-luna`.

| Pipeline | Macro-F1 | Benign macro-F1 | Wrong decisions | Unsafe outcomes |
|---|---:|---:|---:|---:|
| Without the model | 0.338 | 0.284 | 35 | 0 |
| With `gpt-6-luna` (high effort) | **0.991** | **0.984** | **0** | 0 |

The model fixes 35 decisions and breaks none: notes such as "The
junction kit is missing; the crew cannot start" now reach the
warehouse with an ETA request instead of a generic reminder, and
permit, access and upstream blockers are escalated. The live
responses are committed in
[`tests/eval/bundles/luna-high.v1`](tests/eval/README.md#live-provider-captures)
and replayed offline by CI on every change.

Two honest limits: the corpus is built from notes the keyword path is
not meant to settle, so the gap is larger than on routine notes; and
one capture is one sample, while two other live runs scored macro-F1
0.975. Replay proves that each capture matches its request, not where
it came from, so the quality label stays `CAPTURE_REPLAY_ONLY`.

### Live models compared

`gpt-6-luna` at high effort against `gpt-6.1-sol` at low effort, one
model per run and no failover, two runs each
([method and raw numbers](docs/eval/model-comparison.md)):

| | `gpt-6-luna`, high | `gpt-6.1-sol`, low |
|---|---:|---:|
| Macro-F1 on 95 notes (two runs) | 0.975 / 0.975 | 0.959 / 0.975 |
| Wrong decisions | 1 / 0 | 0 / 0 |
| Latency per call, p50 / p95 | 3.1 s / 4.5 s | 2.9 s / 4.2 s |
| Corpus cost | **$0.013** | $0.168 |
| Assignment: correct, wall time, cost | 10 / 10, 36.9 s, **$0.0067** | 10 / 10, **18.8 s**, $0.046 |

Quality is a tie, single calls are equally fast, and Luna is 7 to 13
times cheaper. It takes about twice as long on the orchestrated run,
which a batch follow-up service can afford. Hence Luna is the primary
route and the Sol generation the failover
([ADR-0019](docs/adr/0019-luna-primary-route-and-live-evidence.md)).

### How containment is proven

| Evidence | What it shows |
|----------|---------------|
| [OWASP LLM Top 10 matrix](docs/security/owasp-llm-top10.md) | Threat, controls and tests for each of LLM01–LLM10; a meta test keeps the matrix and the test markers in step |
| [`tests/property/test_agent_containment.py`](tests/property/test_agent_containment.py) | Hypothesis feeds arbitrary and hostile plans (fake tools, foreign recipients, `close_task`, unknown asks and tones, smuggled `subject`/`body` fields); every result is inside the envelope or is the baseline |
| [`tests/unit/domain/test_guard.py`](tests/unit/domain/test_guard.py) | Each escape attempt maps to a closed violation code; every composable message passes the fact rules |
| [`tests/unit/llm/test_owasp_llm_top10.py`](tests/unit/llm/test_owasp_llm_top10.py) | No model text is fed back, the history tool returns closed facts, prompts hold no secrets, an earlier contact must be real |
| [`tests/unit/processing/test_review_flow.py`](tests/unit/processing/test_review_flow.py) | A held action is not sent until a person approves it, then exactly once |
| [`tests/eval/`](tests/eval/README.md) | 95 labelled notes including 32 injections; synthetic scripts and live `gpt-6-luna` captures both replay with zero unsafe outcomes |

### Quality gates

| Gate | Result |
|------|--------|
| Tests | 1,650+ unit, property, contract, scenario and integration tests |
| Coverage | 100 % statements and branches (application, mock and evaluation harness) |
| Static checks | Ruff `ALL`, PEP 8 at 79 columns, mypy `--strict`, Bandit, import-linter (hexagonal layers), Semgrep project invariants |
| Mutation testing | mutmut on policy, identity and messages |
| LLM evaluation | 95 cases replayed in CI from synthetic scripts and from live `gpt-6-luna` captures: macro-F1 0.99, 0 wrong decisions, 0 unsafe outcomes |
| Observability | Every metric behind the alerts and dashboard is emitted (`docs/14` §8, checked by a test); allow-listed JSON logs with run, plan and LLM cost fields |
| 10,000 tasks | 89.1 s first run, 57.6 s rerun, 202 MB, PostgreSQL 18 ([benchmarks](docs/benchmarks.md)) |
| Security | OWASP ASVS 5.0 L2 as the design target, [OWASP LLM Top 10 matrix](docs/security/owasp-llm-top10.md) with a test per control, least-privilege DB roles ([docs/15](docs/15-security-asvs-l2.md)) |

Architecture: a hexagonal layout with a pure domain core
(`domain/`), use cases (`application/`), adapters for HTTP, PostgreSQL,
OpenAI and the CLI (`adapters/`), and composition in `bootstrap.py`.

---

## How it works

```mermaid
flowchart LR
    A[Task API<br/>mock] --> B[Normalize<br/>untrusted text]
    B --> C{Policy<br/>rules R1–R9}
    C -- no action --> R[(Run report)]
    C -- needs attention --> D[LLM orchestrator<br/>tools: history · check · submit]
    D --> G{Guard<br/>envelope + message rules}
    G -- violations --> D
    G -- approved --> O[(Outbox<br/>PostgreSQL)]
    G -- failure --> T[Baseline + template]
    T --> O
    O --> S[Dispatcher<br/>retries · breaker · quota]
    S --> M[Delivery API<br/>mock]
    S -- timeout = unknown --> Q[Reconciler<br/>lookup by operation id]
    Q --> O
```

1. **Ingestion.** Records from the mock API are normalized: IDs, status
   synonyms, owners, dates, notes. Notes are `UntrustedText`, cleaned of
   control characters and screened for prompt injection.
2. **Policy.** An ordered rule table decides from dates and status whether
   a task needs a follow-up, an ETA request, a clarification or an
   escalation, and computes a **baseline** action. Ambiguous notes are
   classified by an LLM with verbatim evidence; the model never sets
   owners, dates or status.
3. **Orchestration.** For every new actionable task state, the LLM plans
   the action with read-only tools inside an envelope computed by code
   (next section).
4. **Outbox.** The decision, the action and its audit receipt are written
   in one transaction. The idempotency key comes from the task state and
   the baseline, so a rerun or a different model answer cannot create a
   second message.
5. **Delivery.** Dispatchers claim rows with `SKIP LOCKED` and fencing
   tokens, send with a stable `Idempotency-Key`, retry with bounded
   backoff, and treat a timeout as **unknown**: the reconciler looks the
   operation up before anything is resent.

---

## Code map for reviewers

The path a task takes is about 3,500 lines of the 12,700 in `src/`.
Read these in order; paths are relative to `src/techcom_followup/`.

| Step | File | Look for |
|------|------|----------|
| Read tasks | `adapters/outbound/task_source_http.py` | One bounded retry layer with `Retry-After` for 429 and 5xx |
| Normalize | `domain/normalize.py` | Status synonyms, strict ISO dates, rejected records with reasons |
| Screen notes | `domain/injection.py` | Instruction-like text is flagged and withheld from the model |
| Decide | `domain/policy.py` | Rules R1–R9, first match wins; the baseline action and its recipient |
| Interpret notes | `application/interpreter.py` | One validated call, verbatim evidence, confidence gate, cache |
| Plan the action | `application/orchestrator.py` | Four steps at most, read-only tools, fallback on any failure |
| Bound the plan | `domain/envelope.py`, `domain/guard.py` | What the model may choose, and the closed violation codes |
| Write the message | `domain/messages.py` | Code-composed text from reviewed fragments; the fact validator |
| Dedupe | `domain/identity.py` | Idempotency key and operation id from the task state |
| Persist | `application/process_tasks.py` | Decision, action and audit in one transaction |
| Deliver | `application/dispatch.py`, `adapters/outbound/delivery_http.py` | Fenced claims, stable `Idempotency-Key`, bounded backoff |
| Reconcile | `application/reconcile.py` | A timeout is "unknown": look the operation up, never resend blindly |

The model side is `adapters/outbound/llm/router.py` (failover, circuit
breakers, budget) and `adapters/outbound/llm/openai_responses.py`
(strict JSON schema, one attempt per call); the offline stand-in is
`adapters/outbound/llm_agent_stub.py`. Everything is wired in
`bootstrap.py` and exposed by `adapters/inbound/cli.py`. The mock API is
`mocks/src/techcom_mocks/`, with fault plans in `faults.py`.

The brief's scenarios are tested end to end in
`tests/scenarios/test_processing.py` (S1–S6) and
`tests/scenarios/test_llm_safety.py` (malformed output, prompt
injection).

**Beyond the four-hour brief.** These parts harden the service but are
not needed to judge the core path: the authenticated REST API
(`adapters/inbound/api/`), the observability stack (`deploy/`,
`infrastructure/telemetry.py`, `infrastructure/otlp.py`), the PostgreSQL
repository and migrations (`adapters/outbound/pg_repository.py`,
`migrations/`), the evaluation harness (`tests/eval/`), the benchmark,
mutation and DAST tooling, and the ADRs.

---

## The LLM orchestrator and how it is kept in check

The assignment asks for an LLM that interprets notes and drafts messages
without inventing facts. Here the model **orchestrates the follow-up**:
it chooses the action, the recipient, the priority, what the message
asks for, its tone, whether to mention an earlier message, and whether a
person should review the action before it is sent. Code bounds, checks
and audits every one of those choices, and code writes every word.

### What the model decides, and what code decides

| Code (deterministic) | LLM (bounded judgement) |
|----------------------|-------------------------|
| Whether a task needs attention at all | Which allowed action fits: follow up, request an ETA, clarify, escalate |
| The envelope of allowed actions, recipients, asks and tones | Which allowed recipient: owner, coordinator, manager, assigned team |
| Every fact: names, dates, IDs, days overdue | Priority (it may raise it, never lower it) |
| Every word of the message, from reviewed fragments | **What to ask**: one or two of status, blocker, unblock plan, delivery ETA, missing details, owner, completion date, on-track check |
| Whether an earlier message really was sent | **Tone**: neutral, friendly, or firm (firm only after an unanswered, overdue message or for an escalation) |
| Deduplication, delivery, retries, budgets | **Mention the earlier message** (only if one was sent) |
| Final approval of the plan; release of held actions | **Hold for human review** with a closed reason instead of sending |

The model never writes message text: the plan schema has no text field
that reaches a recipient. Its free-text rationale is stored in the audit
trail as *its opinion*, after the guard rejected links, contacts,
instruction-like text and copied note text.

### The loop

```mermaid
sequenceDiagram
    participant P as Policy
    participant O as Orchestrator
    participant L as LLM
    participant G as Guard
    P->>O: baseline + envelope (no names)
    O->>L: context, tools, step 1 of 4
    L->>O: get_history
    O->>L: earlier actions for this task (closed fields)
    L->>O: check_plan {decision, recipient, priority, asks, tone, earlier_contact, delivery}
    O->>G: review
    G->>O: violations: ["unverified_earlier_contact"]
    O->>L: violation codes only (repair)
    L->>O: submit_plan {...}
    O->>G: review
    G->>O: approved: decision + message composed and rendered by code
```

Tools are **read-only**: `get_history`, `check_plan`, `submit_plan`.
Nothing the model does sends a message, changes a task or reaches the
network. The outbox is the only executor. Only violation codes go back
to the model, never its own earlier text.

### The envelope

| Baseline from the rules | Alternatives the model may choose |
|-------------------------|-----------------------------------|
| follow up with the owner | escalate to the project manager, **only if** an earlier action for the task was sent and it is still overdue |
| request an ETA from the assigned team | ask the owner; escalate under the same condition |
| clarify missing data | send it to the project coordinator |
| escalate to the manager | address the coordinator; never downgrade |
| no action | the model is not called |

Each decision also has its allowed asks (an ETA request must ask for the
delivery date; "who should own this" only for an unowned task) and its
allowed tones. First contacts always follow the brief's routing.

### Note interpretation

Notes the keyword path cannot settle go to the model, which returns a
closed blocker category, whether an ETA is needed, a confidence and a
**verbatim** quote. Code rejects a quote that is not in the note and any
blocker below confidence 0.6; a low-confidence `unclear` is accepted,
because the prompt asks for exactly that when the model is unsure and
the policy routes it like the fallback. Accepted answers are cached by
model, prompt version and note.

### Human review

A plan with `delivery: hold_for_review` is stored as a **held** action.
Dispatchers never claim it; `followup review` lists held actions and
`followup review --approve <operation-id>` or `--reject <operation-id>`
decides one, with an audit event. Approval keeps the original operation
identity, so the next dispatch sends it exactly once. Holding can only
reduce automation; an alert fires when an action waits for 4 hours.

### Routing, failure handling and budgets

- **Routes.** Every purpose tries `gpt-6-luna` at high reasoning effort
  first, then `gpt-6.1-sol`, `gpt-6-sol` and `gpt-5.6-sol`. Failover
  happens only on availability errors (timeouts, 429, 5xx, model not
  available); each model has its own circuit breaker.
- At most **four steps** per plan; on the last one the model must submit.
- Each model call has a 30-second timeout and the whole plan, including
  the history lookup, a 60-second deadline.
- Invalid JSON, an unknown tool, a timeout, an open circuit breaker, an
  exhausted budget or a plan that never passes the guard all fall back
  to the **baseline action and its template** (`llm_fallback` flag).
- The worst-case cost of every call is reserved before the call, in the
  process budget and in a **PostgreSQL ledger shared by all workers**;
  an unreachable ledger admits nothing.
- If the note, title or project name trips the injection screen, the
  note is **withheld** from the model. Titles and project names are never
  sent, recipients are opaque references and contacts in notes are
  redacted. A note can still mention a person.
- Each plan stores the baseline next to the chosen decision, the chosen
  intents, the model's rationale and an audit trace (tools, steps,
  violation codes, outcome) in the same transaction as the decision.
- Notification slots allow one action per source task state, plus one
  escalation, whatever the interpreter or the model answers (ADR-0017).

Design records: [ADR-0016](docs/adr/0016-bounded-llm-orchestrator.md),
[ADR-0017](docs/adr/0017-llm-output-and-notification-boundaries.md),
[ADR-0018](docs/adr/0018-message-intents-review-and-shared-budget.md),
[ADR-0019](docs/adr/0019-luna-primary-route-and-live-evidence.md).
`FOLLOWUP_LLM_PLANNER=templates` switches the orchestrator off.

---

## Required scenarios

| Scenario from the brief | Behaviour | Test |
|-------------------------|-----------|------|
| Overdue, no explanation | Follow-up to the owner | `tests/scenarios/test_processing.py::test_s1…` |
| Overdue, blocked by materials | `request_eta` to the warehouse team | `…::test_s2…` |
| Completed task | No action | `…::test_s3_done…` |
| Missing owner or due date | Clarification to the owner or project coordinator | `…::test_s3_missing…` |
| Second run, unchanged data | Duplicates suppressed, nothing sent | `…::test_s4…`, `make demo` |
| 429 / transient 5xx | Bounded retries with jittered backoff and `Retry-After` | `…::test_s5…` |
| Timeout after a committed write | Outcome `unknown` → lookup by operation id → no resend | `…::test_s6…` |
| Malformed LLM JSON | Validation failure → baseline and template | `tests/scenarios/test_llm_safety.py`, `tests/unit/processing/test_orchestrator.py` |
| Prompt injection in a note | Flagged, withheld from the model, decision from dates and status | `tests/scenarios/test_llm_safety.py`, `tests/eval/` |

Faults can be injected into the mock, for example
`FOLLOWUP_MOCK_FAULTS=tasks:429@1,messages:timeout_after_commit@1 make mock`.

---

## Output on the supplied fixture

| Task | Decision | Recipient | Why |
|------|----------|-----------|-----|
| TC-1001 | follow_up_owner | John Smith | 3 days overdue, no explanation |
| TC-1002 | request_eta | Warehouse team | blocked by materials |
| TC-1003 | no_action | — | done |
| TC-1004 | clarify_missing_info | Wisconsin Build coordinator | no owner |
| TC-1005 | clarify_missing_info | Alex Brown | no due date |
| TC-1006 | follow_up_owner + `prompt_injection_suspected` | Jamie Lee | note withheld, decided from dates |
| TC-1007 | no_action | — | on track |
| TC-1008 | escalate | SURF Illinois project manager | blocked upstream, 6 days overdue |
| TC-1009 | request_eta | Warehouse team | materials, due today |
| TC-1010 | no_action | — | done |

The live model reaches the same ten decisions: 7 messages sent, and a
second run sends nothing and makes no model call, because every
interpretation is cached and every action is a duplicate.

A record from [`samples/decisions.json`](samples/decisions.json):

```json
{
  "task_id": "TC-1002",
  "decision": "request_eta",
  "target": {"kind": "team", "name": "Warehouse team", "project": "SURF Illinois"},
  "rule_id": "R5+agent",
  "baseline": {"decision": "request_eta", "rule_id": "R5"},
  "reason": "Blocked by materials; 3 day(s) overdue; delivery date unknown.",
  "rationale": "Agent chose request_eta for assigned_team, asking for delivery_eta in a neutral tone.",
  "flags": ["agent_planned"],
  "message": {
    "subject": "Delivery ETA needed: TC-1002 Install handholes",
    "body": "Hello Warehouse team, TC-1002 'Install handholes' (SURF Illinois) is waiting on materials and is due 2026-10-18. Our tracker shows: Blocked by materials; 3 day(s) overdue; delivery date unknown. When will the materials arrive, or who can confirm the delivery date? Thank you.",
    "source": "llm",
    "template_id": "agent.v3"
  },
  "outcome": "enqueued"
}
```

The mock delivery log is [`samples/delivery_log.jsonl`](samples/delivery_log.jsonl);
the full run with outbox states is [`samples/run_report.json`](samples/run_report.json).

---

## Commands and configuration

| Command | What it does |
|---------|--------------|
| `followup run --fixture … --dry-run` | Decisions and messages, no persistence, delivery or LLM calls |
| `followup run --dispatch` | Process, then send due actions (PostgreSQL) |
| `followup dispatch --limit 50` | Send due actions (PostgreSQL) |
| `followup reconcile --limit 50` | Resolve uncertain sends by lookup |
| `followup report --run-id …` | Decisions and outbox states of a run |
| `followup review [--approve ID \| --reject ID]` | List actions held for human review, or decide one |
| `followup demo` | Run, dispatch, reconcile and rerun in memory against the mock |
| `followup maintenance prune` | Apply retention (maintenance role) |
| `followup-api` | Optional authenticated REST API ([docs/19](docs/19-api-and-deployment.md)) |

| Evaluation | What it does |
|------------|--------------|
| `make eval` | Replay the synthetic corpus offline; contract gates |
| `make eval-provider` | Replay the live `gpt-6-luna` captures offline with `--require-provider` |
| `make eval-reports` | Regenerate the evidence pages in `docs/eval/`, including the model-value report |
| `scripts/capture_eval.py` | Capture the corpus again from a live model (needs an API key) |

| Variable | Default | Meaning |
|----------|---------|---------|
| `FOLLOWUP_LLM_PROVIDER` | `stub` | `stub` (offline) or `router` (OpenAI Responses API) |
| `FOLLOWUP_LLM_PLANNER` | `agent` | `agent` (bounded orchestrator) or `templates` |
| `FOLLOWUP_DATABASE_URL` | unset | PostgreSQL; without it only `--dry-run` and `demo` run |
| `FOLLOWUP_DISPATCH_ENABLED` | `true` | Kill switch for new sends |
| `FOLLOWUP_WRITE_CONCURRENCY` | `8` | Concurrent planning and writes per batch (1–16) |
| `FOLLOWUP_RECIPIENT_DAILY_LIMIT` | `20` | Messages per recipient per UTC day |
| `FOLLOWUP_METRICS_FILE` | unset | Write command metrics for the node_exporter textfile collector |
| `FOLLOWUP_OPENAI_API_KEY` | unset | Only for `router`; never committed |

The `.env.example` file documents settings; for CLI runs, export them
into the shell (the CLI does not implicitly load `.env`). Docker Compose
reads `.env` for substitution. Real-provider runs also need to expose
`FOLLOWUP_OPENAI_API_KEY` to the process using your secret manager.

Thresholds, routing, model routes and status synonyms live in
[`config/`](config/).

---

## Assumptions and trade-offs

- **Evaluation date** is an explicit input (`--as-of`), UTC calendar days.
- **One follow-up per task state.** The dedupe key covers task, action,
  recipient and the decision-relevant state (status, due date, owner,
  notes); touching `last_update` alone does not re-notify.
- **Strict dates.** Only ISO dates/datetimes are accepted; anything else
  counts as missing and triggers a clarification instead of a guess.
- **Source freshness.** A separate source timestamp watermark rejects older
  snapshots, including after `Done`; newer versions can reopen work.
  Date-only and equal versions cannot establish intra-day order
  ([reliability details](docs/05-reliability.md)).
- **PostgreSQL instead of a broker.** The outbox, `SKIP LOCKED` claims and
  advisory locks cover the workload without Redis or Celery (ADR-0002).
- **Intents over prose.** The model chooses what to ask and how firmly;
  code writes the words and renders the facts. Messages are less varied
  than free prose, and a whole class of hallucinations disappears.
- **A cheaper model that thinks longer.** Luna at high effort matches
  Sol at low effort for a seventh of the cost and takes about twice as
  long on a plan; for a batch service the cost matters more.
- **Per-decision transactions.** Simple to reason about and atomic; it is
  the main cost in the 10k benchmark.

---

## Not implemented, and why

| Item | Reason |
|------|--------|
| Real task tracker and e-mail/SMS provider | The brief asks for a mock API and forbids real messages; both sit behind ports |
| Re-reminders on an unchanged task | Needs a product decision on cadence and quiet hours |
| A review UI and API endpoint | Held actions are reviewed with `followup review`; a UI with role-based approval is product work |
| Real identity provider, internal TLS | Tokens are verified with configured keys; TLS is a deployment concern |
| 60 s / 30 s targets for 10,000 tasks | Would need batched multi-row writes; current speed is 1,000× the stated volume |

Details: [docs/08](docs/08-implementation-plan.md).

---

## Queue freshness and deployment checks

A newer processed task snapshot retires unsent actions from older states;
a completed task cancels its queued reminders. Held actions are retired
too. An in-flight or unknown write is still reconciled, because it may
already have succeeded, but an obsolete action is never released for a
retry. Already-sent messages cannot be recalled. Freshness is checked
against snapshots the service has actually ingested, not unseen tracker
updates. Migration `0009` applies this fence to existing queues too.

`make compose-check` validates every Compose combination with synthetic
settings. `make db-init` can be rerun without dropping the service role;
generated database secrets are owner-readable. Docker build inputs are
allow-listed in `.dockerignore`; extra CA certificates are optional
build secrets and ordinary builds retain their default CA trust.
Publishing remains manual and requires successful CI for the exact
commit being published.

---

## What changes for production

1. Real tracker and messaging adapters behind the existing ports.
2. TLS gateway and IdP-issued service tokens for the API.
3. Extend the live evaluation from the labelled corpus to labelled real
   field notes, and gate prompt changes on fresh captures.
4. A provider-side spend cap on top of the shared ledger.
5. Re-reminder cadence and a review UI once product rules exist.

---

## Documentation

| Document | Content |
|----------|---------|
| [00 Requirements](docs/00-requirements-analysis.md) | Requirements, scenarios, fixture analysis, assumptions |
| [02 Architecture](docs/02-architecture.md) | Layers, data flow, schema, configuration |
| [04 Decision policy & LLM](docs/04-decision-policy-and-llm.md) | Rule table, LLM contracts, orchestrator envelope |
| [05 Reliability](docs/05-reliability.md) | Identity, transactions, retries, reconciliation, failure modes |
| [06 Test strategy](docs/06-test-strategy.md) | Test pyramid, properties, coverage policy |
| [08 Scope and status](docs/08-implementation-plan.md) | What is done, what is not and why |
| [09 Operations at scale](docs/09-operations-at-scale.md) | 10,000 tasks/day, cost, monitoring, access |
| [15 Security](docs/15-security-asvs-l2.md) | ASVS 5.0 L2, OWASP API and LLM Top 10 |
| [18 Runbook](docs/18-runbook.md) | Alerts, levers, backup and restore |
| [19 API and deployment](docs/19-api-and-deployment.md) | Authenticated API, runtime configuration |
| [OWASP LLM Top 10](docs/security/owasp-llm-top10.md) | Threats, controls and tests for LLM01–LLM10 |
| [Model value report](docs/eval/model-value-luna-high.v1.md) | The corpus with and without the model |
| [Live capture report](docs/eval/luna-high.v1.md) | `gpt-6-luna` captures replayed in CI |
| [Model comparison](docs/eval/model-comparison.md) | `gpt-6-luna` high against `gpt-6.1-sol` low |
| [ADRs](docs/adr/) | Architecture decisions 0001–0019 |

AI coding assistants were used, as the brief allows.
