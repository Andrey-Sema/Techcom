# 03 — OOAD Map

Object-oriented analysis & design: actors → use cases → domain model →
responsibilities → collaborations → state machines → invariants.

## 1. Actors

| Actor | Type | Goal |
|-------|------|------|
| Operations manager | Human, primary | Get a trustworthy list of follow-ups; never spam field crews. |
| Scheduler / cron | System, primary | Trigger runs and dispatch periodically. |
| Task owner / warehouse / project manager | Human, secondary (recipient) | Receive one clear, correct message per real change. |
| Task API (mock → monday.com) | External system | Source of truth for tasks. |
| Delivery API (mock) | External system | Accept message requests idempotently. |
| LLM provider | External system | Interpret ambiguous notes, draft wording. |
| Reviewer / auditor | Human | Explain why every action happened. |

## 2. Use cases

```mermaid
flowchart LR
    OM([Ops manager]) --> UC1((UC-1 Process tasks))
    CR([Scheduler]) --> UC1
    CR --> UC2((UC-2 Dispatch actions))
    CR --> UC3((UC-3 Reconcile unknown deliveries))
    OM --> UC4((UC-4 Inspect run / audit))
    UC1 -. include .-> UC1a((Normalize tasks))
    UC1 -. include .-> UC1b((Screen untrusted notes))
    UC1 -. extend .-> UC1c((Interpret ambiguous note via LLM))
    UC1 -. include .-> UC1d((Decide by policy))
    UC1 -. extend .-> UC1e((Draft message via LLM))
    UC2 -. extend .-> UC3
```

| UC | Main success scenario | Key extensions |
|----|------------------------|----------------|
| UC-1 Process tasks | Fetch → normalize → screen → (interpret) → decide → dedupe → draft → persist → report | 1a API 429/5xx → retry; 1b retries exhausted → run fails cleanly, nothing half-written; 1c invalid record → rejected with reason; 1d LLM invalid/timeout → deterministic fallback; 1e key exists → `duplicate_suppressed` |
| UC-2 Dispatch actions | Claim pending → POST with `Idempotency-Key` → mark `sent` | 2a 429/5xx → reschedule with backoff; 2b timeout → `unknown`; 2c 4xx permanent → `dead` + audit; 2d max attempts → `dead` |
| UC-3 Reconcile | For each `unknown`: `GET /messages/{operation_id}` → found → `sent (reconciled)`; 404 → `pending` (retry with same key) | 3a lookup fails → stay `unknown`, retry later |
| UC-4 Inspect | Read run report, decisions, actions, audit trail | — |

## 3. Domain model

