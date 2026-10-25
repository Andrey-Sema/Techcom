# ADR-0019: gpt-6-luna as the primary route, with live evidence

* Status: Accepted (amends the route list of ADR-0013 and the evidence
  of ADR-0012)
* Date: 2026-10-25

## Context

ADR-0013 routed every purpose through the Sol generation. The only
evaluation evidence was the synthetic `hardening.v2` replay, which
proves the workflow contracts but says nothing about a real model.
Two flaws surfaced only once the corpus ran against a live model:

* The prompt asks for `unclear` with low confidence when the model is
  unsure, but validation rejected every answer below confidence 0.6,
  so correct `unclear` answers counted as failed calls.
* Interpretation and drafting had a 10 s deadline, below the latency of
  a high-effort reasoning call (up to 18.8 s observed).

## Decision

* `gpt-6-luna` at `high` reasoning effort is the first route for every
  purpose. `gpt-6.1-sol`, `gpt-6-sol` and `gpt-5.6-sol` stay as
  failover. A configuration test pins Luna first at `high`.
* Every model call has a 30 s deadline; the 60 s plan deadline is
  unchanged.
* A low-confidence `unclear` interpretation is an accepted answer. It
  routes exactly like the fallback; every other blocker still needs
  confidence 0.6 and a verbatim quote.
* Live Luna responses for the whole corpus are committed as
  `tests/eval/bundles/luna-high.v1` and replayed in CI with
  `--require-provider` (`make eval-provider`). `scripts/capture_eval.py`
  captures them again when the prompts or the workflow change.
* `python -m eval --value-report` compares the corpus with and without
  the model, so the model's contribution is measured, not asserted.

## Evidence

* With the model: macro-F1 0.99, no wrong decisions, no unsafe
  outcomes. Without it: macro-F1 0.34 and 35 wrong decisions. The model
  fixes 35 decisions and breaks none
  ([report](../eval/model-value-luna-high.v1.md)).
* Against `gpt-6.1-sol` at low effort, quality is a tie, single calls
  are equally fast, Luna is 7 to 13 times cheaper and about twice as
  slow on the orchestrated run
  ([comparison](../eval/model-comparison.md)).

## Consequences

* CI now fails when a prompt or workflow change invalidates the live
  captures; they must be captured again, which needs an API key and
  costs about two cents.
* The captures prove consistency with their requests, not origin;
  their quality label stays `CAPTURE_REPLAY_ONLY`.
* The corpus is built from notes the keyword path is not meant to
  settle, so the measured gap is larger than on routine notes.
