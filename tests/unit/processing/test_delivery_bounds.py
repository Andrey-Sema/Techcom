"""The receiver accepts long source data in every message path."""

from itertools import combinations

import pytest

from support import AS_OF, repo_policy
from techcom_followup.adapters.outbound.llm_stub import ScriptedLLM
from techcom_followup.application.drafter import (
    MessageDrafter,
    approved_phrasings,
)
from techcom_followup.domain.agent_messages import Ask, Tone
from techcom_followup.domain.envelope import build_envelope
from techcom_followup.domain.guard import PlanProposal, review
from techcom_followup.domain.interpretation import prefilter
from techcom_followup.domain.messages import (
    DraftSource,
    MessageDraft,
    MessageTemplate,
    compose,
    redact_contacts,
    render_draft,
)
from techcom_followup.domain.models import Decision, Task
from techcom_followup.domain.normalize import normalize
from techcom_mocks.models import Message


def _source_task(task_id: str, scenario: str) -> Task:
    record: dict[str, object] = {
        "task_id": task_id,
        "task": "T" * 80,
        "project": "P" * 80,
        "assigned_to": "N" * 80,
        "due_date": "2026-10-18",
        "status": "Blocked",
        "last_update": "2026-10-20",
        "notes": "",
    }
    if scenario == "materials":
        record["notes"] = "Awaiting materials; no ETA."
    elif scenario == "clarification":
        record["due_date"] = None
    elif scenario == "escalation":
        record["due_date"] = "2026-10-01"
    result = normalize([record], AS_OF)
    assert result.rejected == ()
    return result.tasks[0]


def _decision(task: Task) -> Decision:
    return repo_policy().decide(
        task, as_of=AS_OF, interpretation=prefilter(task.notes)
    )


def _receiver_accepts(message: MessageDraft, task: Task) -> None:
    parsed = Message(
        subject=message.subject,
        body=message.body,
        template_id=message.template_id,
    )
    assert task.task_id in parsed.subject
    assert task.task_id in parsed.body
    assert task.title in parsed.body


@pytest.mark.parametrize("task_id", ["TC-12345678", "ABCDE-123456789"])
@pytest.mark.parametrize(
    "scenario", ["blocked", "materials", "clarification", "escalation"]
)
def test_templates_preserve_ids_and_obey_receiver_limits(
    task_id: str,
    scenario: str,
) -> None:
    task = _source_task(task_id, scenario)
    _receiver_accepts(compose(_decision(task), task, AS_OF), task)


@pytest.mark.parametrize("task_id", ["TC-12345678", "ABCDE-123456789"])
@pytest.mark.parametrize(
    "scenario", ["blocked", "materials", "clarification", "escalation"]
)
async def test_drafter_preserves_ids_and_obeys_receiver_limits(
    task_id: str,
    scenario: str,
) -> None:
    task = _source_task(task_id, scenario)
    decision = _decision(task)
    baseline = compose(decision, task, AS_OF)
    choice = approved_phrasings(baseline.template_id)[-1]
    message = await MessageDrafter(
        ScriptedLLM([choice.model_dump_json()])
    ).draft(
        decision,
        task,
        as_of=AS_OF,
    )
    assert message.source.value == "llm"
    _receiver_accepts(message, task)


@pytest.mark.parametrize("task_id", ["TC-12345678", "ABCDE-123456789"])
@pytest.mark.parametrize(
    "scenario", ["blocked", "materials", "clarification", "escalation"]
)
def test_agent_preserves_ids_and_obeys_receiver_limits(
    task_id: str,
    scenario: str,
) -> None:
    task = _source_task(task_id, scenario)
    decision = _decision(task)
    envelope = build_envelope(
        decision, task, repo_policy().routing, as_of=AS_OF, prior_follow_ups=1
    )
    assert envelope is not None
    ref = next(
        ref
        for ref, target in envelope.recipients.items()
        if target == decision.target
    )
    allowed = sorted(envelope.messages[decision.kind].asks)
    for asks in [(ask,) for ask in allowed] + list(combinations(allowed, 2)):
        for tone in envelope.tones(decision.kind):
            verdict = review(
                PlanProposal(
                    decision.kind,
                    ref,
                    decision.priority,
                    asks,
                    tone,
                    earlier_contact=True,
                    rationale="Use the baseline.",
                ),
                envelope,
                task,
                as_of=AS_OF,
                note_visible=False,
            )
            primary = envelope.messages[decision.kind].required_any
            if not set(asks) & primary:
                assert verdict.violations == ("missing_primary_ask",)
                continue
            assert verdict.accepted, verdict.violations
            assert verdict.message is not None
            _receiver_accepts(verdict.message, task)


@pytest.mark.parametrize(
    "text",
    [
        "ETA 2026-10-22 12:00",
        "ETA 2026-10-22 - 2026-10-23",
        "ETA 2026-10-22T12:00:00Z",
        "ETA 2026-10-22 12:00 - 2026-10-23 13:30",
        "ETA 2026-10-22T12:00:00+03:00",
    ],
)
def test_contact_redaction_preserves_date_time_context(text: str) -> None:
    assert redact_contacts(text) == text


def test_date_preservation_does_not_keep_adjacent_contacts() -> None:
    result = redact_contacts("ETA 2026-10-22 12:00; call +1 555 010 9999.")
    assert "2026-10-22 12:00" in result
    assert "555" not in result


@pytest.mark.parametrize("size", [19, 801])
def test_rendered_body_bounds_fail_closed(size: int) -> None:
    with pytest.raises(ValueError, match="delivery bounds"):
        render_draft(
            MessageTemplate("test", "Task {task_id}", "{details}"),
            {"task_id": "TC-1", "details": "x" * size},
            source=DraftSource.TEMPLATE,
        )


def test_invalid_date_shaped_contacts_are_still_redacted() -> None:
    assert "2026-99-99" not in redact_contacts("Call 2026-99-99.")


@pytest.mark.parametrize(
    "text",
    [
        "Call +47 2026-10-22",
        "Call 47 2026-10-22",
        "Call +1 2026-10-22-12",
        "Call (+47) 2026-10-22",
    ],
)
def test_phone_cannot_borrow_a_date_substring_exemption(text: str) -> None:
    assert "2026-10-22" not in redact_contacts(text)


def _refuse(*_args: object, **_kwargs: object) -> MessageDraft:
    message = "rendered message exceeds delivery bounds"
    raise ValueError(message)


async def test_drafter_falls_back_when_rendering_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _source_task("TC-1001", "escalation")
    decision = _decision(task)
    baseline = compose(decision, task, AS_OF)
    choice = approved_phrasings(baseline.template_id)[0]
    monkeypatch.setattr(
        "techcom_followup.application.drafter.render_draft", _refuse
    )
    message = await MessageDrafter(
        ScriptedLLM([choice.model_dump_json()])
    ).draft(decision, task, as_of=AS_OF)
    assert message == baseline


def test_agent_plan_is_rejected_when_rendering_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _source_task("TC-1001", "blocked")
    decision = _decision(task)
    envelope = build_envelope(
        decision, task, repo_policy().routing, as_of=AS_OF, prior_follow_ups=0
    )
    assert envelope is not None
    monkeypatch.setattr("techcom_followup.domain.guard.render_draft", _refuse)
    verdict = review(
        PlanProposal(
            decision.kind,
            "owner",
            decision.priority,
            (Ask.STATUS,),
            Tone.NEUTRAL,
            earlier_contact=False,
            rationale="Use the baseline.",
        ),
        envelope,
        task,
        as_of=AS_OF,
        note_visible=False,
    )
    assert verdict.violations == ("rendered_length",)
