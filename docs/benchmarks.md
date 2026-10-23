# Benchmarks

`make bench` runs `tests/bench/test_10k.py`: 10,000 seeded synthetic
tasks (the fixture's shape, five projects, missing owners and dates,
materials/permit/dependency/injection notes) through `ProcessTasks`
composed as `followup run` composes it: PostgreSQL 18 with a pooled
engine, advisory run lock, note interpreter on the offline stub (model
calls fail fast, ambiguous notes take the fallback) and the bounded
orchestrator on the offline model (three steps per new action). It then
runs again over the same data and writes `var/bench/results.json`.

## Results

Measured on 2026-10-23 (after ADR-0018): 4 vCPU x86_64, PostgreSQL 18 in Docker on the
same host, Python 3.14.6, eight concurrent writers.

| Measure | Sequential writes, templates | Parallel writes, templates | Parallel writes + orchestrator (current) | Target |
|---------|------------------------------|----------------------------|------------------------------------------|--------|
| First run, 10,000 tasks | 110.4 s | 55.6 s | **89.1 s** | < 60 s |
| Rerun, all duplicates | 97.7 s | 53.2 s | **57.6 s** | < 30 s |
| Peak RSS | 229 MB | 194 MB | **202 MB** | < 300 MB |
| Decisions / enqueued / rerun duplicates | 10,000 / 6,360 / 6,360 | same | same | asserted |

The test guards against regressions at 120 s per run and 300 MB.

**Where the time goes.** Each decision, its outbox row and its audit
receipt are one transaction (per-decision atomicity), so a run is about
10,000 short transactions; eight writers overlap them. The orchestrator
adds, per new action, one indexed history lookup (`decisions_task_idx`)
and about 2 ms of CPU for three loop steps, the guard and composing
the message from its intents. Planning runs
concurrently within each batch and its audit trace is written in the
decision's own transaction. A rerun never calls the model: every slot is
already taken.

**What the targets would need.** Multi-row inserts per batch for
decisions and receipts (one transaction per batch instead of per task)
and a batched history lookup. Both trade per-decision isolation for
throughput and are not needed at the stated volume.

**Operational meaning.** 10,000 tasks a day is 0.12 tasks per second
(`09`); 89 s per 10,000 tasks is about 110 tasks per second. With a real
model, latency dominates: up to four calls per *new* action, eight
actions planned concurrently per batch, bounded by per-call timeouts,
the circuit breaker and the daily budget.

## API

Measured in-process (ASGI, in-memory store, one worker): 10,000
authenticated `GET /v1/actions` requests take about 12.5 s (≈ 800 rps;
EdDSA verification dominates). The PostgreSQL-shared limiter admits
about 420 requests per second for the whole deployment, because each
admission serializes on one advisory lock per namespace; that is the
ceiling to raise first if the API sees real traffic.
