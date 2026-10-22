import typing
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from openai.types.shared.chat_model import ChatModel

from techcom_followup.adapters.outbound.llm.catalog import (
    KNOWN_MODELS,
    ModelPrice,
)
from techcom_followup.application.ports import TokenUsage

SOL_MODELS = {"gpt-5.6-sol", "gpt-6-sol", "gpt-6.1-sol"}


def price(**overrides: object) -> ModelPrice:
    data: dict[str, object] = {
        "input_per_mtok": "2",
        "output_per_mtok": "10",
        "source": "test",
        "verified_on": "2026-10-22",
    }
    data.update(overrides)
    return ModelPrice.model_validate(data)


def test_catalogue_covers_the_sol_generation() -> None:
    assert set(KNOWN_MODELS) == SOL_MODELS


def test_every_catalogue_model_exists_in_the_installed_sdk() -> None:
    # Guards against typos and against an SDK downgrade that no longer
    # knows a model we route to.
    sdk_models = set(typing.get_args(ChatModel))
    assert set(KNOWN_MODELS) <= sdk_models


def test_sol_models_reject_disabled_reasoning() -> None:
    for capabilities in KNOWN_MODELS.values():
        assert "none" not in capabilities.efforts
        assert "minimal" not in capabilities.efforts
        assert capabilities.structured_outputs


def test_cost_plain_input_and_output() -> None:
    usage = TokenUsage(input_tokens=100_000, output_tokens=10_000)
    assert price().cost(usage) == Decimal("0.300000")


def test_cost_uses_cached_price_when_known() -> None:
    usage = TokenUsage(
        input_tokens=100_000,
        output_tokens=0,
        cached_input_tokens=50_000,
    )
    cost = price(cached_input_per_mtok="0.2").cost(usage)
    assert cost == Decimal("0.110000")


def test_cost_bills_unknown_cached_price_at_full_input_price() -> None:
    usage = TokenUsage(
        input_tokens=100_000,
        output_tokens=0,
        cached_input_tokens=50_000,
    )
    assert price().cost(usage) == Decimal("0.200000")


def test_cost_cache_writes_carry_a_premium() -> None:
    usage = TokenUsage(
        input_tokens=100_000,
        output_tokens=0,
        cache_write_tokens=100_000,
    )
    assert price().cost(usage) == Decimal("0.250000")


def test_cost_long_context_multipliers_apply_to_whole_request() -> None:
    usage = TokenUsage(input_tokens=300_000, output_tokens=100_000)
    # 0.3M * 2 * 2 + 0.1M * 10 * 1.5
    assert price().cost(usage) == Decimal("2.700000")


def test_cost_without_long_context_threshold() -> None:
    usage = TokenUsage(input_tokens=300_000, output_tokens=0)
    cost = price(long_context_threshold=None).cost(usage)
    assert cost == Decimal("0.600000")


@given(
    input_tokens=st.integers(min_value=0, max_value=2_000_000),
    output_tokens=st.integers(min_value=0, max_value=200_000),
    cached=st.integers(min_value=0, max_value=2_000_000),
    extra=st.integers(min_value=1, max_value=10_000),
)
def test_cost_is_non_negative_and_monotonic_in_output(
    input_tokens: int,
    output_tokens: int,
    cached: int,
    extra: int,
) -> None:
    model_price = price()
    base = TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=min(cached, input_tokens),
    )
    more = TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens + extra,
        cached_input_tokens=min(cached, input_tokens),
    )
    assert model_price.cost(base) >= 0
    assert model_price.cost(more) >= model_price.cost(base)
