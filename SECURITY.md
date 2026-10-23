# Security policy

## Supported versions

Only the latest commit on `main` is supported. There are no released
versions yet; the first tag will be `v1.0.0`.

## Reporting a vulnerability

Report privately through
[GitHub security advisories](https://github.com/Andrey-Sema/Techcom/security/advisories/new).
Do not open a public issue, and do not include real secrets, task notes
or customer data in the report.

| Severity (CVSS 4.0) | Acknowledge | Fix or mitigation |
|---------------------|-------------|-------------------|
| Critical | 2 days | 7 days |
| High | 5 days | 30 days |
| Medium / Low | 10 days | next planned release |

## Scope and baseline

The design target is OWASP ASVS 5.0 level 2 (`docs/15-security-asvs-l2.md`).
Known gaps and accepted deviations are listed there. Dependency vulnerabilities are checked
with `make audit` (pip-audit on the locked runtime set).
