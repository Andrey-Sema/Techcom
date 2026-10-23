from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from techcom_followup.adapters.outbound.llm import catalog
from techcom_followup.adapters.outbound.llm.config import (
    RoutingConfig,
    load_routing_config,
)
from techcom_followup.application.ports import LLMPurpose

PRICE = {
    "input_per_mtok": "2",
    "output_per_mtok": "10",
    "source": "test",
    "verified_on": "2026-10-22",
}


def config_dict(**overrides: Any) -> dict[str, Any]:
    route = {"model": "gpt-6.1-sol"}
    data: dict[str, Any] = {
        "routes": {
            "interpret_note": [route],
            "draft_message": [route],
            "orchestrate": [route],
        },
        "prices": {"gpt-6.1-sol": PRICE},
        "daily_budget_usd": "5",
    }
    data.update(overrides)
    return data


def test_repository_config_is_valid_and_ordered(
    routing_config: RoutingConfig,
) -> None:
    for purpose in LLMPurpose:
        models = [r.model for r in routing_config.routes[purpose]]
        assert models == ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"]
        assert all(
            r.provider == "openai" for r in routing_config.routes[purpose]
        )


def test_minimal_config_uses_safe_defaults() -> None:
    config = RoutingConfig.model_validate(config_dict())
    route = config.routes[LLMPurpose.INTERPRET_NOTE][0]
    assert route.effort == "low"
    assert route.service_tier == "auto"
    assert config.request_timeout_s == 20.0


def test_missing_purpose_is_rejected() -> None:
    data = config_dict(routes={"interpret_note": [{"model": "gpt-6-sol"}]})
    with pytest.raises(ValidationError, match="no routes configured"):
        RoutingConfig.model_validate(data)


def test_empty_route_list_is_rejected() -> None:
    data = config_dict(
        routes={
            "interpret_note": [],
            "draft_message": [],
            "orchestrate": [],
        }
    )
    with pytest.raises(ValidationError, match="is empty"):
        RoutingConfig.model_validate(data)


@pytest.mark.owasp_llm("LLM03")
def test_unknown_model_is_rejected() -> None:
    route = [{"model": "gpt-6.1-sool"}]
    data = config_dict(
        routes={
            "interpret_note": route,
            "draft_message": route,
            "orchestrate": route,
        }
    )
    with pytest.raises(ValidationError, match="unknown model"):
        RoutingConfig.model_validate(data)


def test_missing_price_is_rejected() -> None:
    data = config_dict(prices={})
    with pytest.raises(ValidationError, match="no price configured"):
        RoutingConfig.model_validate(data)


def test_effort_outside_literal_is_rejected() -> None:
    route = [{"model": "gpt-6.1-sol", "effort": "minimal"}]
    data = config_dict(
        routes={
            "interpret_note": route,
            "draft_message": route,
            "orchestrate": route,
        }
    )
    with pytest.raises(ValidationError):
        RoutingConfig.model_validate(data)


@pytest.mark.parametrize(
    ("capabilities", "message"),
    [
        (
            catalog.ModelCapabilities(
                provider="openai",
                efforts=frozenset({"medium"}),
                structured_outputs=True,
                max_output_tokens=128_000,
            ),
            "does not support effort",
        ),
        (
            catalog.ModelCapabilities(
                provider="openai",
                efforts=frozenset({"low"}),
                structured_outputs=False,
                max_output_tokens=128_000,
            ),
            "no structured outputs",
        ),
        (
            catalog.ModelCapabilities(
                provider="openai",
                efforts=frozenset({"low"}),
                structured_outputs=True,
                max_output_tokens=1_000,
            ),
            "max_output_tokens too large",
        ),
    ],
)
def test_capability_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capabilities: catalog.ModelCapabilities,
    message: str,
) -> None:
    monkeypatch.setitem(catalog.KNOWN_MODELS, "gpt-6.1-sol", capabilities)
    with pytest.raises(ValidationError, match=message):
        RoutingConfig.model_validate(config_dict())


def test_load_routing_config_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "llm.toml"
    path.write_text(
        'daily_budget_usd = "1"\n'
        '[[routes.interpret_note]]\nmodel = "gpt-6-sol"\n'
        '[[routes.draft_message]]\nmodel = "gpt-6-sol"\n'
        '[[routes.orchestrate]]\nmodel = "gpt-6-sol"\n'
        '[prices."gpt-6-sol"]\n'
        'input_per_mtok = "2"\noutput_per_mtok = "10"\n'
        'source = "t"\nverified_on = "2026-10-22"\n',
        encoding="utf-8",
    )
    config = load_routing_config(path)
    assert config.routes[LLMPurpose.DRAFT_MESSAGE][0].model == "gpt-6-sol"
