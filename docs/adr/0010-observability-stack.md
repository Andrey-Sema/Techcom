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
