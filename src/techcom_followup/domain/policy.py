"""The decision policy: an ordered rule table, first match wins.

The policy is a pure function of the task, the evaluation date, the
injection screen and (optionally) a note interpretation. It decides
*whether* to act and *whom* to address; a language model can only
supply a blocker category, and every category leads to a human being
asked, never to silence on an overdue task.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from techcom_followup.domain.models import (
    CLEAN_SCREEN,
    BlockerCategory,
    Decision,
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
from techcom_followup.domain.text import clean_line, fold

MAX_RECIPIENT_CHARS = 80

DEPENDENCY_BLOCKERS = frozenset(
    {
        BlockerCategory.UPSTREAM_DEPENDENCY,
        BlockerCategory.PERMIT,
        BlockerCategory.SITE_ACCESS,
        BlockerCategory.OTHER,
    }
)


class PolicyConfig(BaseModel):
    """Thresholds of the rule table (``config/policy.toml``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}\.\d+$")
    escalate_after_days: int = Field(default=5, ge=1, le=90)
    due_soon_days: int = Field(default=1, ge=0, le=30)
    stale_days: int = Field(default=3, ge=1, le=90)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)


def _recipient(value: str) -> str:
    if not value or value != clean_line(value, MAX_RECIPIENT_CHARS):
        msg = "recipient names must be short, single-line and trimmed"
        raise ValueError(msg)
    return value


class ProjectContacts(BaseModel):
    """Role mailboxes of one project."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    coordinator: str
    manager: str

    _check_names = field_validator("coordinator", "manager")(_recipient)


class RoutingTable(BaseModel):
    """Who receives routed messages (``config/routing.toml``).

    Recipients come only from here or from the task owner field;
    nothing in this table can be influenced by task notes or a model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fallback_team: str
    blocker_teams: Mapping[BlockerCategory, str] = Field(default_factory=dict)
    projects: Mapping[str, ProjectContacts] = Field(default_factory=dict)

    _check_fallback = field_validator("fallback_team")(_recipient)

    @field_validator("blocker_teams")
    @classmethod
    def _check_teams(
        cls, value: Mapping[BlockerCategory, str]
    ) -> Mapping[BlockerCategory, str]:
        for team in value.values():
            _recipient(team)
        return value

    @field_validator("projects")
    @classmethod
    def _fold_project_keys(
        cls, value: Mapping[str, ProjectContacts]
    ) -> Mapping[str, ProjectContacts]:
        folded = {fold(name): contacts for name, contacts in value.items()}
        if len(folded) != len(value):
            msg = "project names must differ by more than case or spacing"
            raise ValueError(msg)
        return folded

    def coordinator(self, project: str) -> Target:
        """Return the project coordinator or the fallback team."""
        contacts = self.projects.get(fold(project))
        if contacts is None:
            return self._fallback(project)
        return Target(TargetKind.ROLE, contacts.coordinator, project)

    def manager(self, project: str) -> Target:
        """Return the manager of ``project`` or the fallback team."""
        contacts = self.projects.get(fold(project))
        if contacts is None:
            return self._fallback(project)
        return Target(TargetKind.ROLE, contacts.manager, project)

    def team_for(self, blocker: BlockerCategory, project: str) -> Target:
        """Return the team that resolves ``blocker``."""
        name = self.blocker_teams.get(blocker, self.fallback_team)
        return Target(TargetKind.TEAM, name, project)

    def _fallback(self, project: str) -> Target:
        return Target(TargetKind.TEAM, self.fallback_team, project)


@dataclass(frozen=True, slots=True)
class _Verdict:
    rule_id: str
    kind: DecisionKind
    target: Target | None
    reason_code: ReasonCode
    reason: str
    priority: Priority = Priority.NORMAL


@dataclass(frozen=True, slots=True)
class _Facts:
    task: Task
    owner: Target
    blocker: BlockerCategory
    needs_eta: bool
    delta: int
    days_since_update: int | None


type _Rule = Callable[[_Facts], _Verdict | None]


