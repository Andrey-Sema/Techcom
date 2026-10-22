# 18 — Operations Runbook

One entry per alert in `deploy/prometheus/rules/followup.rules.yml`. Alert
annotations carry `runbook_url: docs/18-runbook.md#<anchor>`.

Golden rule: **when unsure, stop sending, not deciding** — set
`FOLLOWUP_DISPATCH_ENABLED=false` (outbox keeps accumulating safely; nothing
is lost, nothing is duplicated) and investigate.

## Global levers

| Lever | Effect | Reversible |
|-------|--------|-----------|
| `FOLLOWUP_DISPATCH_ENABLED=false` | Dispatcher stops claiming; actions stay `pending` | yes |
| `FOLLOWUP_LLM_PROVIDER=stub` | Keyword prefilter + templates only | yes |
| `followup run --dry-run` | Decisions computed and reported, nothing enqueued | n/a |
| `followup actions requeue --status dead --id <uuid>` | Moves a reviewed dead letter back to `pending` (same key) | yes |
| `followup maintenance prune` | Applies retention | no (by design) |

## followup-dead-letters {#followup-dead-letters}

* **Meaning:** an action reached `dead` (permanent 4xx, key mismatch, or max
  attempts).
* **First look:** Grafana → Delivery row; Loki
  `{service="followup"} | json | event="delivery.dead"`; read `status_code`.
* **Decide:** 422 key mismatch → bug (payload changed for the same key):
  open an incident, do **not** requeue. 400/404 → bad target/routing: fix
  routing config, then requeue. Max attempts on 5xx → provider outage:
  requeue after recovery.

## followup-outbox-stuck {#followup-outbox-stuck}

* **Meaning:** oldest `pending`/`unknown` action older than 30 min.
* **Check:** kill switch accidentally off? Delivery circuit open? Dispatcher
  process alive (`/readyz`)? Rows with `lease_until` far in the future
  (clock skew)?
* **Act:** restart dispatcher (leases expire → safe resend with same key);
  `unknown` rows are reconciled first, never blindly re-sent.

## followup-llm-fallback-rate-high {#followup-llm-fallback-rate-high}

* **Meaning:** > 5 % of LLM calls failed or were rejected for 15 min.
* **Check:** `llm.fallback` events grouped by `stage`. `transport` →
  provider outage/quotas; `schema`/`grounding` → model or prompt drift.
* **Act:** outage → nothing (fallback is safe), maybe switch to `stub` to
  save retries; drift → pin previous model id, run eval harness, open a
  prompt-version change.

## followup-llm-cost-budget {#followup-llm-cost-budget}

* **Meaning:** 24 h spend above budget.
* **Check:** cache hit ratio, LLM calls per run, unexpected note churn.
* **Act:** the daily budget guard already degrades to keyword+template;
  investigate the cause (e.g., a sync job rewriting notes).

## followup-circuit-open {#followup-circuit-open}

* **Meaning:** a dependency failed repeatedly; calls are short-circuited.
* **Act:** verify dependency health; the breaker half-opens automatically.

## followup-injection-spike {#followup-injection-spike}

* **Meaning:** unusual number of flagged notes.
* **Check:** which projects/sources; are they real attacks or a new benign
  phrase (false positive)?
* **Act:** no system risk (no capability); inform the data owner; extend
  benign corpus if false positive.

## followup-auth-failures {#followup-auth-failures}

* **Meaning:** burst of rejected tokens.
* **Check:** `security.authn_failed` reasons (`expired`, `bad_signature`,
  `wrong_audience`); source.
* **Act:** expired → client clock/rotation issue; bad signature → possible
  attack: block source at the edge, rotate keys if leakage suspected.

## Diagnostics toolbox (never in the production image)

| Question | Tool | How |
|----------|------|-----|
| CPU hot path right now | py-spy | Run a **separate debug container** sharing the PID namespace with `cap_add: [SYS_PTRACE]` (the app container keeps `cap_drop: ALL`): `py-spy dump --pid 1`, `py-spy record -o var/profile.svg` |
| Memory growth | memray | Reproduce locally with the same input: `memray run -o var/memray.bin -m techcom_followup ...` |
| Slow queries | PostgreSQL | `log_min_duration_statement`, `EXPLAIN (ANALYZE, BUFFERS)` |
| "Why did task X get message Y?" | audit trail | `followup report --task TC-1001` (decision, rule, key, attempts, delivery) |

## Backup & restore

* `make db-backup` → `pg_dump --format=custom` to `var/backups/`;
  `make db-restore FILE=…` into a fresh database.
* Restore is verified by a weekly scheduled CI job that restores the
  latest dump into a throwaway container and runs `followup report`.
