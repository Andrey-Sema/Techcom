# ADR-0010: Prometheus + Grafana + Loki + Alloy, provisioned as code

* Status: Accepted
* Date: 2026-10-22

## Context

The live review asks how to monitor cost and operate at 10k tasks/day;
ASVS V16 requires a log inventory and shipping logs to a separate system.

## Decision

Application exposes Prometheus metrics on an internal port and writes JSON
logs to stdout plus OTLP. A separate compose file runs Prometheus (with
alert rules checked by `promtool`), Loki, Grafana Alloy (OTLP receiver →
Loki; Promtail is end-of-life since 2026-03-02) and Grafana with
provisioned datasources and dashboard JSON. No container mounts the Docker
socket. Tracing via OpenTelemetry is prepared (fields reserved) but not
enabled.

## Consequences

+ One command shows the system's health, cost and security signals.
+ Everything is reviewable in Git; no click-ops.
− Four extra containers in the optional profile; the core demo does not
depend on them.

## As built (M10)

* Images pinned by tag and digest; Dependabot (`docker-compose`) proposes
  updates with a 7-day cooldown.
* Loki's OTLP endpoint maps `service.name` to the `service_name` label;
  queries use `{service_name="followup"}` and filter attributes as
  structured metadata (no `| json` needed).
* Alloy drops `notes`, `prompt`, `raw_output`, `input_text`, `api_key`
  and `authorization` attributes before storage: defence in depth behind
  the Semgrep rule `no-untrusted-text-in-logs`.
* Grafana secrets must be readable by uid 472. An unreadable secret file
  makes Grafana start with `admin/admin`; the smoke test asserts that the
  default credentials are refused (found while building this milestone).
* Evidence: `make obs-check` (promtool `check config` and `test rules`,
  `loki -verify-config`, `alloy fmt --test` and `validate`),
  `make obs-smoke` (live stack, OTLP record end to end, redaction,
  provisioning, all dashboard queries parsed by Prometheus) and
  `tests/meta/test_observability_config.py` (metric names vs the
  catalogue in `docs/14`, runbook anchors, promtool coverage, budget
  threshold vs `config/llm.toml`, compose hardening).
