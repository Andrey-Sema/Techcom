"""Secure listener and collector defaults without opening sockets."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from techcom_followup.serving import ApiRuntimeSettings, token_verifier


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://alloy:4318/v1/logs",
        "https://unapproved.test/v1/logs",
        "https://secret@alloy/v1/logs",
        "https://alloy/v1/logs?token=SECRET",
        "https://alloy/v1/logs#fragment",
        "https://alloy/unapproved",
        "https://alloy:invalid/v1/logs",
    ],
)
def test_unapproved_collector_destinations_are_rejected(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        ApiRuntimeSettings(
            issuer="issuer",
            audience="audience",
            public_keys_file=Path("unused.json"),
            otlp_endpoint=endpoint,
        )


def test_plain_http_requires_explicit_internal_opt_in() -> None:
    settings = ApiRuntimeSettings(
        issuer="issuer",
        audience="audience",
        public_keys_file=Path("unused.json"),
        otlp_endpoint="http://alloy:4318/v1/logs",
        allow_internal_http=True,
    )
    assert settings.port != settings.metrics_port
    assert settings.host == settings.metrics_host == "127.0.0.1"
    assert not settings.all_project_subjects


def test_key_file_is_bounded_and_must_contain_a_map(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    settings = ApiRuntimeSettings(
        issuer="issuer",
        audience="audience",
        public_keys_file=path,
    )
    for data in (b"[]", b"{}", b"x" * 65_537):
        path.write_bytes(data)
        with pytest.raises(ValueError, match="public-key"):
            token_verifier(settings)
