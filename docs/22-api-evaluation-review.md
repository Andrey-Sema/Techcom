# M6b / M11a / M11b review and evidence

This batch integrates the authenticated API and the minimal/expanded offline
LLM evaluation harness with `main` at `da0f2a0`. Existing container, database
role, retention, observability and performance work is preserved. It does
not claim ASVS certification or measured live-provider quality.

## API authority and request bounds

The API factory takes explicit repository, processing, verification, clock
and observer dependencies. The actual `ProcessTasks` implementation meets
the run-service port; scenario tests exercise API authentication, processing,
outbox creation, project-filtered reads and duplicate suppression together.
No deployment credential or live provider is needed for these tests.

* JWT verification uses configured Ed25519 public keys only: exact algorithm
  and token type, signature, issuer, audience, required issued/not-before/
  expiry times, a maximum 15-minute lifetime and bounded clock leeway.
  Token-supplied remote key URLs and unknown header fields are rejected.
* Every business route requires a declared scope. Signed project memberships
  authorize object reads; query fields only narrow those memberships. A
  wildcard additionally requires an explicitly privileged service subject.
  Global processing rejects a project-only subject before reading its body.
* UUIDs are identifiers, not authorization. Scoped run reports fail closed
  for incomplete, ambiguous, quiet or mixed-project decision graphs because
  historical rows lack independent source-project provenance. This is a
  deliberate availability restriction until M9 persists that provenance.
* Authentication, capability admission and subject rate limiting run before
  request JSON parsing. Bodies have an actual-byte limit and a total five-
  second receive deadline (configurable within a finite bound). Limits also
  cover query bytes, duplicate fields, response rows and token length.
* `ActionReadQuery` carries server-derived authority to both repositories.
  PostgreSQL filters before `LIMIT`; application row decoding is bounded to
  100. Its current JSON project predicate has no dedicated index, so this
  bounds materialization, not database scan cost.
* Generic errors cover guards, receive failures, validation and dependency
  exceptions without echoing their text. Cancellation and lifespan failures
  propagate. Request IDs and security headers survive rejection paths.
  Only the two declared health paths are public; accidentally added root
  routes are rejected before their handlers execute.
* Metrics use a separate ASGI application with a private registry. The public
  API does not expose its metrics route. Security-event sinks are injected;
  concrete OTLP exporter and deployed listener wiring remain follow-up work.

The process-local token buckets do not establish a shared multi-worker
quota. Production must use a gateway/shared limiter or the documented
single-worker boundary. Rate limiting does not replace project authorization.

## Reproduced findings and fixes

Independent review of the initial API identified three concrete paths:

| Finding | Fix and regression evidence |
|---|---|
| Unauthenticated slow body retained a request before authentication | Admission precedes receive; a bounded total receive deadline covers authorized/public requests. Tests observe zero body reads on 401/403 and a controlled 408 for a stalled stream. |
| Invalid JSON returned 422 before failed-auth accounting | Malformed unauthenticated requests now produce 401 then 429; authenticated invalid JSON is charged once to the subject bucket. |
| A 100-row response loaded the entire outbox before filtering | Typed server-authorized query port, bounded memory selection and PostgreSQL predicates before row decoding and `LIMIT`; shared contracts plus actual PostgreSQL tests. |

A JWT recursion candidate was disproved: the installed parser already
rejected those inputs with 401/429. It is not reported as a vulnerability.
These checks were manual code review and executable reproductions, not a
formal automated Codex Security scan.

The final integration pass additionally reproduced a logging mismatch:
CLI logging configuration dropped API event names and correlation. Its
closed projection now preserves known security event codes, canonical UUID
request IDs, bounded HTTP statuses and hashed actor IDs. Actual API logging
is checked without a test sink; tokens and arbitrary fields remain absent.

Integration also exposed two inherited verification/build issues. Gitleaks
returned success after failing to open worktree Git metadata and scanning
zero bytes. Its container now mounts the common Git metadata read-only and
independently verifies the repository before scanning. Container data copies
also inherited restrictive host permissions, preventing non-root users from
reading configuration and fixtures. Explicit root-owned read permissions
make the images independent of host umask. Image tests keep their private
service aliases in `NO_PROXY`, preserving external proxy configuration.

## Historical execution evidence and final review boundary

The following executions preceded the instruction to stop environment
verification. No tests, builds, linters or security scanners were run after
that instruction. Subsequent edits received manual code/security review only.

* Before the final public-route guard, Python 3.13.5 and 3.14.8 each passed
  **1,002 tests**, with 100% production/mock line and branch coverage.
* After that guard, Python 3.14.8 passed **1,003 tests**. Python 3.13.5
  passed 1,002 and failed one Hypothesis timing deadline, with no failed
  correctness assertion. Both runs measured 100% line and branch coverage.
  Live-provider, image and benchmark cases are separate.
