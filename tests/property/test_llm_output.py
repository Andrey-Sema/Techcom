import asyncio
import json
from datetime import timedelta

from hypothesis import given
from hypothesis import strategies as st

from support import AS_OF, make_task, repo_policy
from techcom_followup.adapters.outbound.llm_stub import ScriptedLLM
from techcom_followup.application.drafter import MessageDrafter
from techcom_followup.application.interpreter import NoteInterpreter
from techcom_followup.domain.messages import BODY_MAX_CHARS, SUBJECT_MAX_CHARS
from techcom_followup.domain.models import CLEAN_SCREEN, DecisionKind

_JSON = st.recursive(
    st.none() | st.booleans() | st.floats() | st.text(),
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(st.text(max_size=30), children, max_size=5)
    ),
    max_leaves=20,
)


@given(st.text() | _JSON.map(json.dumps))
def test_arbitrary_llm_output_never_crashes_or_silences_overdue(
    text: str,
) -> None:
    async def exercise() -> None:
        task = make_task(
            due_date=AS_OF - timedelta(days=1),
            notes="The latest field update is inconclusive.",
        )
        stub = ScriptedLLM([text, text])
        interpretation = await NoteInterpreter(stub).interpret(
            task, as_of=AS_OF, screen=CLEAN_SCREEN
        )
        decision = repo_policy().decide(
            task, as_of=AS_OF, interpretation=interpretation
        )
        assert decision.kind is not DecisionKind.NO_ACTION
        draft = await MessageDrafter(stub).draft(decision, task, as_of=AS_OF)
        assert len(draft.subject) <= SUBJECT_MAX_CHARS
        assert len(draft.body) <= BODY_MAX_CHARS
        assert "{" not in draft.subject + draft.body

    asyncio.run(exercise())
