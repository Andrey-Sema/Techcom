# ADR-0012: LLM evaluation harness with recorded responses

* Status: Accepted
* Date: 2026-10-22

## Context

Unit tests prove that invalid LLM output is handled safely, but nothing
measured whether valid output is *correct*. Live models are not
bit-deterministic, so exact-match tests against them are flaky.

## Decision

Maintain a labelled dataset of synthetic notes (materials, upstream,
permit, site access, none, ambiguous, injection variants). Record provider
responses per prompt version and replay them in CI. Compute per-class
precision/recall, macro-F1, fallback rate and an *unsafe-outcome* count.
Gates: macro-F1 ≥ 0.85, unsafe outcomes = 0. A prompt or model change
ships only with a new eval report.

## Consequences

+ AI quality becomes evidence, not anecdote; regressions are visible.
+ Safety is measured independently from accuracy.
− Dataset curation effort; synthetic notes may not match real field
language — mitigated by feeding shadow-mode disagreements back into it.