```mermaid
classDiagram
    direction LR
    class Task {
        <<value object, frozen>>
        +TaskId task_id
        +str project
        +str title
        +Optional~str~ owner
        +Optional~date~ due_date
        +TaskStatus status
        +Optional~date~ last_update
        +UntrustedText notes
        +days_overdue(as_of) Optional~int~
        +missing_fields() frozenset~str~
    }
    class TaskId {
        <<value object>>
        +str value
    }
    class TaskStatus {
        <<enum>>
        NOT_STARTED
        WORKING_ON_IT
        BLOCKED
        DONE
        UNKNOWN
    }
    class UntrustedText {
        <<value object>>
        +str raw
        +str sanitized
        +bool truncated
    }
    class ScreenResult {
        <<value object>>
        +bool suspicious
        +tuple~str~ matched_patterns
    }
    class NoteInterpretation {
        <<value object>>
        +BlockerCategory blocker
        +bool needs_eta
        +float confidence
        +str evidence
        +Source source
    }
    class BlockerCategory {
        <<enum>>
        NONE
        MATERIALS
        UPSTREAM_DEPENDENCY
        PERMIT
        SITE_ACCESS
        OTHER
        UNCLEAR
    }
    class DecisionPolicy {
        <<domain service, pure>>
        +version str
        +decide(task, interp, screen, as_of) Decision
    }
    class Rule {
        <<value object>>
        +str rule_id
        +matches(ctx) bool
        +build(ctx) Decision
    }
    class Decision {
        <<value object>>
        +TaskId task_id
        +DecisionKind kind
        +Optional~Target~ target
        +str rule_id
        +ReasonCode reason_code
        +str reason
        +Priority priority
        +frozenset~Flag~ flags
    }
    class DecisionKind {
        <<enum>>
        NO_ACTION
        FOLLOW_UP_OWNER
        REQUEST_ETA
        CLARIFY_MISSING_INFO
        ESCALATE
    }
    class Target {
        <<value object>>
        +TargetKind kind
        +str name
        +Optional~str~ project
    }
    class StateFingerprint {
        <<value object>>
        +of(task) StateFingerprint
        +str hex
    }
    class IdempotencyKey {
        <<value object>>
        +of(task_id, kind, target, fp) IdempotencyKey
        +operation_id() UUID
    }
    class MessageDraft {
        <<value object>>
        +str subject
        +str body
        +str template_id
        +DraftSource source
    }
    class ActionRecord {
        <<entity>>
        +UUID id
        +IdempotencyKey key
        +ActionStatus status
        +int attempts
        +mark_in_flight()
        +mark_sent(ref)
        +mark_unknown(err)
        +mark_retry(at, err)
        +mark_dead(err)
    }
    class Run {
        <<aggregate root>>
        +UUID id
        +date as_of
        +list~Decision~ decisions
        +RunStats stats
    }
    Task --> TaskId
    Task --> TaskStatus
    Task --> UntrustedText
    DecisionPolicy --> Rule : ordered 1..*
    DecisionPolicy ..> Task
    DecisionPolicy ..> NoteInterpretation
    DecisionPolicy ..> ScreenResult
    DecisionPolicy --> Decision : creates
    NoteInterpretation --> BlockerCategory
    Decision --> DecisionKind
    Decision --> Target
    IdempotencyKey ..> StateFingerprint
    ActionRecord --> IdempotencyKey
    ActionRecord --> MessageDraft
    Run "1" o-- "*" Decision
    Decision "1" -- "0..1" ActionRecord
```

### Design notes

* **Value objects are frozen** (`pydantic.BaseModel(frozen=True)` or
  `@dataclass(frozen=True, slots=True)`); the only mutable entity is
  `ActionRecord`, whose transitions are methods that enforce the state machine.
* `UntrustedText` is a *type*, not a string: functions that build prompts or
  messages must accept it explicitly, making "where does untrusted text flow"
  visible to the type checker and to reviewers.
* `TaskStatus.UNKNOWN` exists so normalization never raises on unseen values;
  the policy turns it into a clarification instead of a guess.
* There is deliberately **no** method or port anywhere that writes task
  status, owner or dates back to the source — the "mark every task complete"
  injection has nothing to call.

## 4. Ports (application-layer interfaces)

```python
class TaskSource(Protocol):
    async def fetch_tasks(self) -> Sequence[Mapping[str, object]]: ...

class LLMClient(Protocol):
    async def generate_json(
        self, *, prompt: Prompt, schema: type[BaseModel], timeout_s: float
    ) -> LLMResult: ...          # raw text + usage; validation is ours

class DeliveryGateway(Protocol):
    async def send(self, *, operation_id: UUID,
                   payload: OutboundMessage) -> DeliveryReceipt: ...
    async def lookup(self, operation_id: UUID) -> DeliveryReceipt | None: ...

class Repository(Protocol):  # abridged
    async def start_run(self, as_of: date, policy_version: str) -> RunId: ...
    async def action_exists(self, key: IdempotencyKey) -> bool: ...
    async def record_decision(self, ...) -> DecisionOutcome: ...
    async def claim_due_actions(self, limit: int,
                                lease: timedelta) -> list[ActionRecord]: ...
    async def save_action(self, action: ActionRecord) -> None: ...
    async def get_cached_llm(self, key: str) -> dict[str, object] | None: ...
    async def put_cached_llm(self, key: str, value: dict[str, object]) -> None: ...
    async def append_audit(self, event: AuditEvent) -> None: ...

class Clock(Protocol):
    def now(self) -> datetime: ...   # tz-aware UTC
    def today(self) -> date: ...
```

