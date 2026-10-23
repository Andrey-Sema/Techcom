import json

from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from eval.harness import evaluate_case
from eval.test_harness import case_named
from techcom_followup.domain.models import BlockerCategory, DecisionKind


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


@given(
    st.sampled_from(
        ["action", "target", "recipient", "status", "due_date", "tools"]
    )
)
async def test_schema_cannot_grant_decision_or_action_capabilities(
    field: str,
) -> None:
    case, recordings = case_named("unclear-short")
    output = {
        "blocker": "none",
        "needs_eta": False,
        "confidence": 1.0,
        "evidence": "",
        field: "attacker-controlled capability",
    }
    malicious = (
        recordings[0].model_copy(update={"text": json.dumps(output)}),
        *recordings[1:],
    )
    with capture_logs():
        result = await evaluate_case(case, malicious)
    assert result.failures == 1
    assert result.predicted == BlockerCategory.UNCLEAR
    assert result.actual_decision == DecisionKind.FOLLOW_UP_OWNER
    assert result.unsafe_reasons == ()
