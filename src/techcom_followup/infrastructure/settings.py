"""Typed settings read from the environment and from secret files.

Secrets are never read from plain environment variables in production:
Docker/Kubernetes mount them as files and ``pydantic-settings`` reads
them from the secrets directory (``/run/secrets`` by default).
"""

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRETS_DIR = Path("/run/secrets")
_OPENAI_KEY_PREFIX = "sk-"


class OpenAIAuthMode(StrEnum):
    """How the service authenticates to OpenAI.

    ``api_key`` is for local development and the assessment demo.
    ``workload_identity`` exchanges a short-lived platform token (for
    example a Kubernetes service-account token) for an OpenAI token, so
    no static key exists at all (ASVS 13.2.1).
    """

    API_KEY = "api_key"
    WORKLOAD_IDENTITY = "workload_identity"


class OpenAISettings(BaseSettings):
    """OpenAI connection settings (prefix ``FOLLOWUP_OPENAI_``).

    The API key is read from the secret file
    ``followup_openai_api_key`` (or the variable
    ``FOLLOWUP_OPENAI_API_KEY`` in tests/CI). It is kept as
    ``SecretStr`` so it never appears in ``repr``, logs or tracebacks.
    """

    model_config = SettingsConfigDict(
        env_prefix="FOLLOWUP_OPENAI_",
        extra="forbid",
        frozen=True,
        # Validation errors must never echo raw input: it would leak the
        # API key into logs and tracebacks (ASVS 16.2.5).
        hide_input_in_errors=True,
    )

    auth_mode: OpenAIAuthMode = OpenAIAuthMode.API_KEY
    api_key: SecretStr | None = None
    project: str | None = None
    organization: str | None = None
    base_url: HttpUrl = HttpUrl("https://api.openai.com/v1")
    allowed_hosts: frozenset[str] = frozenset({"api.openai.com"})
    identity_provider_id: str | None = None
    service_account_id: str | None = None
    subject_token_file: Path = Path(
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        _check_endpoint(self.base_url, self.allowed_hosts)
        if self.auth_mode is OpenAIAuthMode.API_KEY:
            _check_api_key(self.api_key)
        else:
            _check_workload_identity(self)
        return self


def _check_endpoint(base_url: HttpUrl, allowed_hosts: frozenset[str]) -> None:
    # Outbound allow-list (ASVS 13.2.4): the SDK would otherwise honour
    # an OPENAI_BASE_URL variable injected into the environment.
    if base_url.scheme != "https":
        msg = "base_url must use https"
        raise ValueError(msg)
    if base_url.host not in allowed_hosts:
        msg = "base_url host is not in allowed_hosts"
        raise ValueError(msg)


def _check_api_key(api_key: SecretStr | None) -> None:
    if api_key is None:
        msg = "api_key is required when auth_mode is api_key"
        raise ValueError(msg)
    if not api_key.get_secret_value().startswith(_OPENAI_KEY_PREFIX):
        # Never echo the value: only say that the format is wrong.
        msg = "api_key does not look like an OpenAI key"
        raise ValueError(msg)


def _check_workload_identity(settings: OpenAISettings) -> None:
    if settings.api_key is not None:
        msg = "api_key must not be set with workload_identity"
        raise ValueError(msg)
    if not settings.identity_provider_id or not settings.service_account_id:
        msg = "identity_provider_id and service_account_id are required"
        raise ValueError(msg)


def load_openai_settings(
    secrets_dir: Path = DEFAULT_SECRETS_DIR,
) -> OpenAISettings:
    """Build settings; read secret files only if the directory exists.

    Raises:
        pydantic.ValidationError: If the settings are inconsistent.
    """
    if secrets_dir.is_dir():
        return OpenAISettings(_secrets_dir=secrets_dir)
    return OpenAISettings()
