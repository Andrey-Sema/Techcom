# 06 — Test Strategy

Goal: **100 % line + branch coverage as a CI gate**, *and* evidence that the
tests are meaningful (scenario tests, property tests, mutation testing on
the policy) — coverage is the floor, not the proof.

## 1. Test pyramid

| Level | Scope | Tooling | Speed | Count (est.) |
|-------|-------|---------|-------|--------------|
| Unit | domain (normalize, policy, identity, screen, validator, templates), resilience primitives, settings | pytest, hypothesis, fake clock | < 2 s | ~150 |
| Property | invariants over generated tasks | hypothesis | < 5 s | ~10 |
| Contract | `Repository` and `LLMClient` port contracts run against every implementation | pytest parametrized fixtures | — | ~25 × impls |
| Adapter | HTTP adapters vs mock API (ASGI in-process), Gemini adapter vs fake `genai` client, respx for edge HTTP | httpx `ASGITransport`, respx | < 3 s | ~40 |
| Integration | PostgreSQL 18 repository, migrations up/down, `ON CONFLICT`, `SKIP LOCKED` concurrency | testcontainers `postgres:18` | ~15 s | ~25 |
| Scenario (E2E-in-process) | The 8 brief scenarios end-to-end through CLI use cases, mock API + PG | all of the above | ~10 s | 8 + variants |
| Golden | Fixture decisions & messages snapshot | syrupy | < 1 s | 1 per fixture |
| Smoke (CI only) | `docker compose up` + `followup demo` + diff vs `samples/` | Make, compose | ~60 s | 1 |

## 2. Required scenario tests (one test module each, named after the brief)

| Brief scenario | Test | Asserts |
|----------------|------|---------|
| S1 overdue, no explanation | `test_s1_overdue_without_explanation_follows_up_owner` | TC-1001 → `follow_up_owner`, target John Smith, rule R7, message mentions days overdue via placeholder. |
| S2 overdue + materials | `test_s2_materials_block_requests_eta_not_completion` | TC-1002 → `request_eta`, target warehouse, message contains "delivery date", does **not** contain "complete". |
| S3 done / missing info | `test_s3_done_is_no_action`, `test_s3_missing_owner_requests_clarification`, `test_s3_missing_due_date_requests_clarification` | No owner/date is ever invented (target is coordinator / owner from data). |
| S4 rerun | `test_s4_second_run_unchanged_data_sends_nothing` | Run 2: all `duplicate_suppressed`, delivery log line count unchanged, outbox unchanged. |
| S4b changed state | `test_s4_changed_due_date_creates_new_action` | Only the changed task gets a new action. |
| S5 429 / 5xx | `test_s5_rate_limit_and_transient_errors_are_retried_bounded` | Attempts == plan + 1, waits ≥ `Retry-After`, total attempts ≤ max, no attempts after budget. |
| S6 timeout after commit | `test_s6_timeout_after_commit_does_not_duplicate` | Action → `unknown` → reconciled `sent`; mock store has exactly 1 message for the op id. Variant with forced re-POST asserts replay header. |
| S7 malformed LLM | `test_s7_malformed_llm_json_falls_back_safely` (parametrized: non-JSON, wrong enum, extra field, out-of-range confidence, ungrounded evidence, timeout) | Decision from fallback path, flag `llm_fallback`, no `no_action` for overdue, metric incremented. |
| S8 injection | `test_s8_prompt_injection_note_is_untrusted_data` | TC-1006 flagged, LLM *not called* for the note, decision `follow_up_owner`, no other task affected, message does not echo the note. |

## 2a. Security and logging tests

* `tests/security/` — one module per ASVS chapter (`test_v8_authorization.py`,
  `test_v9_tokens.py`, `test_v13_configuration.py`, …); each test carries
  `@pytest.mark.asvs("<id>")`. A `conftest.py` hook writes the
  requirement → test map used by the traceability report.
* Route inventory test: every route except health requires a token and a
  declared scope (deny-by-default proof).
* Format-string payloads (`{recipient_name.__class__}`), CRLF in subjects,
  oversized bodies, duplicate query params, `TRACE`, tampered/expired/`none`
  tokens, wrong `aud`/`typ`.
