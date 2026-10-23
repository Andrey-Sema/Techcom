# OWASP Docker Security Cheat Sheet — checklist

Scope: the `runtime` and `mock` images (`Dockerfile`), the observability
stack (`compose.observability.yaml`), the API service (`compose.api.yaml`)
and CI. Status words follow [`15`](../15-security-asvs-l2.md) §1.

| Rule | Control here | Evidence | Status |
|------|--------------|----------|--------|
| #0 Keep host and Docker up to date | Platform responsibility; CI runners are GitHub-hosted | — | Prod |
| #1 Do not expose the Docker daemon socket | No container mounts `docker.sock`; Loki/Alloy get logs over OTLP, not via the socket (D12) | `tests/meta/test_observability_config.py::TestComposeHardening::test_no_docker_socket_and_read_only_mounts` | Built |
| #2 Set a user | `runtime` runs as UID 10001, `mock` as 10002; code owned by root | `tests/image/test_images.py::test_runtime_runs_as_the_unprivileged_service_user`, `test_service_user_cannot_modify_its_own_code` | Built |
| #3 Limit capabilities | `cap_drop: [ALL]` in compose; image tests run with `--cap-drop=ALL` | `compose.observability.yaml`, `compose.api.yaml`, `tests/image/` | Built |
| #4 Prevent in-container privilege escalation | `no-new-privileges:true` everywhere | same as #3 | Built |
| #5 Inter-container connectivity | Observability stack on its own network, host ports bound to 127.0.0.1 | `compose.observability.yaml` | Built; an `internal: true` backend network for the app: Planned |
| #6 Linux security modules | Docker's default seccomp/AppArmor profiles are kept; nothing sets `seccomp=unconfined` or `privileged` | compose review | Built |
| #7 Limit resources | Memory/CPU/PID limits per service | — | Planned (compose sets no memory/CPU/PID limits yet) |
| #8 Read-only filesystem and volumes | Images work with `--read-only`; writable state only on named volumes (`/var/lib/mock`) or tmpfs | `tests/image/test_images.py` (every container runs `--read-only`) | Built |
| #9 Container scanning in CI | hadolint (`make gates`, pre-commit), Trivy config + image scan, scanners pinned by digest | `tests/meta/test_security_gates.py::test_dependency_and_image_scans_fail_the_build`, `test_scanners_are_pinned_by_digest` | Built |
| #10 Docker daemon logging level `info` | Platform default | — | Prod |
| #11 Rootless mode | Platform choice | — | Prod |
| #12 Docker secrets for sensitive data | Database and Grafana credentials via compose `secrets:`; the service reads `/run/secrets` (`infrastructure/settings.py`) | `tests/unit/llm/test_settings_and_factory.py::test_key_is_read_from_secret_file_and_never_shown`; `make obs-smoke` asserts `admin/admin` is refused | Built (the API reads its database URL and keys from compose secrets) |
| #13 Supply chain | Base, uv and scanner images pinned by digest; Dependabot (docker, docker-compose, uv, actions) with a 7-day cooldown; CycloneDX SBOMs of the lock and of the runtime image | `tests/meta/test_security_gates.py::test_sboms_are_built_and_kept_as_artefacts` | Built (signed attestation: Planned, needs a registry) |
