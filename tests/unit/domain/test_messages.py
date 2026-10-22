from datetime import timedelta

import pytest

from support import AS_OF, make_task
from techcom_followup.domain.messages import (
    BODY_MAX_CHARS,
    BODY_MIN_CHARS,
    LLM_PLACEHOLDERS,
    MAX_FACT_CHARS,
    SUBJECT_MAX_CHARS,
    TEMPLATE_PLACEHOLDERS,
    TEMPLATES,
    DraftSource,
    MessageValidator,
    compose,
    facts_for,
    render,
    template_for,
)
from techcom_followup.domain.models import (
    Decision,
    ReasonCode,
    TaskStatus,
)
from techcom_followup.domain.policy import DecisionPolicy

GOOD_BODY = "Hi {recipient_name}, could you update {task_id} for us?"


@pytest.fixture
def validator() -> MessageValidator:
    return MessageValidator(known_names=["John Smith", "Jo Li"])


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_every_template_passes_the_validator(template_id: str) -> None:
    template = TEMPLATES[template_id]
    validator = MessageValidator(known_names=["John Smith"])
    assert (
        validator.violations(
            template.subject, template.body, TEMPLATE_PLACEHOLDERS
        )
        == ()
    )


def test_code_owned_facts_are_not_offered_to_the_llm() -> None:
    assert {"owner_name", "reason_text"}.isdisjoint(LLM_PLACEHOLDERS)


def test_valid_llm_draft_has_no_violations(
    validator: MessageValidator,
) -> None:
    assert validator.violations("Update on {task_id}", GOOD_BODY) == ()


