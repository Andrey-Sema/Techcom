from datetime import date, timedelta

import pytest

from support import AS_OF, make_task
from techcom_followup.domain.models import (
    BlockerCategory,
    DecisionKind,
    Flag,
    InterpretationSource,
    NoteInterpretation,
    Priority,
    ReasonCode,
    ScreenResult,
    Target,
    TargetKind,
    Task,
    TaskStatus,
)
from techcom_followup.domain.policy import (
    DecisionPolicy,
    PolicyConfig,
    RoutingTable,
)

OWNER = Target(TargetKind.PERSON, "John Smith", "SURF Illinois")
MANAGER = Target(
    TargetKind.ROLE, "SURF Illinois project manager", "SURF Illinois"
)
WAREHOUSE = Target(TargetKind.TEAM, "Warehouse team", "SURF Illinois")
FLAGGED = ScreenResult(suspicious=True, matched_patterns=("disregard",))


def due_in(days: int) -> date:
    return AS_OF + timedelta(days=days)


def reading(
    blocker: BlockerCategory,
    confidence: float = 0.9,
    source: InterpretationSource = InterpretationSource.LLM,
) -> NoteInterpretation:
    return NoteInterpretation(
        blocker,
        blocker is BlockerCategory.MATERIALS,
        confidence,
        "quote",
        source,
    )


def decide(
    policy: DecisionPolicy,
    task: Task,
    interpretation: NoteInterpretation | None = None,
    screen: ScreenResult = ScreenResult(suspicious=False),  # noqa: B008
) -> tuple[DecisionKind, str, ReasonCode, Target | None]:
    decision = policy.decide(
        task, as_of=AS_OF, screen=screen, interpretation=interpretation
    )
    return (
        decision.kind,
        decision.rule_id,
        decision.reason_code,
        decision.target,
    )


def test_version_comes_from_config(policy: DecisionPolicy) -> None:
    assert policy.version == "2026-10-21.1"