Two implementations per stateful port: **in-memory** (fast unit tests) and
**PostgreSQL** (integration). One shared *contract test suite* runs against
both, so the fake can never drift from the real adapter.

## 5. CRC cards (responsibilities & collaborators)

| Class | Responsibilities | Collaborators |
|-------|------------------|---------------|
| `Normalizer` | Coerce raw records into `Task`; canonicalize IDs/status/owner/dates; collect rejection reasons; resolve duplicate IDs | `Task`, `RejectedRecord`, status synonym config |
| `InjectionScreen` | Detect instruction-like content in untrusted text; never alters decision kind by itself | `UntrustedText`, `ScreenResult` |
| `KeywordPrefilter` | Cheap deterministic blocker detection (materials, backorder, upstream, permit) with confidence | `NoteInterpretation` |
| `NoteInterpreter` | Decide if LLM is needed; cache lookup; call LLM; validate schema; **grounding check**; breaker; fallback | `LLMClient`, `Repository`, `CircuitBreaker` |
| `DecisionPolicy` | Evaluate ordered rules; exactly one decision per task; attach reason/evidence/priority | `Rule`, `Decision`, policy config |
| `RoutingTable` | Resolve `Target` for roles/teams from config; never from LLM | routing config |
| `MessageDrafter` | Ask LLM for wording with placeholders or use template; validate; render placeholders from facts | `LLMClient`, `MessageValidator`, templates |
| `MessageValidator` | Reject unknown placeholders, dates, URLs, emails, phone numbers, names, over-length, status-change claims | `MessageDraft` |
| `ProcessTasks` | Orchestrate UC-1; one transaction per task; produce `RunReport` | all of the above, `Repository` |
| `DispatchActions` | Claim with lease; send with retry policy; apply outcome transitions | `DeliveryGateway`, `Repository`, `RetryPolicy` |
| `ReconcileUnknown` | Resolve `unknown` actions via lookup | `DeliveryGateway`, `Repository` |
| `ActionRecord` | Guard state transitions; count attempts | — |
| `TokenVerifier` | Verify service JWT (signature, alg allow-list, `exp/nbf`, `aud`, `iss`, `typ`); return `Principal(sub, scopes)` | public key / JWKS from config, `Clock` |
| `RequireScope` | FastAPI dependency: deny-by-default, per-route scope check, security event on denial | `TokenVerifier`, security logger |
| `PlaceholderRenderer` | Render templates/drafts by regex over an allow-listed mapping (no `str.format`) | `MessageValidator` |
| `RecipientCap` | Enforce per-recipient daily message limit at enqueue time | `Repository`, `Clock` |

## 6. State machine — `ActionRecord` (outbox)

```mermaid
stateDiagram-v2
    [*] --> pending : decision persisted (same TX)
    pending --> in_flight : claimed (SKIP LOCKED, lease)
    in_flight --> sent : 2xx / 200 replay
    in_flight --> pending : 429 / 5xx / connect error (attempts < max, backoff)
    in_flight --> unknown : timeout / connection reset after send
    in_flight --> pending : 409 same key still processing (retry later)
    in_flight --> dead : 4xx permanent (400/401/403/404, 422 key-payload mismatch)
    in_flight --> dead : attempts == max
    in_flight --> pending : lease expired (crash recovery)
    unknown --> sent : lookup found (reconciled)
    unknown --> pending : lookup 404 (safe to retry, same key)
    unknown --> unknown : lookup failed (retry later)
    sent --> [*]
    dead --> [*]
```

