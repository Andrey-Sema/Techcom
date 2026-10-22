# ADR-0009: OWASP ASVS 5.0 Level 2 as the security baseline

* Status: Accepted
* Date: 2026-10-22

## Context

The service messages real people based on untrusted data and an LLM. The
brief scores "security checks" under production reliability, and the
candidate cited ASVS in the interview. A named, verifiable standard turns
"secure" into a checklist reviewers can audit.

## Decision

Target ASVS 5.0.0 Level 2 (incl. L1). Track every applicable requirement
with a status (Built / Prod / Doc / Deviation / N/A) in
`docs/15-security-asvs-l2.md`, link Built items to tests via a pytest
`asvs` marker and generate a traceability report in CI. Complement ASVS
with the OWASP LLM Top 10 and the OWASP Cheat Sheet Series. Consequences
for design: short-lived EdDSA service JWTs instead of a static API key
(13.2.1), deny-by-default scopes, two DB roles, outbound allow-list,
regex placeholder rendering, per-recipient cap, mocks excluded from the
production image, logs shipped without Docker-socket access.

## Consequences

+ Security is evidence-based and reviewable requirement by requirement.
+ Several latent issues were found and fixed at design time (see `12`).
− About 4 extra hours (M9) and more tests; local compose keeps documented
deviations (internal TLS, static per-machine DB/LLM credentials).
