# Live model comparison: gpt-6-luna (high) and gpt-6.1-sol (low)

Measured on 2026-10-25 against the OpenAI Responses API. This page
records a one-off measurement; it is not replayed in CI. The CI
evidence for the primary route is the
[`luna-high.v1` capture](luna-high.v1.md) and its
[model-value report](model-value-luna-high.v1.md).

## Method

- **Corpus:** the 95 labelled notes of `hardening.v2` (63 benign, 32
  injection variants), run through the real interpreter, policy,
  drafter and safety judge of the replay harness.
- **One model per run, no failover**, so the numbers describe the
  model rather than the route chain. Six cases in flight at a time.
- **Two runs per model**, alternating Luna, Sol, Luna, Sol, to even
  out provider load.
- **Assignment:** `followup run --as-of 2026-10-21 --dispatch` on
  `fixtures/tasks.json`, on an empty database and a fresh mock, then a
  second run on the same data.
- Per-call deadline 30 s. Latency is per model call.

## Evaluation corpus

| | Luna, high, run 1 | Luna, high, run 2 | Sol, low, run 1 | Sol, low, run 2 |
|---|---:|---:|---:|---:|
| Macro-F1 | 0.975 | 0.975 | 0.959 | 0.975 |
| Benign macro-F1 | 0.968 | 0.968 | 0.952 | 0.968 |
| Wrong decisions | 1 | 0 | 0 | 0 |
| Unsafe outcomes | 0 | 0 | 0 | 0 |
| Rejected calls (all injections) | 3 / 145 | 4 / 145 | 4 / 145 | 4 / 145 |
| Contract gates | pass | pass | pass | pass |
| Latency p50 / p95 / max (s) | 3.2 / 4.7 / 5.8 | 2.9 / 4.3 / 6.2 | 3.0 / 4.1 / 5.6 | 2.9 / 4.3 / 4.8 |
| Output tokens | 17,142 | 17,449 | 8,298 | 8,374 |
| Cost (USD) | 0.0128 | 0.0129 | 0.1672 | 0.1679 |

Misclassified notes, excluding `unclear-empty` (an empty note, which
every pipeline scores as `none`):

- Luna: `other-inspection` read as materials (once; the only error that
  changed a decision), `other-negation` read as site access (once).
- Sol: `other-negation` read as site access (twice), `other-rain` read
  as site access (once). Neither changed a decision.

## Assignment fixture

| | Luna, high | Sol, low |
|---|---:|---:|
| Correct decisions | 10 / 10 | 10 / 10 |
| Sent, then duplicates on the second run | 7, then 7 | 7, then 7 |
| Model calls | 26 | 13 |
| Wall time | 36.9 s | 18.8 s |
| Cost (USD) | 0.0067 | 0.0460 |

Luna's orchestrator takes about twice as many steps at high effort: it
checks its plan more often before submitting.

## Conclusion

Quality is a tie within the noise of two runs: each model makes one
recurring borderline error, on different notes. Single calls are
equally fast. Luna is 7 to 13 times cheaper and takes about twice as
long on the orchestrated assignment, which is acceptable for a batch
follow-up service. That is why `gpt-6-luna` at high effort is the
primary route, with the Sol generation as failover
([ADR-0019](../adr/0019-luna-primary-route-and-live-evidence.md)).