* Log tests: catalogue coverage, redaction property test, log-injection
  payload (see `14` §10).
* Image test (CI): `docker run --entrypoint python … -c "import
  techcom_mocks"` must fail; no `.git`, no `tests/` in the image.

## 2b. LLM evaluation harness (`ADR-0012`)

* Dataset `tests/eval/notes.jsonl`: ≥ 60 synthetic, labelled notes —
  materials (incl. "backorder", "awaiting delivery"), upstream dependency,
  permit, site access, none, genuinely ambiguous, and ≥ 30 injection
  variants (direct, homoglyph, base64, multilingual, "role" tags).
* Recorded provider responses (`tests/eval/recordings/<prompt_version>/`)
  are replayed in CI; `make eval` re-records against the live provider
  (opt-in, needs a key).
* Metrics: per-class precision/recall, macro-F1, confusion matrix,
  fallback rate, **unsafe-outcome count** (any decision that differs from
  the rules-only decision in a forbidden direction, e.g. overdue →
  `no_action`).
* Gates: macro-F1 ≥ 0.85, unsafe outcomes = 0, fallback rate ≤ 10 %.
  Report committed as `docs/eval/<prompt_version>.md`.

## 2c. Benchmark and API fuzzing

* `tests/bench/test_10k.py` (`-m bench`): 10,000 generated tasks (seeded),
  stub LLM, PostgreSQL 18 via testcontainers; budgets: full run < 60 s,
  rerun (all duplicates) < 30 s, peak RSS < 300 MB. Results written to
  `docs/benchmarks.md` with hardware info.
* As built (M8): `make bench`. Measured 83 s / 71 s / 218 MB after the
  benchmark exposed eight round trips per decision (now five) and a
  connection per transaction in the test helper. The 60 s / 30 s targets
  are not met; the test guards against regressions at 120 s, and
  `docs/benchmarks.md` explains why and quantifies the next lever.
* Schemathesis against the running API (`/openapi.json`) with all checks
  (status-code conformance, response schema, server errors) using a CI
  service token; plus negative-auth stateful checks.
* As built (M8): the same harness runs in-process against the mock
  receiver (`tests/contract/api/test_mock_openapi.py`). Its first run
  found that the mock's OpenAPI described FastAPI's default 422 body and
  omitted 400/404/409/413/503; the routes now document what they send.

## 3. Property-based invariants (hypothesis)

Implemented with M1 (`tests/property/test_domain_properties.py`): 1–6, 8,
9, 10. Invariants 7, 11 and 12 need the use cases and the LLM
interpreter and arrive with M4/M5.

1. **Totality**: every normalized task yields exactly one decision.
2. **Done is quiet**: `status == DONE ⇒ decision == no_action`.
3. **No invention**: `target` is the task owner, or a value from the routing
   config — never anything else.
4. **Missing info is never guessed**: `owner is None or due_date is None`
   and not Done ⇒ `clarify_missing_info`.
5. **Overdue is never silenced**: `Δ < 0` and not Done ⇒ decision ≠
   `no_action` regardless of LLM output (including adversarial outputs).
6. **Key stability**: idempotency key invariant to whitespace/case noise in
   source fields; changes when any fingerprinted field changes.
7. **Idempotent processing**: `process(process(state)) == process(state)` in
   terms of outbox contents.
8. **LLM cannot change decision kind beyond allowed set**: for any
   interpretation, decision ∈ the set reachable from date/status rules.
9. **Message safety**: rendered messages contain no unresolved placeholders,
   no URLs/e-mails, length within bounds.