class DecisionPolicy:
    """Evaluates the ordered rule table R1-R9 (``docs/04`` s. 2)."""

    def __init__(self, config: PolicyConfig, routing: RoutingTable) -> None:
        """Bind thresholds and routing; both are validated config."""
        self._config = config
        self._routing = routing
        self._rules: tuple[_Rule, ...] = (
            self._r4_unknown_status,
            self._r5_materials,
            self._r6_escalate,
            self._r7_follow_up,
            self._r8_due_soon,
        )

    @property
    def version(self) -> str:
        """Return the policy version recorded with every decision."""
        return self._config.version

    @property
    def routing(self) -> RoutingTable:
        """Share the recipient table with the action envelope."""
        return self._routing

    @property
    def due_soon_days(self) -> int:
        """Share the decision horizon with note interpretation."""
        return self._config.due_soon_days

    def decide(
        self,
        task: Task,
        *,
        as_of: date,
        screen: ScreenResult = CLEAN_SCREEN,
        interpretation: NoteInterpretation | None = None,
    ) -> Decision:
        """Return exactly one decision for ``task``.

        Args:
            task: A normalised task.
            as_of: Evaluation date (UTC calendar date).
            screen: Injection screen result for the task notes. A
                flagged note is ignored (rule R2): its interpretation
                is dropped and the blocker becomes ``UNCLEAR``.
            interpretation: Keyword, LLM or fallback interpretation of
                the notes, if one was made.

        Returns:
            The decision of the first matching rule.
        """
        used = None if screen.suspicious else interpretation
        verdict = self._evaluate(task, as_of, used)
        return Decision(
            task_id=task.task_id,
            kind=verdict.kind,
            target=verdict.target,
            rule_id=verdict.rule_id,
            reason_code=verdict.reason_code,
            reason=verdict.reason,
            priority=verdict.priority,
            flags=_flags(task, screen, used),
            interpretation=used,
        )

    def _evaluate(
        self,
        task: Task,
        as_of: date,
        interpretation: NoteInterpretation | None,
    ) -> _Verdict:
        if task.status is TaskStatus.DONE:
            return _Verdict(
                "R1",
                DecisionKind.NO_ACTION,
                None,
                ReasonCode.COMPLETED,
                "Task is done.",
            )
        owner = self._owner_or_coordinator(task)
        if task.owner is None or task.due_date is None:
            return _r3_missing_info(task, owner)
        facts = _Facts(
            task=task,
            owner=owner,
            blocker=self._blocker(interpretation),
            needs_eta=bool(interpretation and interpretation.needs_eta),
            delta=(task.due_date - as_of).days,
            days_since_update=(
                None
                if task.last_update is None
                else (as_of - task.last_update).days
            ),
        )
        for rule in self._rules:
            verdict = rule(facts)
            if verdict is not None:
                return verdict
        return _Verdict(
            "R9",
            DecisionKind.NO_ACTION,
            None,
            ReasonCode.ON_TRACK,
            f"On track; {_timing(facts.delta)}.",
        )

    def _owner_or_coordinator(self, task: Task) -> Target:
        if task.owner is None:
            return self._routing.coordinator(task.project)
        return Target(TargetKind.PERSON, task.owner, task.project)

    def _blocker(
        self, interpretation: NoteInterpretation | None
    ) -> BlockerCategory:
        # No usable reading of the notes (none was made, R2 dropped an
        # injection-like note, or the confidence is too low) leaves the
        # blocker unknown; R7 then asks the owner of a blocked task.
        if (
            interpretation is None
            or interpretation.confidence < self._config.min_confidence
        ):
            return BlockerCategory.UNCLEAR
        return interpretation.blocker

    @staticmethod
    def _r4_unknown_status(facts: _Facts) -> _Verdict | None:
        if facts.task.status is not TaskStatus.UNKNOWN:
            return None
        return _Verdict(
            "R4",
            DecisionKind.CLARIFY_MISSING_INFO,
            facts.owner,
            ReasonCode.UNKNOWN_STATUS,
            "Status value is not recognised.",
        )

    def _r5_materials(self, facts: _Facts) -> _Verdict | None:
        if (
            facts.blocker is not BlockerCategory.MATERIALS
            or not facts.needs_eta
        ):
            return None
        return _Verdict(
            "R5",
            DecisionKind.REQUEST_ETA,
            self._routing.team_for(facts.blocker, facts.task.project),
            ReasonCode.MATERIALS_ETA_UNKNOWN,
            f"Blocked by materials; {_timing(facts.delta)}; "
            "delivery date unknown.",
            Priority.HIGH if facts.delta <= 0 else Priority.NORMAL,
        )

    def _r6_escalate(self, facts: _Facts) -> _Verdict | None:
        blocked = facts.task.status is TaskStatus.BLOCKED
        if facts.blocker in DEPENDENCY_BLOCKERS and (
            blocked or facts.delta < 0
        ):
            code = ReasonCode.BLOCKED_DEPENDENCY
            label = facts.blocker.value.replace("_", " ")
            reason = f"Blocked by {label}; {_timing(facts.delta)}."
        elif facts.delta <= -self._config.escalate_after_days:
            code = ReasonCode.OVERDUE_ESCALATION
            reason = (
                f"{_timing(facts.delta).capitalize()}; escalation "
                f"threshold is {self._config.escalate_after_days} day(s)."
            )
        else:
            return None
        return _Verdict(
            "R6",
            DecisionKind.ESCALATE,
            self._routing.manager(facts.task.project),
            code,
            reason,
            Priority.HIGH,
        )

    @staticmethod
    def _r7_follow_up(facts: _Facts) -> _Verdict | None:
        # A blocked task that reaches R7 has no usable blocker: R5 and
        # R6 already took materials and dependency blockers.
        if facts.blocker is BlockerCategory.MATERIALS:
            if (
                facts.delta >= 0
                and facts.task.status is not TaskStatus.BLOCKED
            ):
                return None
            code = ReasonCode.MATERIALS_ETA_KNOWN
            reason = (
                "Materials dependency has an ETA; "
                f"{_timing(facts.delta)}; please confirm progress."
            )
        elif facts.task.status is TaskStatus.BLOCKED:
            code = ReasonCode.BLOCKED_REASON_UNCLEAR
            reason = (
                f"Status is blocked but the reason is unclear; "
                f"{_timing(facts.delta)}."
            )
        elif facts.delta < 0:
            code = ReasonCode.OVERDUE_NO_EXPLANATION
            reason = (
                f"{_timing(facts.delta).capitalize()}; no blocker explained."
            )
        else:
            return None
        return _Verdict(
            "R7", DecisionKind.FOLLOW_UP_OWNER, facts.owner, code, reason
        )

    def _r8_due_soon(self, facts: _Facts) -> _Verdict | None:
        if facts.delta > self._config.due_soon_days:
            return None
        if facts.task.status is TaskStatus.NOT_STARTED:
            code = ReasonCode.DUE_SOON_NOT_STARTED
            reason = f"{_timing(facts.delta).capitalize()}; not started."
        elif (
            facts.days_since_update is None
            or facts.days_since_update >= self._config.stale_days
        ):
            code = ReasonCode.DUE_SOON_STALE
            reason = (
                f"{_timing(facts.delta).capitalize()}; "
                f"{_staleness(facts.days_since_update)}."
            )
        else:
            return None
        return _Verdict(
            "R8", DecisionKind.FOLLOW_UP_OWNER, facts.owner, code, reason
        )


