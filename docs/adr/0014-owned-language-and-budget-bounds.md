# ADR-0014: Owned language calls and approved message phrasing

* Status: Accepted
* Date: 2026-10-22
* Refines: ADR-0004 and ADR-0013

## Context

A strict JSON schema and a known-name/date detector cannot establish that
arbitrary prose contains no invented facts. For example, a new person name
can evade a detector built from the owner directory. Separately, a stale
request can finish after a breaker opens and a new probe starts; global
probe cleanup can accidentally admit a second probe. Checking spending
before an await also admits concurrent requests against the same balance.

## Decision

* A note interpreter can return only a closed blocker enum, a Boolean,
  finite confidence and a verbatim evidence quote. Its source is set by
  code. Notes are independently screened before calls; flagged notes never
  reach a model. Notes are escaped and isolated from system instructions.
* Drafting prompt `draft.v2` offers exact code-authored phrase variants.
  Local validation accepts only an exact catalogue member. Adding a name,
  an assertion, a date or an instruction rejects the whole draft. Every
  factual value is inserted by code. Escalation owner/reason context is
  appended by code and is never offered as a model placeholder. This
  deliberately trades unrestricted stylistic freedom for a verifiable
  language boundary; regexes remain supplementary checks.
* Breaker admission returns an attempt-owned generation receipt. A stale
  completion cannot close a newer generation or release another probe.
  Cancellation releases only its own probe. A total deadline includes
  semaphore admission, transport and response acquisition. There are no
  application or SDK retries; the router may try distinct configured
  routes for availability failures within the caller's total deadline.
* Spending is reserved synchronously before network admission and settled
  exactly once in `finally`. Actual usage replaces a bound; unknown billing
  and cancellation consume the full bound. Reservations carry across UTC
  midnight. A reported overrun is charged in full and permanently blocks
  admission; its uncovered amount remains visible across midnight.
* Bounds count ASCII-serialized input and schema bytes, which dominate
  UTF-8 bytes even for supplementary Unicode. The byte-level tokenizer
  assumption bounds content tokens by bytes. The additional 8,192-token
  provider framing/schema-compilation allowance is a deployment assumption,
  not a proof about hidden provider internals. Prices include the most
  expensive cached/cache-write mix and applicable long-context premiums.

## Scope and consequences

The budget is atomic only inside one event-loop thread in one process.
Multiple workers and restarts need a shared durable reservation ledger.
Provider-side spending quotas remain the ultimate external hard cap;
provider pricing, tokenizer and framing changes require review. Validated
cache keys include an explicitly configured model/route identity and prompt
version; changing routing requires changing that identity.

The workflow has no tools or task-mutation capability. Deterministic rules
own every decision, target and fact. Invalid interpretation becomes
`UNCLEAR`; invalid phrasing becomes a template. Existing overdue escalation
rules still apply independently of LLM availability. Tests provide evidence
for these implemented controls, not ASVS L2 certification or operational
controls absent from this repository.
