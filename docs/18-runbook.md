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
| `followup maintenance prune` | Applies retention (see below); run it with the maintenance role | no (by design) |

`dead` is terminal by design (ASVS 2.3.1): there is no command that
moves a dead letter back to `pending`. After the cause is fixed, a human
follows up on the listed tasks; the next run proposes a new action only
if the task state changed.

## followup-dead-letters {#followup-dead-letters}

* **Meaning:** an action reached `dead` (permanent 4xx, key mismatch, or max
  attempts).
* **First look:** Grafana → Delivery row; Loki
  `{service_name="followup"} | event="delivery.dead"`; read `status_code`.
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

## followup-target-down {#followup-target-down}

* **Meaning:** Prometheus has not scraped the service for 5 minutes, so
  every other alert is blind.
* **Check:** is the service running and attached to the
  `techcom-observability` network? Does `/metrics` answer on the internal
  port 9464? Until the API (M6b) exists this alert is expected in the
  local stack.

## Diagnostics toolbox (never in the production image)

| Question | Tool | How |
|----------|------|-----|
| CPU hot path right now | py-spy | Run a **separate debug container** sharing the PID namespace with `cap_add: [SYS_PTRACE]` (the app container keeps `cap_drop: ALL`): `py-spy dump --pid 1`, `py-spy record -o var/profile.svg` |
| Memory growth | memray | Reproduce locally with the same input: `memray run -o var/memray.bin -m techcom_followup ...` |
| Slow queries | PostgreSQL | `log_min_duration_statement`, `EXPLAIN (ANALYZE, BUFFERS)` |
| "Why did task X get message Y?" | audit trail | `followup report --run-id <uuid>` (decisions with rule, reason, key and message; actions with attempts and delivery state); across runs, read-only SQL: `SELECT kind, at, run_id FROM audit_events WHERE task_id = 'TC-1001' ORDER BY id` |

## Database roles (least privilege, D3)

* Migrations run as the schema owner (the migrator). Migration `0002`
  creates the NOLOGIN group role `followup_app` if it is missing and
  grants it DML only: `SELECT, INSERT, UPDATE` on runs, decisions and
  actions, `DELETE` additionally on `llm_cache`, and only
  `SELECT, INSERT` on `audit_events`. `PUBLIC` loses `CONNECT` on the
  database and `USAGE` on schema `public`.
* The service connects as its own login role, a member of the group:

  ```sql
  CREATE ROLE followup_service LOGIN PASSWORD '<from the secret manager>'
      IN ROLE followup_app;
  ```

* Rotate by creating a second login role, switching the secret, then
  `DROP ROLE` on the old one. Never grant the service role ownership or
  `TRUNCATE`; `tests/integration/persistence/test_db_roles.py` lists
  what it must not be able to do.

## Retention (`followup maintenance prune`)

* Limits live in `config/retention.toml` and follow the data
  classification (`15` §8): decisions without actions and empty runs
  after 90 days; sent and dead actions, and the audit trail, after one
  year; expired LLM cache entries at once. Pending, in-flight and
  unknown actions, and decisions they refer to, are never deleted.
* Run it daily, with a login role that is a member of
  `followup_maintenance` (migration `0003`: `SELECT` and `DELETE` only,
  no `INSERT`/`UPDATE`, so it cannot forge history):

  ```bash
  FOLLOWUP_DATABASE_URL=postgresql+psycopg://followup_retention:...@db/followup \
      followup maintenance prune
  ```

* Each invocation deletes at most `batch_size` rows per table per
  transaction and stops after `max_batches`; `"complete": false` in the
  output means the next run continues.
* Trade-off: deleting a sent action deletes its idempotency key, so a
  task whose state stays exactly the same for more than a year gets one
  new reminder.

## Backup & restore

* `make db-backup` → `pg_dump --format=custom` into `var/backups/`
  (owner-only directory and files: dumps contain personal data).
* `make db-restore FILE=var/backups/…` restores into the compose
  database with `--clean --if-exists --exit-on-error
  --single-transaction`: it either fully succeeds or changes nothing.
* Restoring into a **new cluster**: create the NOLOGIN roles first
  (`CREATE ROLE followup_app NOLOGIN; CREATE ROLE followup_maintenance
  NOLOGIN;`) so the table grants restore, then grant `CONNECT` on the
  database to both (database-level grants are not part of the dump).
* `tests/integration/persistence/test_backup_restore.py` proves the round
  trip with the same commands: rows, migration version and grants
  survive.
