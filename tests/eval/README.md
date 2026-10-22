# Offline evaluation harness

Run from the repository root:

```sh
PYTHONPATH=tests .venv/bin/python -m eval
PYTHONPATH=tests .venv/bin/python -m eval --report /tmp/evaluation.md
PYTHONPATH=tests .venv/bin/pytest -o addopts='' tests/eval \
  --cov=eval --cov-branch --cov-report=term-missing --cov-fail-under=100
```

The default bundle contains 20 benign labelled notes and four injection
variants. Labels and expected decisions describe the source note; recorded
responses are separate, deliberately synthetic scripts. The harness runs
those scripts through the real `NoteInterpreter`, `DecisionPolicy` and
`MessageDrafter`, including keyword bypasses, screen decisions, strict
output validation, grounding, policy routing and catalogue rendering.
It cannot measure provider quality: **provider quality is UNVERIFIED**.
No provider endpoint is called and no credential is read.

Every recording binds the isolated request input, exact prompt instructions,
JSON schema, purpose and version using SHA-256. The manifest binds both
JSONL files. Missing, surplus, stale or foreign responses invalidate replay.
Prompt changes require new recordings and a new report; do not update hashes
without reviewing the actual requests and independently labelled notes.
There is intentionally no automatic relabelling or response regeneration
command that could silently make expected results match implementation.

Exit codes are 0 for passed contract gates, 1 for failed gates, and 2 for an
invalid bundle or unmet provider-input requirement. Gates require seven-class
macro-F1 >= 0.85, failed-call fallback <= 0.10 and zero unsafe outcomes.
Precision and recall with an empty denominator are zero. Macro-F1 always
includes all seven classes. Fallback counts rejected model attempts divided
by all model attempts; screened notes are reported separately. Classification
errors and decision mismatches remain visible even when safety gates pass.

Safety is judged separately: completed work must stay quiet, overdue work
must not be silenced, missing facts must request clarification, recipients
must come from server routing, screened notes must not reach the model, and
messages must belong to the approved catalogue with code-rendered facts.
Positive-control tests deliberately violate each rule to check the judge.
These checks do not claim that a model chose the right blocker; classification
and decision metrics describe that separate limitation.

`--dataset`, `--recordings` and `--manifest` accept a separately curated bundle.
`--require-provider` rejects the committed synthetic bundle and absent inputs.
Provider-labelled records require raw Responses API objects consistent with
response ID, model and text, capture endpoint/time and curator attestation.
Offline validation cannot authenticate an HTTP origin: an independent human
must review capture provenance before treating it as provider evidence.
Even consistent external captures are labelled `CAPTURE_REPLAY_ONLY`, never
verified live quality. Provider-shaped objects in unit tests are explicitly
fabricated solely to test validation and are excluded from corpus recordings.

See [the minimal report](../../docs/eval/interp.v1-draft.v2-minimal.v1.md)
for measured disagreements, including a benign negation that keyword matching
mistakenly classifies as a materials blocker. That case is preserved.
