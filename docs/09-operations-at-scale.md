# 09 — Operating at 10,000 Tasks/Day

## 1. Back-of-the-envelope

| Quantity | Value | Comment |
|----------|-------|---------|
| Tasks evaluated / day | 10,000 | ≈ 0.12 tasks/s average; even 10× peak is < 2/s |
| Rule-only decisions | ~70–80 % | Done, missing data, clear dates, keyword-detected blockers |
| LLM interpretations / day | ≤ 2,000–3,000 before cache | only ambiguous notes on actionable tasks |
| After content-hash cache | ~1,000 | notes rarely change between runs |
| Tokens per interpretation | ~500 in / ~80 out | short system prompt + few-shot (cacheable prefix) + note |
| Daily LLM tokens | ~0.5 M in / ~0.08 M out | |
| Daily cost (small model tier) | **≈ $0.30 – $0.50** | `in × price_in + out × price_out`; prices from config, verify against the provider's pricing page |
| Messages / day | a few hundred | dedupe keeps it proportional to *state changes*, not runs |

Conclusion: throughput is not the problem; **correctness, rate limits, cost
visibility and access control** are.

## 2. Architecture changes at scale

1. **Event-driven ingestion**: monday.com webhooks → small ingress that
   stores the event (dedupe by event id) → incremental processing of the
   changed item; a nightly full scan reconciles missed webhooks.
2. **Horizontal workers**: N stateless workers; outbox claims with
   `SKIP LOCKED` already make dispatchers safe to scale.
3. **Partitioning**: `actions` / `audit_events` partitioned by month;
   retention job; idempotency keys at the receiver retained ≥ 24 h.
4. **LLM economics**: keyword prefilter first; content-hash cache; smallest
   adequate model tier; prompt-prefix caching where supported; **Batch API**
   (≈ 50 % cheaper, hours of latency) for the nightly full scan, synchronous
   calls only for webhook-driven urgent items.
5. **Rate limits**: per-provider token bucket shared via PostgreSQL (or
   Redis if multiple regions), adaptive concurrency on 429. Higher quotas via
   paid tier / quota requests — never by rotating free-tier keys (terms of
   service).
6. **Provider resilience**: circuit breaker → template fallback today;
   optional second provider behind the same port tomorrow (same schemas,
   same validator, own golden tests).

## 3. Monitoring & cost control

| Signal | Metric | Alert |
|--------|--------|-------|
| Cost | `followup_llm_cost_usd_total` (daily) | > budget × 1.2 |
| LLM quality | fallback rate, validation failures by stage | > 5 % for 15 min |
| Safety | `note.flagged` count | spike vs 7-day baseline |
| Delivery | outbox backlog by status, age of oldest `pending`/`unknown` | oldest > 30 min |
| Dead letters | `dead` count | any (each one is investigated) |
| Duplicates | receiver replays (`Idempotent-Replayed`) | trend only — replays are *working as intended*, but a rise means timeouts upstream |
| Dependency health | circuit state, retry rate | breaker open > 5 min |

Dashboards: one Grafana board with the above; logs searchable by `run_id`,
`task_id`, `operation_id`.

## 3a. Rollout strategy (added after `16` #22)

1. **Shadow mode (1–2 weeks):** the service decides and logs, dispatch is
   disabled (kill switch). Ops compare its decisions with what humans
   actually did; disagreements feed the eval set and rule tuning.
2. **Canary:** enable dispatch for one project (routing flag), watch dead
   letters, fallback rate, per-recipient cap hits and recipient feedback.
3. **General availability** per project; kill switch stays as the first
   incident lever (`18`).
4. **Rollback:** disable dispatch → outbox keeps state → no data loss; a
   policy rollback is a config revert plus golden update.

Time zones and quiet hours (US multi-state crews) are handled at delivery
time (`04` §2a), not in the decision policy.

## 4. Limiting access (least privilege)

* **LLM**: no tools, no write access, minimal data (note + title only), PII
  minimised; outputs are proposals.
* **Service credentials**: task API token is *read-only*; delivery token can
  only create messages; separate DB roles — app (DML on its tables), migrator
  (DDL), read-only analyst.
* **API**: short-lived EdDSA service JWTs with scopes today (local issuer);
  production tokens from the company IdP via OAuth 2.0 client credentials
  or workload identity federation, same claims and scopes (`15` §5); every
  trigger audited with the token subject.
* **Secrets**: env from a secret manager; rotation without redeploy; never
  in logs (redaction processor + test).
* **Human-in-the-loop**: escalations and any future "write back to CRM"
  capability go through an approval queue; the AI never gets direct write
  authority over project status.