10. **Normalization totality**: for arbitrary JSON values in every field
    (`st.recursive` over None/bool/int/float/str/list/dict), `normalize`
    never raises and returns `Task` or `RejectedRecord` (`16` #2).
11. **Any LLM text is safe**: for arbitrary stub output (`st.text()`,
    JSON-like strings), processing never crashes and an overdue non-Done
    task is never `no_action` (`16` #8).
12. **Retry allocation**: for any sequence of dependency failures, retry
    admissions never exceed `floor(allocated_request_units / 5)`. This
    invariant concerns the configured allocation, not the ratio of actual
    observed network calls. Exhausted delivery admission defers the claim
    without I/O or a consumed send attempt.

## 4. Fault-injection mechanics (hermetic, no sleeps)

* Mock API runs in-process via `httpx.ASGITransport`.
* A `FaultInjectingTransport` wraps it: for `timeout_after_commit` it
  forwards the request (the mock commits) and then raises
  `httpx.ReadTimeout` — deterministic, zero wall-clock wait.
* `stamina.set_testing(True)` (or an injected `sleep`) removes real backoff
  waits while still asserting computed delays via a recording sleeper.
* Clock is injected (`FixedClock(2026-10-21T09:00Z)`); no `datetime.now()`
  in domain code (ruff `DTZ` rules enforce tz-aware usage).

## 5. Coverage policy (100 %, honest)

```toml
[tool.coverage.run]
branch = true
source = ["src"]

[tool.coverage.report]
fail_under = 100
show_missing = true
skip_covered = true
exclude_also = [
    "if TYPE_CHECKING:",
    "raise NotImplementedError",   # Protocol stubs only
    "@overload",
]
```

* **No ad-hoc `# pragma: no cover`** — a CI grep fails the build if one
  appears outside the allow-list above.
* The Gemini adapter is covered with an injected fake SDK client (adapter
  takes the client as a constructor argument); an opt-in
  `@pytest.mark.live_llm` test hits the real API only when
  `FOLLOWUP_GEMINI_API_KEY` is set (excluded from the default run, not from
  coverage accounting — the code it exercises is already covered by the
  fake).
* `__main__` entry points are thin and covered via `typer.testing.CliRunner`.

## 6. Mutation testing (signal that tests bite)

`mutmut` on `domain/policy.py`, `domain/identity.py`,
`domain/messages.py` — target **≥ 90 % killed** mutants; run in a nightly /
manual CI job (too slow for every push). Surviving mutants are either killed
with a new test or documented as equivalent.

As built (M8): `make mutation` (scope in `[tool.mutmut]`,
gate in `scripts/mutation_gate.py`), nightly workflow `nightly.yaml`. The
first run killed 583/613 (95.1 %); the survivors exposed missing boundary
tests (R5 priority at one day, R6 dependency due today, R8 kind and
reason, message length limits, fact caps), a dead branch in the blocker
logic (removed) and one real gap: the draft validator replaced
placeholders with a space, so `{project}.com` passed (fixed, see `04`
§2b). Current score **602/608 (99.0 %)**. The six survivors are
equivalent:

| Mutant | Why it is equivalent |
|--------|----------------------|
| `encode("utf-8")` → `encode("UTF-8")` | Same codec. |
| `ensure_ascii=False` → `ensure_ascii=None` | Both falsy. |
| `body.replace("\n", "")` → `replace("\n", "XXXX")` | Only removes newlines before the control-character search; either replacement has no control characters. |
| placeholder stand-in `"x"` → `"XXxXX"` / `"X"` (2) | Any word stand-in keeps the adjacency check; case is ignored by the patterns. |
| recipient error message padded with `XX` | Tests match a substring of the message. |

## 7. Test data

* `fixtures/tasks.json` — the official fixture (golden snapshot).
* `tests/fixtures/unseen_*.json` — hand-written variants of the same schema:
  synonyms (`"in progress"`, `"DONE "`), lowercase IDs, ISO datetimes, empty
  owner strings, duplicate IDs, future `last_update`, very long notes, unicode
  homoglyph injection, HTML in notes, null everything.
* No real names/e-mails beyond the anonymized fixture; recipients resolve to
  `@example.test` addresses in the mock directory.

## 8. Commands

```text
make lint        # ruff check + ruff format --check + import-linter
make type        # mypy --strict src tests
make test        # pytest -n auto --cov --cov-branch (fails < 100 %)
make test-fast   # unit + property only, no Docker
make mutation    # mutmut run on domain
make demo        # compose up, migrate, run, dispatch, rerun, print report
```
