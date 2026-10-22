# Executed CLI samples

Generated with `make demo`, `as_of=2026-10-21`, the committed task fixture,
default stub and a fresh local receiver journal. These are actual output
artifacts, not hand-written expected responses. Run/row/receipt identifiers
and the receiver's wall-clock timestamps vary between executions.

* `decisions.json`: all ten explainable decisions; seven have messages.
* `run_report.json`: first run, outbox states and second-run dedupe counters.
* `delivery_log.jsonl`: seven receiver commits from the same demo.

The second run proposes seven duplicate actions and sends none. Regenerate
from `var/demo/` after `make demo`; keep all three artifacts from one
execution together. The model is an offline stub: these samples do not
measure real-provider accuracy. Report files contain deliberate fixture
business facts, distinct from the redacted operational log stream.
