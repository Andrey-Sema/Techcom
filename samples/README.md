# Executed workflow samples

Produced by `make demo` (ADR-0018) with the real processor, offline
orchestrator, dispatcher, reconciler and the mock's DeliveryStore
journal at `as_of=2026-10-21`. Run/row/receipt identifiers and the
receiver's wall-clock timestamps vary between executions;
`make demo-check` compares everything else.

* `decisions.json`: all ten explainable decisions; seven have messages.
* `run_report.json`: first run, outbox states and second-run dedupe counters.
* `delivery_log.jsonl`: seven receiver commits from the same demo.

The second run proposes seven duplicate actions and sends none. Regenerate
from `var/demo/` after `make demo`; keep all three artifacts from one
execution together. Report files contain deliberate fixture
business facts, distinct from the redacted operational log stream.

Each actionable decision shows the policy `baseline`, the code-owned
`rationale` summarising the model's choices (action, recipient, asks,
tone) and the `agent_planned` flag. `api_actions.example.json` is a separately
labeled, hand-authored API shape example using synthetic facts; it is
not an execution artifact and is excluded from demo comparison.
