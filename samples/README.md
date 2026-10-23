# Executed workflow samples

Refreshed for ADR-0017 with the real processor, offline orchestrator,
dispatcher and DeliveryStore journal at `as_of=2026-10-21`. The same
fixture and workflow as the CLI demo were used, with an in-process source
and direct mock-store gateway because HTTP runtime dependencies were
unavailable. These are executed workflow artifacts; the HTTP CLI demo
comparison still needs its complete environment. Run/row/receipt identifiers
and the receiver's wall-clock timestamps vary between executions.

* `decisions.json`: all ten explainable decisions; seven have messages.
* `run_report.json`: first run, outbox states and second-run dedupe counters.
* `delivery_log.jsonl`: seven receiver commits from the same demo.

The second run proposes seven duplicate actions and sends none. Regenerate
from `var/demo/` after `make demo`; keep all three artifacts from one
execution together. The model is an offline stub: these samples do not
measure real-provider accuracy. Report files contain deliberate fixture
business facts, distinct from the redacted operational log stream.

Each actionable decision shows the policy `baseline`, the orchestrator's
`rationale` and the `agent_planned` flag. `api_actions.example.json` is a separately
labeled, hand-authored API shape example using synthetic facts; it is
not an execution artifact and is excluded from demo comparison.
