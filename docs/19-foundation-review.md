# M0 / M2 / M3 implementation review

Review date: 2026-10-22. Baseline: `1332053` on `main`. This batch
implements three milestones: bootstrap, the local mock receiver and
persistence. M1 and the first OpenAI router were already on the baseline.

## Independent review and evidence

One GPT-6.1 Sol agent at high reasoning implemented M3 while the primary
agent implemented M0/M2. Before publishing the PR, each reviewed the
other's implementation. The M3 author also reviewed the existing router
without editing it. This is independent review of the relevant author's
changes, not an independent certification of the complete application.

The review traced every new source module, repository contracts, migration,
workflow permissions/pinning, packaging, locks and delivery journal. It
found and resolved these issues before PR publication:

| Finding | Resolution | Evidence |
|---------|------------|----------|
| Lookup during an in-progress delivery returned 404, allowing a future reconciler to resend too early | Reserved operations return 409 with Retry-After | `test_pending_lookup_never_signals_safe_to_resend`, including a non-idempotent receiver |
| A returned in-memory run shared mutable initial stats with storage | Detached initial snapshot | Shared repository run lifecycle contract |
| Concurrent duplicate action inserts could hit the second unique index | Handle all insertion conflicts; validate derived operation identity before insert | Concurrent same-key and same-run tests against PostgreSQL 18 |
| Unicode line separators could split a JSONL record during recovery | ASCII-escaped JSONL serialization | Hypothesis arbitrary-text journal round-trip |
| Expired send leases were planned to return directly to pending | Recover to unknown; require a fenced reconciliation claim | Shared lease-expiry, stale-token and reconciliation tests |

Verification uses locked dependencies, strict mypy, Ruff ALL at 79
columns, Bandit, import-linter, all pre-commit hooks, a Python 3.13/3.14
test matrix and the existing 100% line/branch coverage gate. PostgreSQL
tests actually run on PostgreSQL 18, exercise migration upgrade/downgrade,
hold database locks across competing claims, and inject cancellation and
late-transaction failure. A production wheel was built and inspected:
neither the mock package nor tests are included. No live LLM request was
used for this evidence.

Final local result on **both Python 3.14.8 and 3.13.5: 400 passed, one
opt-in live test deselected, 100% line/branch coverage**. CI repeats the
complete suite on both versions. All quality checks and pre-commit hooks
passed.

## Threat boundaries and applicable controls

| Boundary / threat | Implemented control | Remaining work |
|-------------------|---------------------|----------------|
| Untrusted task text, prompt injection (tampering) | Existing pure domain policy, distinct untrusted-text type, safe placeholder renderer | Wire screening, strict LLM output validation and grounding in M5; evaluate injections in M11 |
| Duplicate, cancelled or stale workers (tampering / repudiation) | Unique identities; transactional decision/action/audit; SKIP LOCKED; token-and-expiry fencing; unknown-state recovery | M4 owns network side effects, reconciliation and single-run orchestration |
| Lost delivery acknowledgement | Journal fsync before response; replay with canonical payload digest; pending lookup never reports absent | Journal is single-process/local-only; production receiver guarantees require an actual provider contract |
| Oversized or malformed mock requests (denial of service / disclosure) | Actual-byte limit including chunked requests, strict bounded transport models, generic errors without input echo | Production rate limits and authentication are M6b/M9 |
| Unsafe software composition | Architecture/isolation contracts, production wheel excludes mocks, pinned read-only CI actions | Full supply-chain/security gate set is M0b/M8/M9 |
| Service identity and object access (spoofing / elevation / BOLA/IDOR) | Mock and database bind to loopback; credentials are local ignored files | Service JWTs and scope RBAC, project ABAC/relationship checks, object/field authorization, and least-privilege DB roles are not implemented in this batch |

UUIDs and deduplication keys are identities, not authorization. All
repository methods are trusted internal ports; they do not grant external
callers access. API authorization must be enforced before these ports are
exposed. No multi-tenant or project-level authorization claim is made.

## Existing LLM router findings assigned to M5

Independent local fake-backend/SDK reproductions identified:

1. SDK redirect following is enabled by default, so validating the initial
   endpoint does not enforce the destination allow-list for redirects.
2. Cancelling a half-open model request leaves its probe flag occupied;
   later requests can skip the model indefinitely.
3. Checking spend before awaiting a request and charging afterwards admits
   concurrent requests past the limit. With limit 0.004, two requests can
   settle a total of 0.008. A reservation/settlement contract is required.

Additionally, the missing-usage estimate based on character count divided
by two is not a proven upper bound for Unicode or schema tokens. M5 must
replace it with a documented conservative estimator and tests. These are
existing findings, not fixed by the foundation batch, and the router is
not used by its mock or persistence operations.

## Limits of the result

This is an ASVS 5.0 L2 design target with evidence for the controls above;
the complete project does not yet meet or certify ASVS L2. The original
security tables described planned controls as Built. Their statuses are
now Planned until implementation evidence is linked. Production TLS,
secret lifecycle, DB role separation, authorization, durable shared LLM
budgets, observability and end-to-end delivery remain explicit milestones.

The original 2,500-line source budget is exceeded by the typed contracts
and the existing domain/router plus two repository implementations
(approximately 3,600 source lines). This is recorded as a scope deviation,
not hidden by code compression or reduced coverage. Complexity gates stay
enabled; the full M0b tool set is still pending.