@pytest.mark.parametrize(
    ("subject", "body", "code"),
    [
        ("", GOOD_BODY, "subject_length"),
        ("x" * 121, GOOD_BODY, "subject_length"),
        ("Update", "Too short", "body_length"),
        ("Update", "x " * (BODY_MAX_CHARS // 2 + 1), "body_length"),
        ("Update\r\nBcc: a", GOOD_BODY, "control_characters"),
        ("Update", GOOD_BODY + "\u202e", "control_characters"),
        ("Update", GOOD_BODY + " {owner_name}", "unknown_placeholder"),
        (
            "Update",
            GOOD_BODY + " {recipient_name.__class__}",
            "unknown_placeholder",
        ),
        ("Update", GOOD_BODY + " {0}", "unknown_placeholder"),
        ("Update", GOOD_BODY + " {task_id", "stray_brace"),
        ("Update", GOOD_BODY + " see https://x.example/a", "url"),
        ("Update", GOOD_BODY + " see www.example.org", "url"),
        ("Update", GOOD_BODY + " see portal.example.com", "url"),
        ("Update", GOOD_BODY + " mail ops@example.com", "email"),
        # A placeholder must not hide the host or mailbox it completes.
        ("Update", GOOD_BODY + " see {project}.com", "url"),
        ("Update", GOOD_BODY + " mail {recipient_name}@example.org", "email"),
        ("Update", GOOD_BODY + " by 2026-10-30", "literal_date"),
        ("Update", GOOD_BODY + " by 10/30", "literal_date"),
        ("Update", GOOD_BODY + " by Oct 30th", "literal_date"),
        ("Update", GOOD_BODY + " by 30 October", "literal_date"),
        ("Update", GOOD_BODY + " by Friday", "literal_date"),
        ("Update", GOOD_BODY + " call 555 12 34 56", "phone"),
        ("Update", GOOD_BODY + " ticket 1234", "digits"),
        (
            "Update",
            GOOD_BODY + " It was marked as complete.",
            "state_change_claim",
        ),
        ("Update", GOOD_BODY + " It has been closed.", "state_change_claim"),
        ("Update", GOOD_BODY + " Status was updated.", "state_change_claim"),
        ("Update", GOOD_BODY + " Ask Smith.", "person_name"),
    ],
)
def test_violations(
    validator: MessageValidator, subject: str, body: str, code: str
) -> None:
    assert code in validator.violations(subject, body)


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("U", GOOD_BODY),
        ("U" * SUBJECT_MAX_CHARS, GOOD_BODY),
        ("Update", "Hi, please update it" + "!" * (BODY_MIN_CHARS - 20)),
        ("Update", "word " * (BODY_MAX_CHARS // 5)),
    ],
)
def test_length_limits_are_inclusive(
    validator: MessageValidator, subject: str, body: str
) -> None:
    assert BODY_MIN_CHARS <= len(body) <= BODY_MAX_CHARS
    assert validator.violations(subject, body) == ()


def test_three_letter_name_tokens_are_checked() -> None:
    validator = MessageValidator(known_names=["Ann Lee"])
    assert "person_name" in validator.violations("Update", GOOD_BODY + " Lee")


def test_body_newlines_are_allowed(validator: MessageValidator) -> None:
    assert validator.violations("Update", GOOD_BODY + "\nThanks!") == ()


def test_short_name_tokens_are_ignored(validator: MessageValidator) -> None:
    # "Jo" and "Li" are too short to tell apart from ordinary words.
    assert validator.violations("Update", GOOD_BODY + " Li") == ()


def test_render_is_one_pass_and_never_formats() -> None:
    facts = {"task_id": "{recipient_name}", "recipient_name": "Ann"}
    assert render("{recipient_name}: {task_id}", facts) == (
        "Ann: {recipient_name}"
    )


def test_render_rejects_unknown_placeholders() -> None:
    with pytest.raises(ValueError, match="unknown placeholder: owner"):
        render("{owner}", {})


def test_compose_overdue_follow_up(policy: DecisionPolicy) -> None:
    task = make_task(due_date=AS_OF - timedelta(days=3))
    decision = policy.decide(task, as_of=AS_OF)
    draft = compose(decision, task, AS_OF)
    assert draft.template_id == "follow_up.overdue.v1"
    assert draft.source is DraftSource.TEMPLATE
    assert draft.subject == "Follow-up: TC-1 Fiber splicing"
    assert draft.body.startswith("Hi John Smith, task TC-1")
    assert "is 3 day(s) overdue" in draft.body


def test_compose_escalation_carries_code_owned_facts(
    policy: DecisionPolicy,
) -> None:
    task = make_task(due_date=AS_OF - timedelta(days=6))
    draft = compose(policy.decide(task, as_of=AS_OF), task, AS_OF)
    assert draft.template_id == "escalate.v1"
    assert "Owner: John Smith." in draft.body
    assert "escalation threshold is 5 day(s)" in draft.body


def test_compose_clarification_lists_missing_fields(
    policy: DecisionPolicy,
) -> None:
    task = make_task(owner=None, due_date=None)
    draft = compose(policy.decide(task, as_of=AS_OF), task, AS_OF)
    assert "is missing: owner, due date." in draft.body
    assert "SURF Illinois coordinator" in draft.body


def test_unknown_status_clarification_names_the_status(
    policy: DecisionPolicy,
) -> None:
    task = make_task(status=TaskStatus.UNKNOWN)
    draft = compose(policy.decide(task, as_of=AS_OF), task, AS_OF)
    assert "is missing: a recognised status." in draft.body


def test_facts_scrub_hostile_task_data(policy: DecisionPolicy) -> None:
    task = make_task(
        title="{recipient_name} visit https://evil.example/x now",
        project="Mail boss@evil.example",
        owner=None,
        due_date=None,
    )
    facts = facts_for(policy.decide(task, as_of=AS_OF), task, AS_OF)
    assert facts["task_title"] == "(recipient_name) visit [link removed] now"
    assert facts["project"] == "Mail [address removed]"
    assert facts["owner_name"] == "unassigned"
    assert facts["due_date"] == "not set"
    assert facts["days_overdue"] == "0"


def test_days_overdue_is_zero_before_the_due_date(
    policy: DecisionPolicy,
) -> None:
    task = make_task(status=TaskStatus.BLOCKED)
    facts = facts_for(policy.decide(task, as_of=AS_OF), task, AS_OF)
    assert facts["days_overdue"] == "0"


def test_facts_are_capped_after_scrubbing(policy: DecisionPolicy) -> None:
    task = make_task(title="word " * 40, owner=None)
    facts = facts_for(policy.decide(task, as_of=AS_OF), task, AS_OF)
    assert len(facts["task_title"]) == MAX_FACT_CHARS - 1


def test_scrubbing_reads_a_bounded_window(policy: DecisionPolicy) -> None:
    # Text past four fact lengths is never examined, so a long link
    # cannot pull distant text into the message.
    url = "https://x.example/" + "p" * (MAX_FACT_CHARS * 4 - 10)
    task = make_task(title=f"Fix {url} SECRET", owner=None)
    facts = facts_for(policy.decide(task, as_of=AS_OF), task, AS_OF)
    assert facts["task_title"] == "Fix [link removed]"


def test_no_action_has_no_template(policy: DecisionPolicy) -> None:
    decision = policy.decide(make_task(), as_of=AS_OF)
    assert decision.reason_code is ReasonCode.ON_TRACK
    with pytest.raises(ValueError, match="have no message"):
        template_for(decision)


def test_actionable_reason_without_target_has_no_template(
    policy: DecisionPolicy,
) -> None:
    decision = policy.decide(make_task(owner=None), as_of=AS_OF)
    broken = Decision(
        task_id=decision.task_id,
        kind=decision.kind,
        target=None,
        rule_id=decision.rule_id,
        reason_code=decision.reason_code,
        reason=decision.reason,
    )
    with pytest.raises(ValueError, match="have no message"):
        template_for(broken)
    assert facts_for(broken, make_task(), AS_OF)["recipient_name"] == "there"
