from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
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
