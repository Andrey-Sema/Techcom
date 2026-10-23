import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx2
import openai
import pytest
from openai import DefaultAsyncHttpxClient
from pydantic import HttpUrl, SecretStr, ValidationError

import llm_doubles as sdk
from llm_doubles import FakeClock
from techcom_followup.adapters.outbound.llm import factory
from techcom_followup.adapters.outbound.llm.config import RoutingConfig
from techcom_followup.application.ports import LLMRequest
from techcom_followup.infrastructure.settings import (
    OpenAIAuthMode,
    OpenAISettings,
    load_openai_settings,
)
from techcom_followup.infrastructure.telemetry import PrometheusTelemetry

if TYPE_CHECKING:
    from openai import AsyncOpenAI

FAKE_KEY = "sk-test-not-a-real-key-0000"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FOLLOWUP_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asvs("13.3.1")
def test_key_is_read_from_secret_file_and_never_shown(tmp_path: Path) -> None:
    (tmp_path / "followup_openai_api_key").write_text(FAKE_KEY)
    settings = load_openai_settings(tmp_path)
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(settings)


def test_missing_secrets_dir_falls_back_to_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOLLOWUP_OPENAI_API_KEY", FAKE_KEY)
    settings = load_openai_settings(tmp_path / "absent")
    assert settings.auth_mode is OpenAIAuthMode.API_KEY


def test_api_key_mode_requires_a_key() -> None:
    with pytest.raises(ValidationError, match="api_key is required"):
        OpenAISettings()


def test_malformed_key_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as caught:
        OpenAISettings(api_key=SecretStr("not-a-key-123"))
    assert "not-a-key-123" not in str(caught.value)


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("http://api.openai.com/v1", "https"),
        ("https://evil.example/v1", "allowed_hosts"),
    ],
)
@pytest.mark.asvs("13.2.4")
def test_outbound_allow_list(base_url: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        OpenAISettings(api_key=SecretStr(FAKE_KEY), base_url=HttpUrl(base_url))


def test_extra_host_can_be_allowed_explicitly() -> None:
    settings = OpenAISettings(
        api_key=SecretStr(FAKE_KEY),
        base_url=HttpUrl("https://llm-gateway.internal.example/v1"),
        allowed_hosts=frozenset({"llm-gateway.internal.example"}),
    )
    assert settings.base_url.host == "llm-gateway.internal.example"


def test_workload_identity_needs_ids() -> None:
    with pytest.raises(ValidationError, match="identity_provider_id"):
        OpenAISettings(auth_mode=OpenAIAuthMode.WORKLOAD_IDENTITY)


def test_workload_identity_forbids_a_static_key() -> None:
    with pytest.raises(ValidationError, match="must not be set"):
        OpenAISettings(
            auth_mode=OpenAIAuthMode.WORKLOAD_IDENTITY,
            api_key=SecretStr(FAKE_KEY),
            identity_provider_id="idp_1",
            service_account_id="sa_1",
        )


def test_api_key_client_has_no_sdk_retries() -> None:
    client = factory.build_openai_client(
        OpenAISettings(api_key=SecretStr(FAKE_KEY))
    )
    assert client._client.follow_redirects is False
    assert client.max_retries == 0
    assert client.api_key == FAKE_KEY
    assert str(client.base_url).startswith("https://api.openai.com/v1")


@pytest.mark.asvs("13.2.1")
def test_workload_identity_client_has_no_static_key(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("header.payload.signature")
    settings = OpenAISettings(
        auth_mode=OpenAIAuthMode.WORKLOAD_IDENTITY,
        identity_provider_id="idp_1",
        service_account_id="sa_1",
        subject_token_file=token_file,
    )
    client = factory.build_openai_client(settings)
    assert client._client.follow_redirects is False
    assert client.max_retries == 0
    assert client.workload_identity is not None
    assert client.workload_identity["service_account_id"] == "sa_1"


def test_utc_today_is_the_utc_calendar_date() -> None:
    assert factory._utc_today() == datetime.now(UTC).date()


class _Responses:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs: Any) -> object:
        self.calls += 1
        return sdk.response(output=[sdk.text_message('{"blocker": "x"}')])


class _Client:
    def __init__(self) -> None:
        self.responses = _Responses()


async def test_build_router_wires_the_responses_api(
    routing_config: RoutingConfig,
    request_: LLMRequest,
    clock: FakeClock,
) -> None:
    stub = _Client()
    router = factory.build_router(
        routing_config,
        cast("AsyncOpenAI", stub),
        monotonic=clock.monotonic,
    )
    result = await router.generate_json(request_)
    assert result.text == '{"blocker": "x"}'
    assert stub.responses.calls == 1


def test_build_router_exports_model_circuit_states(
    routing_config: RoutingConfig,
) -> None:
    telemetry = PrometheusTelemetry()
    factory.build_router(
        routing_config, cast("AsyncOpenAI", _Client()), telemetry=telemetry
    )
    for model in ("gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"):
        assert (
            telemetry.registry.get_sample_value(
                "followup_circuit_state",
                {"dependency": f"llm:{model}", "state": "closed"},
            )
            == 1
        )


@pytest.mark.parametrize("redirect_status", [307, 308])
async def test_factory_client_never_follows_redirect_to_unapproved_host(
    monkeypatch: pytest.MonkeyPatch, redirect_status: int
) -> None:
    calls: list[httpx2.Request] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            redirect_status,
            headers={"location": "https://unapproved.example/v1/responses"},
            request=request,
        )

    real_client = DefaultAsyncHttpxClient

    def transport_client(**kwargs: Any) -> DefaultAsyncHttpxClient:
        return real_client(transport=httpx2.MockTransport(respond), **kwargs)

    monkeypatch.setattr(factory, "DefaultAsyncHttpxClient", transport_client)
    client = factory.build_openai_client(
        OpenAISettings(api_key=SecretStr(FAKE_KEY))
    )
    assert client._client.follow_redirects is False
    # Avoid SDK user-agent discovery using a sandboxed thread executor.
    client._platform = "Linux"
    try:
        with pytest.raises(openai.APIStatusError):
            async with asyncio.timeout(5):
                await client.responses.create(
                    model="gpt-6.1-sol", input="offline redirect regression"
                )
    finally:
        await client.close()
    assert len(calls) == 1
    assert calls[0].url.host == "api.openai.com"
    assert calls[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
