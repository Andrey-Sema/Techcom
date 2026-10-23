# Logging security checklist

Reference: [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html).
Checked items describe code inspected manually, not successful executions.

- [x] Use closed event names, bounded numeric fields, validated UUIDs and
  hashed actors. Do not log notes, prompts, outputs, tokens or exceptions.
- [x] Merge correlation context before the closed projection and escape
  control characters with a JSON renderer.
- [x] Export the same redacted projection over OTLP/HTTP JSON; the exporter
  applies the projection even when invoked directly.
- [x] Bound memory and batch sizes; enqueue safely across threads. Count
  overload, collector failures and partial rejection without recursive logs.
- [x] Allow only configured collector hosts, HTTPS by default, no URL
  credentials/query/fragment, no redirects, and finite export/drain time.
- [x] Keep logging outside the request-path network lifecycle and retain
  sanitized stderr when the collector is unavailable.
- [x] Restrict metrics to an internal listener and keep log credentials and
  DAST reports in owned secret/private directories.
- [x] Keep immutable business audit identity separate from expiring message
  payloads and operational log retention.
- [ ] Execute serialization, arbitrary-input, overload and shutdown tests.
- [ ] Confirm collector TLS trust, Loki retention/access and dashboard
  cardinality in the actual deployment; configure alerts for dropped logs.

Implementation: `infrastructure/log_config.py`, `infrastructure/otlp.py`,
`serving.py`. [Final integration review](../23-final-integration-review.md).
