"""Builds the OpenAI client and the router from validated settings."""

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import partial

from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from openai.auth import k8s_service_account_token_provider

from techcom_followup.adapters.outbound.llm.config import RoutingConfig
from techcom_followup.adapters.outbound.llm.openai_responses import (
    OpenAIResponsesBackend,
)
from techcom_followup.adapters.outbound.llm.router import LLMRouter
from techcom_followup.application.telemetry import NULL_TELEMETRY
from techcom_followup.infrastructure.resilience import (
    CircuitBreaker,
    SpendLedger,
)
from techcom_followup.infrastructure.settings import (
    OpenAIAuthMode,
    OpenAISettings,
)
from techcom_followup.infrastructure.telemetry import PrometheusTelemetry

# Default per-client timeout; every request passes its own, shorter one.
_CLIENT_TIMEOUT_S = 60.0


def build_openai_client(settings: OpenAISettings) -> AsyncOpenAI:
    """Create an ``AsyncOpenAI`` client with SDK retries disabled.

    Retries happen in exactly one layer (the router's failover); the
    SDK's own retries would multiply attempts and hide failures.
    """
    base_url = str(settings.base_url)
    if settings.auth_mode is OpenAIAuthMode.WORKLOAD_IDENTITY:
        return AsyncOpenAI(
            workload_identity={
                "identity_provider_id": str(settings.identity_provider_id),
                "service_account_id": str(settings.service_account_id),
                "provider": k8s_service_account_token_provider(
                    settings.subject_token_file,
                ),
            },
            project=settings.project,
            organization=settings.organization,
            base_url=base_url,
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(follow_redirects=False),
            timeout=_CLIENT_TIMEOUT_S,
        )
    # The settings validator guarantees the key exists in this mode.
    api_key = settings.api_key.get_secret_value() if settings.api_key else ""
    return AsyncOpenAI(
        api_key=api_key,
        project=settings.project,
        organization=settings.organization,
        base_url=base_url,
        max_retries=0,
        http_client=DefaultAsyncHttpxClient(follow_redirects=False),
        timeout=_CLIENT_TIMEOUT_S,
    )


def _utc_today() -> date:
    return datetime.now(UTC).date()


def build_router(
    config: RoutingConfig,
    client: AsyncOpenAI,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    today: Callable[[], date] = _utc_today,
    telemetry: PrometheusTelemetry | None = None,
    ledger: SpendLedger | None = None,
) -> LLMRouter:
    """Compose the router with the OpenAI Responses backend."""
    backend = OpenAIResponsesBackend(client.responses.create)
    router = LLMRouter(
        config=config,
        backends={"openai": backend},
        monotonic=monotonic,
        today=today,
        telemetry=NULL_TELEMETRY if telemetry is None else telemetry,
        ledger=ledger,
    )
    if telemetry is not None:
        for model, breaker in router.breakers.items():
            telemetry.watch_circuit(f"llm:{model}", partial(_state, breaker))
    return router


def _state(breaker: CircuitBreaker) -> str:
    return breaker.state.value
