"""Verify service JWTs using only operator-configured Ed25519 keys."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from techcom_followup.application.repository import require_aware

MAX_TOKEN_BYTES = 8192
MAX_TOKEN_LIFETIME_S = 900
MAX_LEEWAY_S = 30
_IDENTIFIER = r"^[A-Za-z0-9_.:@-]+$"
SCOPES = frozenset({"runs:trigger", "runs:read", "actions:read"})


class AuthenticationError(Exception):
    """Authentication failed; the HTTP boundary never echoes details."""


@dataclass(frozen=True, slots=True)
class TokenSettings:
    """Issuer keys and explicitly privileged service subjects."""

    issuer: str
    audience: str
    public_keys: Mapping[str, bytes]
    all_project_subjects: frozenset[str] = frozenset()
    leeway_s: int = 30


class _Claims(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", hide_input_in_errors=True
    )

    iss: str
    aud: str
    sub: str = Field(min_length=1, max_length=160, pattern=_IDENTIFIER)
    jti: str = Field(min_length=16, max_length=128, pattern=_IDENTIFIER)
    exp: int
    iat: int
    nbf: int
    scope: str = Field(max_length=200)
    projects: list[Annotated[str, Field(min_length=1, max_length=200)]] = (
        Field(min_length=1, max_length=128)
    )


@dataclass(frozen=True, slots=True)
class Principal:
    """Verified service capabilities and exact project memberships."""

    actor_id: str
    scopes: frozenset[str]
    projects: frozenset[str]
    all_projects: bool

    def can_access(self, project: str | None) -> bool:
        """Deny missing project relationships to scoped callers."""
        return self.all_projects or (
            project is not None and project in self.projects
        )


class TokenVerifier:
    """Verify signatures, strict claims and injected time."""

    def __init__(self, settings: TokenSettings) -> None:
        """Load configured Ed25519 keys without fetching URLs."""
        if (
            not settings.issuer
            or not settings.audience
            or not settings.public_keys
            or not 0 <= settings.leeway_s <= MAX_LEEWAY_S
        ):
            message = "issuer, audience, keys and leeway must be configured"
            raise ValueError(message)
        self._settings = settings
        self._keys = {
            key_id: self._public_key(pem)
            for key_id, pem in settings.public_keys.items()
        }

    @staticmethod
    def _public_key(pem: bytes) -> Ed25519PublicKey:
        key = serialization.load_pem_public_key(pem)
        if not isinstance(key, Ed25519PublicKey):
            message = "JWT verification keys must use Ed25519"
            raise TypeError(message)
        return key

    def verify(self, token: str, now: datetime) -> Principal:
        """Reject malformed, expired or overprivileged signed tokens."""
        require_aware(now)
        if len(token) > MAX_TOKEN_BYTES or not token.isascii():
            raise AuthenticationError
        try:
            header = jwt.get_unverified_header(token)
            self._check_header(header)
            claims = self._decode(token, header["kid"])
            self._check_time(claims, now)
            return self._principal(claims)
        except (jwt.PyJWTError, ValidationError, ValueError, KeyError) as exc:
            raise AuthenticationError from exc

    def _check_header(self, header: Mapping[str, object]) -> None:
        if (
            set(header) != {"alg", "typ", "kid"}
            or header.get("alg") != "EdDSA"
            or header.get("typ") != "at+jwt"
            or not isinstance(header.get("kid"), str)
            or header["kid"] not in self._keys
        ):
            raise AuthenticationError

    def _decode(self, token: str, key_id: str) -> _Claims:
        # PyJWT verifies signature, issuer and audience. Time is checked
        # below using the same injected clock as request processing.
        raw = jwt.decode(
            token,
            self._keys[key_id],
            algorithms=["EdDSA"],
            audience=self._settings.audience,
            issuer=self._settings.issuer,
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
        return _Claims.model_validate(raw)

    def _check_time(self, claims: _Claims, now: datetime) -> None:
        current, leeway = now.timestamp(), self._settings.leeway_s
        if (
            not 0 <= claims.iat <= claims.nbf < claims.exp
            or claims.exp - claims.iat > MAX_TOKEN_LIFETIME_S
            or claims.iat > current + leeway
            or claims.nbf > current + leeway
            or claims.exp <= current - leeway
        ):
            raise AuthenticationError

    def _principal(self, claims: _Claims) -> Principal:
        scopes = frozenset(claims.scope.split())
        projects = frozenset(claims.projects)
        wildcard = "*" in projects
        if (
            not scopes <= SCOPES
            or len(projects) != len(claims.projects)
            or (
                wildcard
                and (
                    projects != {"*"}
                    or claims.sub not in self._settings.all_project_subjects
                )
            )
        ):
            raise AuthenticationError
        actor = hashlib.sha256(
            (claims.iss + "\0" + claims.sub).encode()
        ).hexdigest()[:24]
        return Principal(actor, scopes, projects, wildcard)
