# FastAPI security checklist

Reference: [OWASP REST Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html).
These are manual code-review assertions; this patch has not been executed.

- [x] Trust only configured Ed25519 keys; pin algorithm/type, issuer,
  audience and strict required claims; reject token-controlled key URLs.
- [x] Declare capabilities per route, deny undeclared routes, and admit
  authorization/rate limits before body parsing or processing.
- [x] Use signed membership and persisted source project for object access.
  UUIDs and caller-supplied project filters do not grant authority.
- [x] Bound token/body/query/row sizes and body deadline. Limit API listener
  concurrency and use finite database/lock deadlines.
- [x] Coordinate request limits across replicas in PostgreSQL and fail
  closed on admission-store errors. Separate failed-auth accounting.
- [x] Keep health paths public and metrics on a separate listener; disable
  production docs/OpenAPI and server access logs containing request data.
- [x] Return closed problem details/security headers without dependency or
  validation input text. Propagate cancellation while joining work.
- [x] Keep the DAST target synthetic and isolated, with ephemeral read-only
  credentials and no external delivery/provider endpoints.
- [ ] Execute regression/DAST gates on the immutable PR revision.
- [ ] Verify gateway TLS, trusted source addresses, network isolation and
  deployment-wide configuration consistency in the actual deployment.

Implementation: `adapters/inbound/api/`, `adapters/outbound/admission.py`,
`serving.py`, migration `0004`. Configuration: [API and deployment](../19-api-and-deployment.md).
