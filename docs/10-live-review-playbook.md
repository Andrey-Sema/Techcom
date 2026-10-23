# 10 — Live Technical Review Playbook (30 minutes)

## Final revision rehearsal boundary

This playbook is prepared for the final integration; no live rehearsal was
run after the instruction to avoid environment verification. Review the
[current evidence boundary](23-final-integration-review.md) first.

When execution is authorized on a review host, demonstrate `make demo`,
compare its ten decisions/seven deliveries/seven duplicate suppressions,
then run `make bench` and retain its machine metadata and 60/30-second
acceptance result. Demonstrate scoped API reads including a quiet source
project, shared request admission across processes, recipient quota deferral
without a send, and replay after payload retention. Check private metrics,
collector export/drop counters, and `make zap` against synthetic data.
Verify an actually published image digest's attestation separately. Until
those steps run, no rehearsal, DAST, new benchmark or signature is claimed.

## Part 1 (10 min) — Run the system

```text
make demo
```

Narrate while it runs (≈ 90 s):

1. `docker compose up -d db mock-api` → `alembic upgrade head`.
2. `followup run --as-of 2026-10-21 --dispatch` → table: 10 tasks, 7
   actions, 3 no-action; TC-1006 flagged; LLM calls = N (stub or Gemini).
3. Show `samples/decisions.json` entry for TC-1002 (`request_eta` →
   warehouse) and TC-1008 (`escalate`, note/status mismatch).
4. `followup run` again → "0 enqueued, 7 duplicate_suppressed"; delivery
   log still 7 lines (`wc -l var/delivery_log.jsonl`).
5. `FOLLOWUP_MOCK_FAULTS=messages:timeout_after_commit@1 make demo-fault` →
   logs show `delivery.unknown` → `delivery.reconciled`; one log line.

## Part 2 (10 min) — Unseen edge case & debugging

Likely injected issues and how to diagnose them fast:

| Injected issue | Symptom | Where to look | Fix |
|----------------|---------|---------------|-----|
| Off-by-one in overdue (`<=` vs `<`) | TC-1009 becomes overdue | `test_due_today_not_overdue` fails; `policy.py` R7 | restore strict `<` |
| Fingerprint includes `last_update` or run id | rerun sends duplicates | S4 test; compare two `idempotency_key`s in `decisions` | remove volatile field |
| Receiver ignores `Idempotency-Key` | S6 forced re-POST creates 2 lines | delivery log grouped by `operation_id` | restore lookup before commit |
| Retry on 400 | flood of attempts in logs | `followup_retries_total`; retry classifier test | fix status set |
| Timeout treated as failure → re-POST without key | duplicate | audit trail order: `attempt, attempt` without `unknown` | route timeouts to `unknown` |
| LLM returns new enum value | validation error | `llm.fallback` audit with stage `schema` | decide: extend enum + tests, or keep fallback |
| New status `"On Hold"` in unseen fixture | `unknown_status` clarifications | rejected/unknown counts in run report | add synonym in `policy.toml` + test |
| Timezone (`datetime.now()` local) | dates shift near midnight | ruff `DTZ` + `FixedClock` tests | use injected UTC clock |

Debugging narrative to say out loud: *reproduce with a test first → read
the audit trail for the task (`followup report --task TC-xxxx`) → bisect by
layer (normalize → policy → key → outbox → delivery) → fix → test stays as
regression.*

## Part 3 (10 min) — Scale, cost, access

Use `09-operations-at-scale.md`. Key sentences:

* "10k/day is 0.12 per second — throughput is trivial; the risks are
  duplicates, unsafe actions, rate limits and cost."
* "Rules first, LLM only for ambiguity, cached by content hash, smallest
  model, batch for nightly scans — roughly cents per day, tracked as a
  metric with an alert."
* "A timeout means unknown outcome. Same idempotency key or reconcile by
  operation id — never a blind retry."
* "The model has no tools and no write path. Least privilege for tokens and
  DB roles; humans approve anything that changes project state."

## Likely live change requests (rehearse each < 5 min)

1. Escalate after 3 days instead of 5 → `policy.toml` + update golden + test
   boundary.
2. Route `permit` blockers to "permitting team" → `routing.toml` + rule test.
3. Add a field to the output record → model + JSON Schema + golden.
4. Make the LLM draft messages (switch `FOLLOWUP_LLM_PROVIDER=gemini`) and
   show validator rejecting a bad draft from the stub.
5. Add re-reminder after N days for unchanged overdue tasks → add a
   `reminder_window` bucket (e.g. ISO week) into the key; explain trade-off.

## Interview answers to correct (see `01`)

Prepare crisp 20-second versions for: AI vs ML vs LLM; what an API is;
agent vs workflow; why key rotation across free tiers is not a scaling plan;
timeout semantics and idempotency.
