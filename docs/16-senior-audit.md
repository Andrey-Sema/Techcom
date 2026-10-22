# 16 — Strict Senior-Level Audit (30 points)

Method: every claim the plan makes is tested against one question — **"what
mechanism would make the build fail (or the reviewer see hard evidence) if
this claim were false?"** Intentions, prose and diagrams do not count.

Verdict scale:

* ✅ **Proven by mechanism** — a constraint, test, or CI gate makes the
  claim falsifiable and enforced.
* ⚠️ **Partial** — mechanism exists but has a hole, or evidence is only
  arithmetic / self-referential.
* ❌ **Refuted / missing** — no mechanism; the claim was an intention.

Every ⚠️/❌ has a fix. The "After" column shows the state once that fix is
in the plan (this revision).

## 1. The 30 points

| # | Area / claim | Proving (or refuting) mechanism | Before | Gap found | Fix (now in plan) | After |
|---|--------------|---------------------------------|--------|-----------|-------------------|-------|
| 1 | Correct decisions on the official fixture | Prototype of R1–R9 executed on `tasks.json` (`12` §1) → planned syrupy golden snapshot | ✅ | — | Golden test is the regression anchor; any rule change shows as a snapshot diff. | ✅ |
| 2 | Robustness on unseen fixtures | Normalization table + hand-written "unseen" corpus | ⚠️ | Hand-written cases only cover what we thought of; wrong JSON *types* (`due_date: 20261018`, `task_id: 1001`, nested objects) not covered. | Hypothesis strategy generating arbitrary JSON values per field (`st.recursive`) → property "`normalize` never raises; returns `Task` or `RejectedRecord`". | ✅ |
| 3 | No duplicate follow-ups on rerun | `UNIQUE(idempotency_key)` + `INSERT … ON CONFLICT DO NOTHING`; S4 test; concurrent-run test | ✅ | — | The constraint (not application code) is the guarantee — the strongest possible mechanism. | ✅ |
| 4 | No duplicate write after timeout-after-commit | Idempotent mock receiver + reconcile + S6 test | ⚠️ | **Self-referential**: we prove safety against *our own* idempotent mock. A real provider may ignore `Idempotency-Key`. | Reconcile-before-resend is mandatory (an `unknown` action is never re-POSTed before a lookup). New test runs S6 against a deliberately **non-idempotent** receiver mode and still expects exactly one message. Provider contract assumptions documented in the adapter docstring. | ✅ |
| 5 | Bounded retries, no flooding | stamina config, retry budget, breaker; recording-sleeper tests | ✅ | Retry budget and breaker are custom code. | Unit + property tests (budget never exceeded for any failure sequence). | ✅ |
| 6 | LLM cannot invent owner/date/status | Output schemas have no such fields; test asserts the exact field set; placeholders filled by code | ✅ | — | Capability removal is provable by a schema field-set test. | ✅ |
| 7 | Prompt injection is neutralised | No tools / no write path (capability) + closed schema + screen | ⚠️ | The regex screen is bypassable (homoglyphs, base64, other languages). Claiming "detected" would be dishonest. | Docs state the screen is a *signal*; safety rests on capability removal. A red-team corpus (≥ 30 variants incl. homoglyph/encoded/multilingual) is part of the eval set (#9); the metric is "unsafe effects = 0", not "detection rate = 100 %". | ✅ |
| 8 | Malformed LLM output is safe | Parametrized S7 test | ⚠️ | Six hand-picked malformed outputs ≠ "any output". | Property test: for **any** string returned by the stub (`st.text()`, `st.from_regex` JSON-ish), the pipeline never crashes and an overdue non-Done task is never `no_action`. | ✅ |
| 9 | LLM interpretation quality | — | ❌ | **No evidence mechanism at all.** "Works on 1 note" is not an AI-engineering standard. | Eval harness (`ADR-0012`): ≥ 60 labelled synthetic notes (materials, upstream, permit, site access, none, ambiguous, injection); metrics per class (precision/recall, macro-F1), confusion matrix; recorded provider responses replayed in CI; gate: macro-F1 ≥ 0.85 and **zero unsafe outcomes**; prompt version bump requires an eval report diff. | ✅ |
| 10 | Deterministic, reproducible runs | Fixed clock, stub LLM, content-hash cache | ⚠️ | Live Gemini is not bit-deterministic even at temperature 0. | Golden/scenario tests use the stub or recorded responses only; live calls are opt-in (`live_llm` marker) and evaluated statistically (#9), never by exact string. | ✅ |
| 11 | 100 % coverage is honest | `fail_under = 100`, branch coverage, pygrep ban on `pragma: no cover` | ✅ | Coverage ≠ assertions. | Mutation testing (≥ 90 % killed on domain) as the counter-proof; mutmut on 3.13 with cosmic-ray as fallback if mutmut lags new Python versions. | ✅ |
| 12 | Type safety | `mypy --strict`, extra error codes | ⚠️ | Untyped third-party modules could silently become `Any`. | Explicit, commented override allow-list; pygrep bans `type: ignore` without an error code; Ruff `ANN401` bans `Any` in signatures. | ✅ |
| 13 | Architecture boundaries | import-linter layers contract | ✅ | — | Fails CI on any adapter import from domain. | ✅ |
| 14 | Readable, small functions | — | ❌ | No complexity gate; "≤ 2.5k LOC" was a wish. | Ruff `C90` (max 8) + pylint limits (args 6, branches 10, statements 40) + xenon (abs B / module A / avg A); LOC reported per module in CI summary. | ✅ |
| 15 | No dead code | — | ❌ | Nothing detects leftovers from refactors. | vulture (min confidence 80, framework whitelist) as a gate. | ✅ |
| 16 | Dependency hygiene & supply chain | pip-audit, `uv.lock` hashes, SHA-pinned actions | ⚠️ | No unused/missing dependency check; security scanners referenced by tag (Trivy was itself compromised in March 2026). | deptry gate; pip-audit with `--require-hashes`; all scanners pinned by digest/SHA; Dependabot with 7-day cooldown; SBOM per build. | ✅ |
| 17 | Security SAST | Ruff `S` | ⚠️ | Only Bandit-equivalent rules via Ruff; no standalone Bandit report; no project-specific rules. | **Bandit** standalone gate (`-l -ii`, no global skips, `# nosec` only with test id) + **Semgrep** registry packs + **custom invariant rules** with their own tests (`17` §7). | ✅ |
| 18 | "ASVS 5.0 L2" claim | Per-requirement table, `asvs` markers, traceability report | ⚠️ | Local compose has documented deviations (internal TLS, static per-machine credentials); claiming plain "L2 compliant" would overstate. | Wording fixed: "ASVS 5.0 L2 **design target**; verified items listed in `docs/asvs-traceability.md`; local deviations in `15`". CI fails if a *Built* item lacks a test. | ✅ |
| 19 | API security testing | Unit/API tests of known cases | ❌ | Nothing explores unknown inputs against the real HTTP surface. | **Schemathesis** (property-based, from our OpenAPI) + ZAP API scan in the smoke job. | ✅ |
| 20 | Single active run & recipient cap race-free | "run concurrency = 1" | ❌ | Mechanism was unspecified; two schedulers could run concurrently and both pass the per-recipient cap check. | `pg_try_advisory_lock(run_lock)` at run start (second run exits with `run.skipped_locked`); cap check inside the per-task transaction; test with two concurrent runs. | ✅ |
| 21 | Graceful shutdown | — | ❌ | SIGTERM mid-dispatch could leave work half-done until lease expiry. | Signal handler: stop claiming, finish/abort in-flight within `stop_grace_period`, mark unfinished as `pending` (release lease); `init: true` in compose; test sends SIGTERM to a running dispatcher. | ✅ |
| 22 | Operational safety levers | — | ❌ | No kill switch, no dry-run, no rollout strategy. | `FOLLOWUP_DISPATCH_ENABLED` kill switch (checked per claim), `followup run --dry-run` (decisions only, no enqueue), **shadow mode** rollout (decide + log, humans compare for a week) in `09`. | ✅ |
| 23 | Incident response | Alert rules exist | ⚠️ | Alerts without a runbook page = pages nobody can act on. | `18-runbook.md`: one entry per alert (meaning, first query, safe actions, escalation); alert annotations link to it. | ✅ |
| 24 | Performance claim (10k tasks/day) | Back-of-the-envelope maths | ⚠️ | Arithmetic is not evidence. | `make bench`: 10,000 synthetic tasks, stub LLM, real PostgreSQL 18; asserts wall time and peak RSS budgets; results committed to `docs/benchmarks.md`. | ✅ |
| 25 | Data retention | Retention numbers in docs | ❌ | No job enforces them. | `followup maintenance prune` (LLM cache 30 d, terminal actions/audit per policy) + integration test + scheduled run. | ✅ |
| 26 | Domain realism | UTC dates per brief | ⚠️ | US multi-state crews: UTC date ≠ local due date; messages at 03:00 local; owner names may not resolve to a contact; real APIs paginate. | Documented assumption (UTC per brief) + production design: per-project timezone in routing config, quiet hours deferral (`next_attempt_at` to next window), rule `owner_contact_unknown` → coordinator clarification, cursor pagination in the task adapter and mock. | ✅ |
| 27 | Repository hygiene files | `.gitignore`, `.dockerignore` | ❌ | Missing `.gitattributes`, `.editorconfig` content, full pre-commit, CODEOWNERS, SECURITY/CONTRIBUTING/CHANGELOG, PR template, Dependabot content; gitignore lacked profiling, SARIF, docs-build, key formats. | Full specs in `17` §3–§9. | ✅ |
| 28 | Container hardening | non-root, read-only, cap_drop, no socket | ⚠️ | No resource limits, no PID-1 init, debugger hooks possible, `restart` policy would loop the one-shot migrator. | `mem_limit`/`cpus`/`pids_limit`, `init: true`, `PYTHONBREAKPOINT=0`, Ruff `T10` (debugger), migrator `restart: "no"`; image test asserts no mocks/tests/.git. | ✅ |
| 29 | Documentation stays consistent | Manual review (`12`) | ⚠️ | 18 docs + ADRs will drift as code changes; earlier rounds already found contradictions (409/422, retries, placeholders). | MkDocs `--strict` build in CI (dead links/nav), samples diff in smoke job, ASVS traceability generated from code, ADR required for blueprint deviations. | ✅ |
| 30 | Scope vs. the brief's 4-hour timebox | Milestone plan (~25 h) | ❌ | The brief explicitly asks to stay within the timebox and document unfinished work. Shipping 25 h of work without saying so reads as ignoring requirements — a seniority *anti*-signal. | Two declared tiers (`08` §0): **Tier A "Submission core"** (reliability story complete, ≈ 19 h honest) and **Tier B "Production hardening"** (security/observability depth). README states actual time spent per tier and what was cut. Live demo never depends on Tier B. | ✅ (as a judgment call, stated openly) |

## 2. Score

| | ✅ | ⚠️ | ❌ |
|--|----|----|----|
| Before this audit | 6 | 14 | 10 |
| After fixes in this revision | 30 | 0 | 0 (plan level) |

"After" is a **plan-level** verdict. It turns into a project-level verdict
only when the gates run green on real code; until then every ✅ means "a
failing gate exists that would catch the violation".

## 3. Is the *implementation* senior-level? Strict assessment

**What would prove it (hard evidence a reviewer can check in minutes):**

1. `make demo` → 7 actions, rerun → 0, fault profile → 1 delivery line.
2. CI page: all gates green (quality, tests 3.13/3.14 @ 100 % branch,
   security, image, smoke, docs).
3. `docs/asvs-traceability.md` generated, `docs/benchmarks.md` measured,
   eval report with per-class metrics.
4. Mutation score report on the domain.
5. Git history: small Conventional Commits per milestone, not one dump.

**What would refute it (red flags the reviewers will look for):**

* Any path where an outbound message is sent without a persisted action row
  or without the stable operation id.
* LLM output used before validation, or validation that "repairs" instead
  of failing closed.
* Tests that pass because of mocks shaped like the implementation (circular
  proofs — see #4).
* Inability to change a threshold live in < 5 min (config + golden update).
* Volume without explanation: the candidate cannot explain a module in
  two minutes.

**Honest expected rubric outcome if Tier A ships fully and Tier B partially:**

| Criterion | Max | Expected | Main risk |
|-----------|-----|----------|-----------|
| Python, API & code structure | 20 | 18–20 | over-abstraction perceived |
| AI integration & validation | 20 | 17–20 | eval set quality; live Gemini quirks |
| Business logic & automation | 20 | 19–20 | rule disagreements on edge cases |
| Production reliability | 20 | 19–20 | — (strongest area) |
| Tests & documentation | 10 | 9–10 | doc volume vs. readability |
| Live technical review | 10 | 7–10 | explaining everything fluently; rehearse `10` |
| **Total** | 100 | **89–100** | |

## 4. Residual gaps that remain *by choice*

* No real IdP, no internal TLS locally (documented ASVS deviations).
* One provider (OpenAI, three Sol generations as failover); a second
  provider backend (Gemini) plugs into the same router later.
* Re-reminder cadence for unchanged overdue tasks is a product decision.
* OpenTelemetry traces prepared, not enabled.
