# 12 — Plan Self-Review (red-team pass)

The plan was re-read end to end against the brief, the rubric, the hard gate
("no silent duplicates or unsafe actions") and the interview. Findings and
resolutions:

## 1. Verification performed

* **Rule table dry-run**: a throwaway prototype of R1–R9 (keyword prefilter
  only, no LLM) was executed on `fixtures/tasks.json` with `as_of =
  2026-10-21`. Output matched `00` §6 exactly: 7 actions (TC-1001, 1002,
  1004, 1005, 1006, 1008, 1009) and 3 `no_action` (TC-1003, 1007, 1010);
  TC-1006 flagged.
* **Cross-document consistency**: rule ids, decision enum, status codes,
  retry ownership, action counts and placeholder lists checked across
  `00`–`11` and ADRs.
* **Hard-gate walk-through**: every path that produces an outbound message
  passes through `UNIQUE(idempotency_key)`, and every HTTP write carries a
  stable `Idempotency-Key`; no code path can mutate task state.

## 2. Findings and fixes

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| 1 | LLM fallback `UNCLEAR` originally fell into the escalation rule, so an LLM outage would have escalated every overdue task with notes to managers (noisy, and outage-dependent behaviour). | High | `UNCLEAR` now falls through to R7 (owner follow-up). LLM failure can neither escalate nor silence a task. |
| 2 | Retries were described in both the HTTP adapter and the dispatcher for `POST` (retry amplification; a read timeout could have been re-POSTed in-call). | High | One layer per operation: reads retry in-call; writes make a single attempt and the dispatcher retries durably; read timeouts → `unknown` → reconcile. |
| 3 | Idempotency mismatch status was inconsistent (409 vs 422). | Medium | Aligned with the IETF draft: 422 = key reused with different payload (`dead`, bug alert); 409 = same key still processing (retryable). |
| 4 | Escalation template used placeholders not in the validator allow-list. | Medium | Split allow-lists: LLM placeholders vs code-owned template placeholders (`{owner_name}`, `{reason_text}`). |
| 5 | Claim "demo needs zero LLM calls" was wrong: TC-1001's note is not keyword-classifiable. | Low | Corrected: one LLM call (TC-1001); its fallback produces the same decision, so the demo is stable with or without a key. |
| 6 | LLM-needed predicate could have called the LLM for tasks already decided by R1/R3/R4. | Low | Predicate now requires that no earlier rule applies (cost + determinism). |
| 7 | PEP 8 strictness vs. ruff's default 88 columns. | Low | `line-length = 79` (docstrings/comments 72) — the user asked for strict PEP 8. |
| 8 | Python version: user asked for 3.13; 3.15 shipped on 2026-10-09. | Decision | Baseline ≥ 3.13, CI 3.13 + 3.14, image 3.14; 3.15 as a later non-blocking canary (wheel availability). |
| 9 | "Pompous" scope vs. the brief's 4-hour timebox. | Risk | Core (M0–M5) carries the full reliability story; polish is optional and capped (≤ 2.5k LOC in `src/`); README is honest about time spent and omissions. |
| 10 | Interview answer on scaling proposed rotating many free-tier keys. | Risk (reputational) | Playbook replaces it with paid tier / quota increase / batching / caching; explicitly mentions provider terms. |
| 11 | Injection screen false positives (e.g. "will mark complete after inspection"). | Low | Consequence of a false positive is only "template message, no LLM" — safe; corpus of benign negatives in tests keeps the rate visible. |
| 12 | Model ids/prices for Gemini change frequently; official docs were not reachable from the research environment. | Low | Model id and prices are configuration; verify against the official model list and pricing page on implementation day. |
| 13 | Requested new private GitHub repository could not be created from this environment (integration lacks permission). | Process | Plan lives in the existing private repository on branch `claude/interview-product-architecture-rwjo7u`; it can be moved to a dedicated repo unchanged. |

## 2a. Second review pass (2026-10-22): files, observability, ASVS L2

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| 14 | `.gitignore`, `.dockerignore`, full `pyproject.toml`, Dockerfile, compose, Prometheus/Grafana/Loki configs were only named, not specified. | Medium | `13-repository-blueprint.md` specifies each file's content. |
| 15 | Logging was described at a medium level (structlog + audit), without inventory, event catalogue, classification or shipping. | Medium | `14-logging-and-observability.md` (ASVS V16, OWASP Logging + Logging Vocabulary). |
| 16 | REST used a static bearer key — violates ASVS 13.2.1 (L2: no unchanging credentials between components). | High | Short-lived EdDSA service JWTs with scopes (`15` D1), deny-by-default. |
| 17 | Draft placeholders could have been rendered with `str.format` — format-string injection from LLM output (ASVS 1.3.7/1.3.10). | High | Regex renderer over an allow-listed mapping; test with `{x.__class__}`. |
| 18 | The first blueprint mounted the Docker socket into Alloy — violates OWASP Docker Security rule #1. | High | Logs exported over OTLP; no socket mounts (AC-S6). |
| 19 | Mock API package would have shipped in the production image (ASVS 15.2.3). | Medium | Mocks are a uv workspace member with their own image target. |
| 20 | No business limit against message storms if task state flaps (ASVS 2.3.2/2.4.1). | Medium | Per-recipient daily cap (D6). |
| 21 | Gemini free tier may use submitted content to improve products (ASVS 14.2.3). | Medium | Paid/enterprise tier for real data; verify provider terms; data minimisation. |
| 22 | PostgreSQL 18 Docker image moved `PGDATA`; old volume path would silently lose data on re-create. | Medium | Volume mounted at `/var/lib/postgresql`. |
| 23 | Prometheus would not reach the mock on the internal network. | Low | Mock not scraped; only the app's internal metrics port. |
| 24 | Internal TLS and dynamic credentials are not practical in a local demo. | Accepted | Documented ASVS deviations with production fix (`15` §4 V12/V13). |

## 2b. Third review pass (2026-10-22): strict 30-point audit

See `16-senior-audit.md`: 6 points were proven by mechanism, 14 partial,
10 missing (notably LLM quality evidence, complexity/dead-code gates,
graceful shutdown, single-run lock, API fuzzing, benchmark evidence,
repository hygiene files, scope tiers). All 30 now have a mechanism in
the plan; tooling consolidated in `17`, operations in `18`.

## 3. Residual risks (accepted)

* Reviewers without Docker cannot run integration tests — mitigated by
  `make test-fast` (unit + property, no Docker) and the committed samples.
* Unchanged overdue tasks are reminded only once (by design of the key);
  cadence is a product decision listed as a production next step.
* Keyword prefilter is English-only; unseen fixtures in other languages fall
  back to the LLM (or `UNCLEAR` → owner follow-up).

## 4. Verdict

The plan meets every MUST criterion in `07` by construction, keeps the LLM
on a short leash, and gives the live review concrete, rehearsed material.
Ready for implementation starting with M0. Security (M9) and observability
(M10) are planned work items with acceptance criteria, not afterthoughts.