* Evaluator: **48 tests**, independently measured 100% line/branch coverage.
* Container suite: **8 tests passed**, including PostgreSQL 18 migration,
  shipped CLI processing/dispatch/dedupe and receiver restart recovery.
* Before the final public-route guard, Ruff ALL, strict mypy, Bandit,
  import-linter, Xenon, Vulture, deptry, spelling, YAML/workflow schemas,
  MkDocs strict and pre-commit passed.
* Semgrep's seven rule fixtures passed; its source scan found no violation.
  The locked runtime dependency audit found no known vulnerability. Gitleaks
  scanned actual staged content and the full 64-commit branch history.
* The real TCP demo passed comparison with committed samples.

One fsync property test had a timing-only Hypothesis failure under load:
273 ms initially versus 2 ms on replay. Its per-example performance deadline
is disabled locally to that I/O property; payload generation, canonical
hashing and persistence assertions remain intact. Both complete matrix runs
then passed before the final route guard. The later Fraction-oracle property
exceeded 200 ms once (404 ms), then completed in under 1 ms on replay.
Its deadline is now disabled only for that correctness property, retaining
the same generators and assertions. This last adjustment and the final
documentation edits have **not been verified by execution**. GitHub Actions
previously could not start with exhausted private-repo minutes; historical
local results do not assert remote CI success.

## Evaluation evidence and its limits

`make eval` runs real interpretation, screening, policy and drafting using
scripted responses. `make eval-test` separately enforces 100% evaluator line
and branch coverage. CI runs replay and its coverage gate on both supported
Python versions. Bandit scans the evaluator independently of the production
configuration's test exclusion; its measured scan covers evaluator code.

The minimal archived dataset has 20 benign notes and four injection cases.
The expanded/current dataset has 63 benign notes (nine per blocker class)
and 32 injection variants. Notes and independent labels stay unchanged.
The current bundle, `postfix.v2`, binds the integrated application source,
judge, prompt/schema/input hashes and every recorded response. Historical
bundles and reports remain immutable and deliberately fail current-source
replay when their pipeline fingerprint differs.

Current synthetic replay has macro-F1 **0.990718**, benign-only macro-F1
**0.984078**, failed-call fallback **7/144**, zero structural violations and
zero decision mismatches. One empty-note classification disagreement remains
visible. These numbers measure curated contract replay, not a model's
accuracy. Every committed response is labeled `synthetic.contract.v1`;
provider quality is **UNVERIFIED** and `--require-provider` rejects it.
Even externally supplied, internally consistent provider captures cannot
prove remote origin through offline checks alone.

The safety judge has positive controls. It checks quiet completed work,
overdue work not silenced, missing facts, code-owned recipient membership,
missing messages, screen bypasses and catalogue-constrained messages. Wrong
semantic classification or an incorrect choice among allowed recipients is
reported separately. Zero structural violations does not mean general
injection immunity. A separate benign-only gate prevents numerous injection
labels from masking poor benign classification.

## Final manual code and security review

The final integration was reviewed by one agent, preserving the earlier
independent API review and its reproduced findings. This final pass is not
an additional independent review. It covered JWT trust and route admission,
project predicates and object visibility, error/log projection, evaluator
metrics and provenance, and the existing LLM router's concurrency boundary.

The STRIDE review maps spoofing to configured signing keys, tampering to
strict claims and locally validated structured output, repudiation to
sanitized correlation events, disclosure to project-authorized reads and
closed errors, denial of service to finite body/query/row limits, and
privilege elevation to explicit capabilities and signed memberships.
Capabilities implement role-style admission; project attributes and object
relationships implement the ABAC/ReBAC checks relevant to these routes.
UUID knowledge grants no object authority, addressing BOLA/IDOR paths.

The LLM router reserves budget before awaiting provider work, caps concurrent
calls, settles its own reservation in `finally`, and uses generation-owned
breaker permits. Uncertain provider outcomes retain conservative charges.
Provider schemas are backed by local strict validation; interpretation cannot
choose an action or recipient, and drafting accepts code-owned catalogue
choices. Refusals and incomplete responses do not initiate another model
attempt. These are bounded code-level controls, not proof of semantic
correctness or deployment-wide quotas. Existing regression and Hypothesis
tests remain in the change; none were executed during this final pass.

## Remaining security and operational work

ASVS traceability distinguishes implemented controls from partial and
production-only requirements. Remaining controls include source-project
provenance/indexing, durable per-recipient admission, shared quotas where
required, and runtime OTLP/export configuration. The LLM budget remains
process-local with a documented provider framing assumption. No real API
credential or provider endpoint was used in this batch.

The integrated maintenance path also deserves a separate dedupe-lifetime
review: removing terminal actions must preserve enough identity information
to suppress historical replays. The current retention horizon is not a proof
of permanent dedupe. This existing-main concern is tracked for the next
security/operations batch rather than hidden by API test results.
