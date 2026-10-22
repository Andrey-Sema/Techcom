from dataclasses import replace
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from pydantic import JsonValue

from api_support import NOW, Harness
from techcom_followup.adapters.inbound.api.security import (
    AuthenticationError,
    TokenSettings,
    TokenVerifier,
)


@pytest.mark.asvs("9.1.1", "9.2.1", "9.2.2", "9.2.3")
def test_verified_claims_produce_capabilities() -> None:
    harness = Harness()
    principal = harness.verifier.verify(harness.token(), NOW)
    assert principal.all_projects
    assert principal.can_access(None)
    assert principal.scopes == {"runs:trigger", "runs:read", "actions:read"}
    assert "global" not in principal.actor_id
    scoped = harness.verifier.verify(
        harness.token(sub="reader", projects=["A"]), NOW
    )
    assert scoped.can_access("A")
    assert not scoped.can_access("B")
    assert not scoped.can_access(None)
    assert not scoped.all_projects


@pytest.mark.asvs("9.1.1", "9.1.2", "9.1.3")
@pytest.mark.parametrize(
    "headers",
    [
        {"typ": "JWT"},
        {"kid": "unknown"},
        {"jku": "https://keys.attacker.invalid/jwks"},
        {"x5u": "https://keys.attacker.invalid/cert"},
        {"jwk": {"kty": "oct", "k": "AAAA"}},
        {"crit": ["anything"]},
    ],
)
def test_header_extensions_never_select_remote_keys(
    headers: dict[str, JsonValue],
) -> None:
    harness = Harness()
    with pytest.raises(AuthenticationError):
        harness.verifier.verify(harness.sign(harness.claims(), **headers), NOW)


@pytest.mark.asvs("9.1.2")
@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_algorithm_allowlist(algorithm: str) -> None:
    harness = Harness()
    token = jwt.encode(
        harness.claims(),
        ""
        if algorithm == "none"
        else "temporary-test-key-longer-than-32-bytes",
        algorithm=algorithm,
        headers={"typ": "at+jwt", "kid": "active"},
    )
    with pytest.raises(AuthenticationError):
        harness.verifier.verify(token, NOW)


@pytest.mark.asvs("9.1.1")
def test_wrong_signature_key() -> None:
    harness = Harness()
    token = jwt.encode(
        harness.claims(),
        ed25519.Ed25519PrivateKey.generate(),
        algorithm="EdDSA",
        headers={"typ": "at+jwt", "kid": "active"},
    )
    with pytest.raises(AuthenticationError):
        harness.verifier.verify(token, NOW)


@pytest.mark.asvs("9.2.1", "9.2.3", "10.3.2")
@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example.test"},
        {"aud": "other-service"},
        {"aud": ["followup-api", "other"]},
        {"exp": int(NOW.timestamp()) - 31},
        {"nbf": int(NOW.timestamp()) + 31},
        {"iat": int(NOW.timestamp()) + 31},
        {"exp": int(NOW.timestamp()) + 901},
        {"iat": -1},
        {"nbf": int(NOW.timestamp()) - 1},
        {"exp": int(NOW.timestamp())},
        {"exp": True},
        {"iat": 123.5},
        {"jti": ""},
        {"sub": "\nforged"},
        {"scope": "runs:delete"},
        {"projects": ["A", "A"]},
        {"projects": []},
        {"projects": ["A\0"]},
        {"projects": ["\ud800"]},
        {"projects": ["*", "A"]},
        {"projects": ["*"], "sub": "scoped-service"},
        {"owner": "injected"},
    ],
)
def test_invalid_claims_are_rejected(changes: dict[str, JsonValue]) -> None:
    harness = Harness()
    with pytest.raises(AuthenticationError):
        harness.verifier.verify(harness.token(**changes), NOW)


@pytest.mark.parametrize(
    "claim",
    ["iss", "aud", "sub", "jti", "exp", "iat", "nbf", "scope", "projects"],
)
def test_required_claims(claim: str) -> None:
    harness = Harness()
    claims = harness.claims()
    del claims[claim]
    with pytest.raises(AuthenticationError):
        harness.verifier.verify(harness.sign(claims), NOW)


def test_expiry_leeway_boundary() -> None:
    harness = Harness()
    token = harness.token()
    assert harness.verifier.verify(
        token, NOW + timedelta(seconds=329)
    ).all_projects
    with pytest.raises(AuthenticationError):
        harness.verifier.verify(token, NOW + timedelta(seconds=330))


@pytest.mark.parametrize(
    "token", ["", "invalid", "x" * 8193, "é", "e30.e30.invalid"]
)
def test_malformed_token_is_rejected(token: str) -> None:
    with pytest.raises(AuthenticationError):
        Harness().verifier.verify(token, NOW)


def test_rotated_key_and_invalid_settings() -> None:
    harness = Harness()
    pem = harness.key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    settings = TokenSettings(
        "https://issuer.example.test",
        "followup-api",
        {"active": pem, "previous": pem},
        frozenset({"global"}),
    )
    rotated = TokenVerifier(settings)
    assert rotated.verify(
        harness.sign(harness.claims(), kid="previous"), NOW
    ).all_projects
    for invalid in (
        replace(settings, issuer=""),
        replace(settings, audience=""),
        replace(settings, public_keys={}),
        replace(settings, leeway_s=31),
    ):
        with pytest.raises(ValueError, match="configured"):
            TokenVerifier(invalid)
    other = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    with pytest.raises(TypeError, match="Ed25519"):
        TokenVerifier(replace(settings, public_keys={"rsa": other}))
