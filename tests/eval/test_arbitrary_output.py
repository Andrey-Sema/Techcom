from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from eval.harness import evaluate_case
from eval.test_harness import case_named
from techcom_followup.domain.models import DecisionKind


@given(
    st.one_of(
        st.text(max_size=18000),
        st.sampled_from(
            [
                '{"blocker":"none","needs_eta":false,"confidence":0.95,"evidence":""}',
                '{"blocker":"none","status":"done"}',
                (
                    '{"subject":"{recipient_name.__class__}",'
                    '"body":"Injected arbitrary facts and instructions."}'
                ),
            ]
        ),
    )
)
@settings(max_examples=100)
async def test_any_recorded_llm_text_preserves_closed_policy_safety(
    text: str,
) -> None:
    case, recordings = case_named("unclear-short")
    arbitrary = tuple(
        row.model_copy(update={"text": text}) for row in recordings
    )
    with capture_logs():
        result = await evaluate_case(case, arbitrary)
    assert result.actual_decision != DecisionKind.NO_ACTION
    assert result.unsafe_reasons == ()