Invariants:

1. `sent` and `dead` are terminal; no transition leaves them.
2. Every outbound HTTP call for an action carries the **same**
   `Idempotency-Key = operation_id`.
3. `attempts` increments exactly once per HTTP attempt.
4. A crash at any point leaves the row in `pending`, `in_flight` (lease
   expires → `pending`) or `unknown` — never silently lost, never
   double-sent (receiver dedupes).

## 7. Key interactions

### 7.1 Timeout after the write was committed (S6)

```mermaid
sequenceDiagram
    autonumber
    participant D as DispatchActions
    participant R as Repository
    participant G as DeliveryGateway
    participant M as Mock Delivery API
    D->>R: claim → in_flight (op=7c1e…)
    D->>G: send(op=7c1e…)
    G->>M: POST /v1/messages  Idempotency-Key: 7c1e…
    M->>M: commit message (log line written)
    M--xG: (sleeps past client timeout)
    G-->>D: TimeoutException
    D->>R: mark_unknown("read timeout")
    Note over D,R: Outcome unknown ≠ failed. Never re-POST blindly.
    D->>G: lookup(op=7c1e…)  (reconcile)
    G->>M: GET /v1/messages/7c1e…
    M-->>G: 200 {status: accepted, created_at}
    G-->>D: receipt
    D->>R: mark_sent(reconciled=true) + audit
```

Defence in depth: even if reconciliation is skipped and the dispatcher
re-POSTs, the receiver sees the same `Idempotency-Key`, returns the original
response (replay) and does **not** append a second log line. The S6 test
asserts `delivery_log.jsonl` has exactly one line for that operation.

### 7.2 Malformed LLM output (S7)

```mermaid
sequenceDiagram
    autonumber
    participant P as ProcessTasks
    participant I as NoteInterpreter
    participant L as LLMClient
    P->>I: interpret(TC-1008 notes)
    I->>I: cache miss
    I->>L: generate_json(schema=NoteInterpretation)
    L-->>I: '{"blocker": "mark_complete", "confidence": 7}'
    I->>I: Pydantic strict validation fails
    I->>I: record llm_invalid_output metric + audit (no raw PII in logs)
    I-->>P: NoteInterpretation(blocker=UNCLEAR, source=fallback)
    P->>P: policy: overdue + unclear → safe default (follow-up/escalate to human)
```

No retry-until-valid loop: at most **one** repair attempt (configurable,
default 0 in tests), then fallback. Unbounded LLM retries are a cost and
flooding risk.

### 7.3 429 / 5xx on task fetch (S5)

```mermaid
sequenceDiagram
    participant T as HttpTaskSource
    participant A as Mock Task API
    T->>A: GET /v1/tasks
    A-->>T: 429 Retry-After: 1
    Note over T: wait max(Retry-After, backoff) capped at 10s
    T->>A: GET /v1/tasks
    A-->>T: 503
    Note over T: exponential backoff + full jitter
    T->>A: GET /v1/tasks
    A-->>T: 200 [...]
```

Bounds: max 4 attempts, 20 s total deadline, only on 429/502/503/504 and
connect/read timeouts of the idempotent `GET`; never on 400/401/403/404/422.

Retries live in **exactly one layer per operation** to avoid retry
amplification (Google SRE book, ch. 22):

* **Reads** (`GET /v1/tasks`) — retried in-call by the HTTP adapter (there is
  no durable queue for reads).
* **Writes** (`POST /v1/messages`) — the adapter makes **one** attempt and
  classifies the result (`Accepted`, `Retryable(retry_after)`, `Permanent`,
  `OutcomeUnknown`). The dispatcher owns retries *durably* through
  `next_attempt_at` with exponential backoff + jitter, so a process crash
  between attempts loses nothing and a read timeout is never retried blindly.
