# M4/M5/M6a implementation and independent review

This batch adds processing, delivery/reconciliation, bounded language calls,
and the runnable CLI. It is stacked on the M0/M2/M3 foundation. Evidence
below concerns implemented local controls. It is not an ASVS certification
or a claim about real-provider language quality.

## Runtime boundaries

* Policy owns decisions, projects, recipients and facts. Notes are untrusted
  data, including when the pattern screen misses an injection. The model
  has no tools, mutation endpoint or authorization authority.
* Interpretation accepts a closed JSON schema, finite confidence and a
  verbatim supporting quote. Output is bounded and revalidated locally,
  including cache hits. The cache namespace includes prompt and routing
  configuration; its clock is explicitly injected.
* Drafting selects an exact code-owned wording variant (`draft.v2`). Facts
  are filled afterward by code. Invented names or arbitrary prose fail
  validation and use the deterministic template. A generic prose regex
  cannot prove the absence of invented facts.
* Keyword classification defers historical, resolved and contradictory
  contexts to interpretation. Unavailable interpretation produces
  `UNCLEAR`, never an automatic conclusion that a task is complete. The
  contextual patterns are conservative and do not solve all negation.
* The router reserves the maximum configured charge before awaiting I/O.
  Unknown billing consumes the reservation; midnight preserves outstanding
  exposure. Owned breaker permits prevent stale cancellation or completion
  from releasing another request's half-open probe. Redirects are disabled.
* Task fetching has a bounded retry allocation and total deadline. Delivery
  POST has one transport attempt; uncertain results become `unknown`.
  Reconciliation must establish authoritative absence before another send.
  HTTP response bytes are bounded while streaming and calls have a total
  deadline. PostgreSQL claims and completions are fenced by token/expiry.
* CLI report/dispatch/recovery dependencies are lazy. A broken language
  configuration cannot block a report or recovery lookup. Memory runs are
  labeled; standalone durable commands require PostgreSQL.
* SIGTERM latches delivery admission closed before or after dispatcher
  construction, then drains for at most 25 seconds. Reconciliation may
  finish lookup work during that grace period. Cancelled sends remain
  uncertain and require reconciliation. Resources close after owned work.
* Standard output and report files intentionally contain business facts.
  Operational stderr logs use a closed metadata projection: no notes,
  prompts, responses, credentials, exception strings or traceback objects.
  Validation logs retain a stage, prompt version and SHA-256 digest. Report
  files use atomic per-file replacement and owner-only permissions; the
  two exported files are not a transactional pair.

## Independent findings and their regressions

Development was divided between three GPT-6.1 Sol agents at high effort and
integration by the primary agent. The author of each major component was
not its sole reviewer. Findings were reproduced and then checked again
against the fixes; the model label itself is not quality evidence.

| Finding | Impact | Implemented fix and evidence |
|---|---|---|
| Old cancellation/completion released a newer half-open probe | Concurrent requests bypassed one-probe admission | Owned permits and generations; `tests/unit/llm/test_router.py`, `test_language_workflow.py` and processing breaker tests |
| SDK followed redirects and budget admission raced across awaits | Wrong outbound destination or concurrent budget overspend | Explicit redirect rejection, reservation accounting and concurrent/midnight/overrun tests in `tests/unit/llm/` |
| Wording validation accepted an invented name with valid placeholders | Fabricated business fact reached a message | Exact approved phrasing plus application validation; `tests/scenarios/test_llm_safety.py` |
| Cancelled advisory-lock acquisition returned an uncertain session to the pool | A later run could inherit the singleton lock | Connection invalidation and owned release; real PostgreSQL tests in `tests/integration/persistence/test_run_lock.py` |
| Per-chunk timeouts allowed an arbitrarily long streamed response | A peer could retain the worker indefinitely | Total call deadline and streaming byte limit; `tests/unit/processing/test_retry_and_http.py` |
| SIGTERM during processing preceded lazy dispatcher creation | `run --dispatch` could start sends after shutdown admission closed | Scope stop latch; `test_shutdown_before_lazy_dispatcher_creation_prevents_later_sends` |
| Demo inherited HTTP proxy variables | Forced loopback URLs could still send payloads through a proxy | Demo HTTP clients use `trust_env=False`; hostile-proxy constructor regression and independent transport-selection check |
| Resolved/history notes matched a high-confidence blocker keyword | False warehouse/manager routing | Context deferral plus unit and actual processing regressions; baseline eval disagreements are retained in the later eval batch |

The final independent CLI pass found no remaining blocking issue in its
reviewed scope and rechecked the stop latch and proxy fixes without live
network calls. This is a scoped review, not proof that no defects remain.

## Reproducible validation

Run from the repository root with Python 3.13 or 3.14 and Docker available:

```sh
make bootstrap
make quality
make test
make demo
uv run --locked pre-commit run --all-files
```

The complete suite enforces 100% line and branch coverage across production
and mock packages. It includes real PostgreSQL 18 transactions, concurrent
claims/locks, fencing, rollback and recovery; Hypothesis checks domain,
normalization, arbitrary LLM output and log projection properties. The
standalone demo uses a real TCP mock server, ten fixture decisions, seven
deliveries, and seven deduplicated proposals on the second run. Committed
samples are generated from that executable path.

The local matrix passed **714 tests** on Python 3.13.5 and 3.14.8 with
100% line and branch coverage; the live-provider test was excluded in
both runs. Ruff, strict mypy, Bandit, import-linter and pre-commit passed.

GitHub Actions was unable to start on PR #1 because the account exhausted
its private-repository minutes. Local matrix evidence is reported separately
from remote CI status. Making the repository public and rerunning Actions
is an owner action; repository visibility has not been changed by agents.

## Remaining boundaries and STRIDE considerations

| Boundary | Evidence or limitation |
|---|---|
| Spoofing / object authorization | The CLI is a trusted operator interface. Service JWTs, scopes and project/object authorization belong to M6b/M9; a UUID is not authorization. The local mock must not be exposed to untrusted clients. |
| Tampering / LLM injection | Local schema/grounding checks, exact wording, code-owned recipients/facts and no model tools constrain authority. The pattern screen is an extra signal, not the sole control. Provider accuracy remains unverified. |
| Repudiation | Initial decision/action/audit creation is atomic. Outcome audit append follows a fenced status update in a separate transaction; a crash can leave an outcome audit gap. Durable transactional outcome auditing remains future hardening. |
| Information disclosure | Closed operational log projection and owner-only report exports are tested. Business reports deliberately contain recipient names and task facts; deployment access/retention controls remain necessary. |
| Denial of service | HTTP bytes, deadlines, retries, breaker admission, output size and router concurrency are bounded. Budget reservations are only atomic within one process/event loop. |
| Privilege escalation | Language output cannot select a recipient, action or task state. Least-privilege database roles and externally authenticated service authorization remain M9 work. |

The 8,192-token provider framing allowance is a deployment assumption, not
a proof of undocumented provider overhead. Shared durable accounting is
needed across workers/restarts; provider spending limits are the ultimate
external cap. ADR-0014 records this boundary. No real model API was called
for the verification above. ASVS L2 evidence must be completed and assessed
against the deployed system, including controls absent from this batch.