class TestR1Done:
    def test_done_is_quiet_even_when_overdue_and_unowned(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(
            status=TaskStatus.DONE, owner=None, due_date=due_in(-30)
        )
        assert decide(policy, task) == (
            DecisionKind.NO_ACTION,
            "R1",
            ReasonCode.COMPLETED,
            None,
        )


class TestR3MissingInfo:
    def test_missing_owner_goes_to_the_project_coordinator(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(owner=None, project="Wisconsin Build")
        kind, rule, code, target = decide(policy, task)
        assert (kind, rule, code) == (
            DecisionKind.CLARIFY_MISSING_INFO,
            "R3",
            ReasonCode.MISSING_OWNER,
        )
        assert target == Target(
            TargetKind.ROLE, "Wisconsin Build coordinator", "Wisconsin Build"
        )

    def test_missing_due_date_goes_to_the_owner(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(due_date=None)
        assert decide(policy, task)[2:] == (
            ReasonCode.MISSING_DUE_DATE,
            OWNER,
        )

    def test_both_missing_are_listed_in_one_decision(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(owner=None, due_date=None)
        decision = policy.decide(task, as_of=AS_OF)
        assert decision.reason_code is ReasonCode.MISSING_OWNER_AND_DUE_DATE
        assert decision.reason == "Missing: owner, due date."

    def test_unknown_project_falls_back_to_the_operations_desk(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(owner=None, project="Nowhere")
        assert decide(policy, task)[3] == Target(
            TargetKind.TEAM, "Operations desk", "Nowhere"
        )

    def test_missing_info_wins_over_a_materials_blocker(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(owner=None, status=TaskStatus.BLOCKED)
        materials = reading(BlockerCategory.MATERIALS)
        assert decide(policy, task, materials)[1] == "R3"


def test_r4_unknown_status_asks_the_owner(policy: DecisionPolicy) -> None:
    task = make_task(status=TaskStatus.UNKNOWN, due_date=due_in(-9))
    assert decide(policy, task) == (
        DecisionKind.CLARIFY_MISSING_INFO,
        "R4",
        ReasonCode.UNKNOWN_STATUS,
        OWNER,
    )
    assert policy.decide(task, as_of=AS_OF).reason == (
        "Status value is not recognised."
    )


class TestR5Materials:
    @pytest.mark.parametrize(
        ("days", "priority"),
        [
            (-3, Priority.HIGH),
            (0, Priority.HIGH),
            (1, Priority.NORMAL),
            (4, Priority.NORMAL),
        ],
    )
    def test_materials_request_an_eta_from_the_warehouse(
        self, policy: DecisionPolicy, days: int, priority: Priority
    ) -> None:
        task = make_task(status=TaskStatus.BLOCKED, due_date=due_in(days))
        decision = policy.decide(
            task,
            as_of=AS_OF,
            interpretation=reading(BlockerCategory.MATERIALS),
        )
        assert decision.kind is DecisionKind.REQUEST_ETA
        assert decision.rule_id == "R5"
        assert decision.target == WAREHOUSE
        assert decision.priority is priority

    def test_unmapped_blocker_team_uses_the_fallback(self) -> None:
        policy = DecisionPolicy(
            PolicyConfig(version="2026-10-21.1"),
            RoutingTable(fallback_team="Ops"),
        )
        task = make_task(status=TaskStatus.BLOCKED)
        target = decide(policy, task, reading(BlockerCategory.MATERIALS))[3]
        assert target == Target(TargetKind.TEAM, "Ops", "SURF Illinois")


class TestR6Escalate:
    @pytest.mark.parametrize(
        "blocker",
        [
            BlockerCategory.UPSTREAM_DEPENDENCY,
            BlockerCategory.PERMIT,
            BlockerCategory.SITE_ACCESS,
            BlockerCategory.OTHER,
        ],
    )
    def test_dependency_on_a_blocked_task_escalates(
        self, policy: DecisionPolicy, blocker: BlockerCategory
    ) -> None:
        task = make_task(status=TaskStatus.BLOCKED, due_date=due_in(10))
        assert decide(policy, task, reading(blocker)) == (
            DecisionKind.ESCALATE,
            "R6",
            ReasonCode.BLOCKED_DEPENDENCY,
            MANAGER,
        )

    def test_dependency_on_an_overdue_working_task_escalates(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(due_date=due_in(-1))
        upstream = reading(BlockerCategory.UPSTREAM_DEPENDENCY)
        assert decide(policy, task, upstream)[1] == "R6"

    @pytest.mark.parametrize(("days", "rule"), [(-1, "R6"), (0, "R9")])
    def test_dependency_on_a_working_task_waits_for_the_due_date(
        self, policy: DecisionPolicy, days: int, rule: str
    ) -> None:
        task = make_task(due_date=due_in(days))
        upstream = reading(BlockerCategory.UPSTREAM_DEPENDENCY)
        assert decide(policy, task, upstream)[1] == rule

    def test_dependency_note_on_a_healthy_task_is_not_noise(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(due_date=due_in(10))
        permit = reading(BlockerCategory.PERMIT)
        assert decide(policy, task, permit)[1] == "R9"

    @pytest.mark.parametrize(
        ("days", "rule"), [(-4, "R7"), (-5, "R6"), (-6, "R6")]
    )
    def test_overdue_threshold_boundary(
        self, policy: DecisionPolicy, days: int, rule: str
    ) -> None:
        decision = policy.decide(make_task(due_date=due_in(days)), as_of=AS_OF)
        assert decision.rule_id == rule
        if rule == "R6":
            assert decision.reason_code is ReasonCode.OVERDUE_ESCALATION
            assert decision.priority is Priority.HIGH
            assert decision.reason == (
                f"{-days} day(s) overdue; escalation threshold is 5 day(s)."
            )

    def test_unknown_project_escalates_to_the_operations_desk(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(project="Nowhere", due_date=due_in(-6))
        assert decide(policy, task)[3] == Target(
            TargetKind.TEAM, "Operations desk", "Nowhere"
        )


class TestR7FollowUp:
    @pytest.mark.parametrize(("days", "rule"), [(-1, "R7"), (0, "R9")])
    def test_overdue_boundary(
        self, policy: DecisionPolicy, days: int, rule: str
    ) -> None:
        task = make_task(due_date=due_in(days))
        assert decide(policy, task)[1] == rule

    def test_overdue_without_explanation_follows_up_owner(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(due_date=due_in(-3))
        assert decide(policy, task, reading(BlockerCategory.NONE)) == (
            DecisionKind.FOLLOW_UP_OWNER,
            "R7",
            ReasonCode.OVERDUE_NO_EXPLANATION,
            OWNER,
        )

    @pytest.mark.parametrize(
        "interpretation",
        [
            None,
            reading(BlockerCategory.NONE),
            reading(BlockerCategory.UNCLEAR),
            reading(BlockerCategory.UPSTREAM_DEPENDENCY, confidence=0.59),
        ],
    )
    def test_blocked_with_unclear_reason_asks_the_owner(
        self,
        policy: DecisionPolicy,
        interpretation: NoteInterpretation | None,
    ) -> None:
        task = make_task(status=TaskStatus.BLOCKED, due_date=due_in(10))
        assert decide(policy, task, interpretation)[2] is (
            ReasonCode.BLOCKED_REASON_UNCLEAR
        )

    def test_confidence_at_threshold_is_trusted(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(status=TaskStatus.BLOCKED, due_date=due_in(10))
        upstream = reading(BlockerCategory.UPSTREAM_DEPENDENCY, 0.6)
        assert decide(policy, task, upstream)[1] == "R6"


class TestR8DueSoon:
    @pytest.mark.parametrize(
        ("days", "rule"), [(0, "R8"), (1, "R8"), (2, "R9")]
    )
    def test_not_started_window(
        self, policy: DecisionPolicy, days: int, rule: str
    ) -> None:
        task = make_task(status=TaskStatus.NOT_STARTED, due_date=due_in(days))
        decision = policy.decide(task, as_of=AS_OF)
        assert decision.rule_id == rule
        if rule == "R8":
            assert decision.kind is DecisionKind.FOLLOW_UP_OWNER
            assert decision.reason_code is ReasonCode.DUE_SOON_NOT_STARTED
            assert decision.target == OWNER
            assert decision.reason.endswith("; not started.")

    @pytest.mark.parametrize(
        ("last_update", "rule"),
        [(AS_OF - timedelta(days=3), "R8"), (AS_OF - timedelta(days=2), "R9")],
    )
    def test_stale_boundary(
        self, policy: DecisionPolicy, last_update: date, rule: str
    ) -> None:
        task = make_task(due_date=due_in(1), last_update=last_update)
        assert decide(policy, task)[1] == rule

    def test_missing_last_update_counts_as_stale(
        self, policy: DecisionPolicy
    ) -> None:
        decision = policy.decide(
            make_task(due_date=due_in(0), last_update=None), as_of=AS_OF
        )
        assert decision.kind is DecisionKind.FOLLOW_UP_OWNER
        assert decision.reason_code is ReasonCode.DUE_SOON_STALE
        assert decision.reason == "Due today; no update recorded."

    def test_stale_reason_names_the_gap(self, policy: DecisionPolicy) -> None:
        task = make_task(due_date=due_in(1), last_update=due_in(-4))
        assert policy.decide(task, as_of=AS_OF).reason == (
            "Due in 1 day(s); no update for 4 day(s)."
        )


def test_r9_on_track(policy: DecisionPolicy) -> None:
    decision = policy.decide(make_task(), as_of=AS_OF)
    assert (decision.kind, decision.rule_id, decision.target) == (
        DecisionKind.NO_ACTION,
        "R9",
        None,
    )
    assert decision.reason == "On track; due in 9 day(s)."
    assert not decision.actionable


class TestR2Injection:
    def test_flag_is_added_and_interpretation_is_dropped(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(due_date=due_in(-1))
        decision = policy.decide(
            task,
            as_of=AS_OF,
            screen=FLAGGED,
            interpretation=reading(BlockerCategory.MATERIALS),
        )
        assert decision.kind is DecisionKind.FOLLOW_UP_OWNER
        assert decision.interpretation is None
        assert Flag.PROMPT_INJECTION_SUSPECTED in decision.flags

    def test_flagged_note_cannot_silence_a_blocked_task(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(status=TaskStatus.BLOCKED)
        assert decide(policy, task, screen=FLAGGED)[2] is (
            ReasonCode.BLOCKED_REASON_UNCLEAR
        )


def test_llm_fallback_is_flagged(policy: DecisionPolicy) -> None:
    fallback = reading(
        BlockerCategory.UNCLEAR, 0.0, InterpretationSource.FALLBACK
    )
    decision = policy.decide(
        make_task(due_date=due_in(-2)), as_of=AS_OF, interpretation=fallback
    )
    assert decision.flags == {Flag.LLM_FALLBACK}
    assert decision.interpretation == fallback


def test_task_data_quality_flags_are_carried(policy: DecisionPolicy) -> None:
    task = make_task(flags=frozenset({Flag.FUTURE_LAST_UPDATE}))
    assert policy.decide(task, as_of=AS_OF).flags == {Flag.FUTURE_LAST_UPDATE}


class TestConfigValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            {"version": "v1"},
            {"version": "2026-10-21.1", "escalate_after_days": 0},
            {"version": "2026-10-21.1", "min_confidence": 1.5},
            {"version": "2026-10-21.1", "surprise": True},
        ],
    )
    def test_policy_config_rejects_bad_values(
        self, bad: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError, match="validation error"):
            PolicyConfig.model_validate(bad)

    @pytest.mark.parametrize(
        "bad",
        [
            {"fallback_team": ""},
            {"fallback_team": "Ops\r\nBcc: x"},
            {"fallback_team": "Ops", "blocker_teams": {"materials": " W"}},
            {"fallback_team": "Ops", "blocker_teams": {"magic": "W"}},
            {
                "fallback_team": "Ops",
                "projects": {"A": {"coordinator": "c", "manager": "x" * 81}},
            },
            {
                "fallback_team": "Ops",
                "projects": {
                    "Indiana OSP": {"coordinator": "c", "manager": "m"},
                    "indiana  osp": {"coordinator": "c", "manager": "m"},
                },
            },
        ],
    )
    def test_routing_table_rejects_bad_values(
        self, bad: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError, match="validation error"):
            RoutingTable.model_validate(bad)

    def test_recipient_errors_name_the_rule(self) -> None:
        with pytest.raises(ValueError, match="recipient names must be short"):
            RoutingTable.model_validate({"fallback_team": "Ops\r\nBcc: x"})

    def test_project_lookup_ignores_case_and_spacing(self) -> None:
        table = RoutingTable.model_validate(
            {
                "fallback_team": "Ops",
                "projects": {
                    "Indiana OSP": {"coordinator": "C", "manager": "M"}
                },
            }
        )
        assert table.manager("indiana   osp").name == "M"


class TestR7MaterialsEtaKnown:
    """A materials blocker with a known ETA asks for progress only."""

    KNOWN = NoteInterpretation(
        BlockerCategory.MATERIALS,
        False,
        0.9,
        "quote",
        InterpretationSource.LLM,
    )

    @pytest.mark.parametrize(
        ("status", "due"),
        [(TaskStatus.WORKING_ON_IT, -2), (TaskStatus.BLOCKED, 5)],
    )
    def test_late_or_blocked_work_gets_a_progress_check(
        self, policy: DecisionPolicy, status: TaskStatus, due: int
    ) -> None:
        task = make_task(status=status, due_date=due_in(due))
        assert decide(policy, task, self.KNOWN) == (
            DecisionKind.FOLLOW_UP_OWNER,
            "R7",
            ReasonCode.MATERIALS_ETA_KNOWN,
            OWNER,
        )

    def test_on_track_work_is_left_to_the_later_rules(
        self, policy: DecisionPolicy
    ) -> None:
        task = make_task(status=TaskStatus.WORKING_ON_IT, due_date=due_in(10))
        kind, rule, _code, _target = decide(policy, task, self.KNOWN)
        assert (kind, rule) == (DecisionKind.NO_ACTION, "R9")
