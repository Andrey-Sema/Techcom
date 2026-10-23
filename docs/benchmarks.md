# Benchmarks

`make bench` runs `tests/bench/test_10k.py`: 10,000 seeded synthetic
tasks (the fixture's shape, five projects, missing owners and dates,
materials/permit/dependency/injection notes) through `ProcessTasks`
composed as `followup run` composes it — PostgreSQL 18 with a pooled
engine, advisory run lock, note interpreter on the offline stub (model
calls fail fast, ambiguous notes take the fallback) and template drafts.
It then runs again over the same data. The test writes
`var/bench/results.json`.

## Historical results

Measured on 2026-10-22: 4 vCPU Intel Xeon @ 2.80 GHz, PostgreSQL 18
in Docker on the same host, Python 3.14.6.

| Measure | Before | After | Original target | Guard at measurement |
|---------|--------|-------|-----------------|-------------------|
| First run, 10,000 tasks | 293.9 s | 83.0 s | < 60 s | < 120 s |
| Rerun, all duplicates | 283.9 s | 71.2 s | < 30 s | < 120 s |
| Peak RSS | 218 MB | 218 MB | < 300 MB | < 300 MB |
| Decisions / enqueued / rerun duplicates | 10,000 / 6,360 / 6,360 | same | — | asserted |

**Before → after.** The first run of the benchmark found two costs:

1. The benchmark itself used the test helper's `NullPool`, so every
   transaction opened a new connection; it now uses the pooled engine
   that `bootstrap.py` builds.
2. `PgRepository.record_decision` made eight round trips per task. It
   now relies on the foreign key to prove the run exists, skips the
   outcome update for decisions without an action, and returns a fresh
   insert from the write instead of reading it back (the contract test
   compares that result with a replay read from the database).

**Historical bottleneck.** Every run records every
decision, action and audit event in its own transaction, by design
(audit of each run, per-decision atomicity). A rerun therefore does the
same writes as the first run except the action inserts; the 30 s rerun
target assumed it would be cheap. Writing one decision costs about 7 ms
here even without any other processing (four to five statements).

**Operational meaning.** 10,000 tasks a day is 0.12 tasks per second
(`09`); 83 s per 10,000 tasks is about 120 tasks per second, three
orders of magnitude of headroom.

**The measured concurrency lever.** Four concurrent writers bring 1,000
decision writes from 7.3 s to 3.4 s (×2.1). Using it means bounded
concurrency in `ProcessTasks` and a stable report order (reports list
decisions by row id in that historical implementation).

## Current implementation and acceptance gates

The final integration implements eight concurrent writers, bounded to
1..16, while keeping interpretation/drafting sequential. Each decision,
outbox identity and audit remain atomic. PostgreSQL combines the existing
decision outcome update and audit insert in one data-modifying CTE,
removing another network round trip for actionable decisions. Explicit
source positions preserve report order regardless of transaction scheduling.
The benchmark pool matches runtime sizing (writers plus two, zero overflow)
so parallel batches reuse connections instead of opening overflow connections
and discarding them after every batch.
Existing messages are fetched by the current bounded identity batch, and
CLI reports read actions for their own run. Historical outbox size therefore
does not determine the number of payloads materialized by these paths.

The test now requires **first run < 60 s**, **rerun < 30 s** and
**peak RSS < 300 MB** for the same seeded 10,000-task workload. Counts and
duplicate assertions are retained. Results record writer concurrency.
This is the offline-stub workload; live-provider latency is outside that
measurement contract.
The CI benchmark job runs the same acceptance test and retains machine
metadata/results as an artifact. Its result on the final revision is not
assumed before the workflow actually completes.

**No current result exists.** The user prohibited environment execution.
The implementation and benchmark test received manual review; neither
the new time gates nor memory/coverage gates were executed. Do not reuse
the historical 83/71-second figures as evidence for the new revision.
