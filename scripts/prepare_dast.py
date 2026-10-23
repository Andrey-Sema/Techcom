"""Create short-lived read-only DAST credentials and API schema."""

import argparse
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from techcom_followup.adapters.inbound.api.app import create_app
from techcom_followup.adapters.inbound.api.security import (
    TokenSettings,
    TokenVerifier,
)
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.run_lock import InMemoryRunLock
from techcom_followup.application.process_tasks import (
    ProcessingDependencies,
    ProcessTasks,
)
from techcom_followup.infrastructure.policy_files import load_decision_policy
from techcom_followup.serving import ApiClock

DIRECTORY = Path("var/dast")


class EmptySource:
    """Construct the external schema without loading any task data."""

    async def fetch_tasks(self) -> tuple[object, ...]:
        """Satisfy the processing port without fetching a provider."""
        return ()


def main() -> None:
    """Keep signing material in memory, reports in an owned folder."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-image", action="store_true")
    arguments = parser.parse_args()
    if (
        re.fullmatch(
            r"ghcr\.io/zaproxy/zaproxy(?::[A-Za-z0-9_.-]+)?@sha256:[a-f0-9]{64}",
            os.environ.get("ZAP_IMAGE", ""),
        )
        is None
    ):
        message = "ZAP_IMAGE must pin an approved official image digest"
        raise ValueError(message)
    if arguments.check_image:
        return
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    DIRECTORY.chmod(0o700)
    for source, destination in (
        (Path("tests/security/dast_app.py"), "dast_app.py"),
        (Path("tests/security/dast_probe.py"), "dast_probe.py"),
        (Path("fixtures/tasks.json"), "tasks.json"),
    ):
        copied = DIRECTORY / destination
        copied.write_bytes(source.read_bytes())
        copied.chmod(0o644)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path = DIRECTORY / "public.pem"
    path.write_bytes(public)
    path.chmod(0o644)  # Public key; mounted alone into the disposable target.
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "dast.local",
            "aud": "techcom.audit",
            "sub": "zap",
            "jti": str(uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + 900,
            "scope": "runs:read actions:read",
            "projects": ["*"],
        },
        key,
        algorithm="EdDSA",
        headers={"typ": "at+jwt", "kid": "audit"},
    )
    auth = DIRECTORY / "auth.properties"
    auth.write_text(
        "replacer.full_list(0).description=ephemeral read-only audit\n"
        "replacer.full_list(0).enabled=true\n"
        "replacer.full_list(0).matchtype=REQ_HEADER\n"
        "replacer.full_list(0).matchstr=Authorization\n"
        "replacer.full_list(0).regex=false\n"
        f"replacer.full_list(0).replacement=Bearer {token}\n",
        encoding="utf-8",
    )
    auth.chmod(0o600)
    clock = ApiClock()
    repository = InMemoryRepository()
    app = create_app(
        repository=repository,
        clock=clock,
        runner=ProcessTasks(
            ProcessingDependencies(
                repository,
                EmptySource(),
                load_decision_policy(Path("config")),
                clock,
                InMemoryRunLock(),
            )
        ),
        verifier=TokenVerifier(
            TokenSettings(
                "dast.local",
                "techcom.audit",
                {"audit": public},
                frozenset({"zap"}),
            )
        ),
    )
    schema = app.openapi()
    schema["servers"] = [{"url": "http://app:8080"}]
    (DIRECTORY / "openapi.json").write_text(
        json.dumps(schema), encoding="utf-8"
    )
    # Container UID matches the host UID: credentials stay
    # owner-readable.
    (DIRECTORY / "scan.env").write_text(
        f"DAST_UID={os.getuid()}\nDAST_GID={os.getgid()}\n",
        encoding="ascii",
    )
    (DIRECTORY / "scan.env").chmod(0o600)


if __name__ == "__main__":
    main()