_MISSING_CODES: Mapping[tuple[str, ...], ReasonCode] = {
    ("owner",): ReasonCode.MISSING_OWNER,
    ("due_date",): ReasonCode.MISSING_DUE_DATE,
}


def _r3_missing_info(task: Task, target: Target) -> _Verdict:
    missing = task.missing_fields()
    code = _MISSING_CODES.get(missing, ReasonCode.MISSING_OWNER_AND_DUE_DATE)
    labels = ", ".join(name.replace("_", " ") for name in missing)
    return _Verdict(
        "R3",
        DecisionKind.CLARIFY_MISSING_INFO,
        target,
        code,
        f"Missing: {labels}.",
    )


def _flags(
    task: Task,
    screen: ScreenResult,
    interpretation: NoteInterpretation | None,
) -> frozenset[Flag]:
    flags = set(task.flags)
    if screen.suspicious:
        flags.add(Flag.PROMPT_INJECTION_SUSPECTED)
    if (
        interpretation is not None
        and interpretation.source is InterpretationSource.FALLBACK
    ):
        flags.add(Flag.LLM_FALLBACK)
    return frozenset(flags)


def _timing(delta: int) -> str:
    if delta < 0:
        return f"{-delta} day(s) overdue"
    if delta == 0:
        return "due today"
    return f"due in {delta} day(s)"


def _staleness(days_since_update: int | None) -> str:
    if days_since_update is None:
        return "no update recorded"
    return f"no update for {days_since_update} day(s)"
