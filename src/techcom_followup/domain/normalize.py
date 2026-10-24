"""Turn raw source records into tasks or explicit rejections.

``normalize`` is total: whatever JSON the source sends, it returns a
result and never raises. Unknown values are kept as ``UNKNOWN`` or
``None`` so that the policy can ask a human instead of guessing.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from techcom_followup.domain.models import (
    Flag,
    RejectedRecord,
    Task,
    TaskId,
    TaskStatus,
    UntrustedText,
)
from techcom_followup.domain.source_version import SourceVersion
from techcom_followup.domain.text import clean_line, fold

MAX_NAME_CHARS = 80
MAX_TITLE_CHARS = 80
UNKNOWN_LABEL = "(unknown)"

_TASK_ID = re.compile(r"[A-Z]{2,5}-[0-9]{1,9}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\S+")
_STATUS_SEPARATORS = re.compile(r"[\s_\-]+")

_STATUS_SYNONYMS: Mapping[str, TaskStatus] = {
    "not started": TaskStatus.NOT_STARTED,
    "todo": TaskStatus.NOT_STARTED,
    "to do": TaskStatus.NOT_STARTED,
    "working on it": TaskStatus.WORKING_ON_IT,
    "in progress": TaskStatus.WORKING_ON_IT,
    "working": TaskStatus.WORKING_ON_IT,
    "blocked": TaskStatus.BLOCKED,
    "stuck": TaskStatus.BLOCKED,
    "done": TaskStatus.DONE,
    "complete": TaskStatus.DONE,
    "completed": TaskStatus.DONE,
    "closed": TaskStatus.DONE,
}

_NO_OWNER = frozenset({"", "n/a", "na", "tbd", "unassigned", "none", "-"})


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    """Tasks that passed, plus every record that did not and why."""

    tasks: tuple[Task, ...]
    rejected: tuple[RejectedRecord, ...]


def normalize(records: Sequence[object], as_of: date) -> NormalizationResult:
    """Normalise raw records from the task source.

    Args:
        records: Decoded JSON items, in source order.
        as_of: Evaluation date; used to flag updates from the future.

    Returns:
        Unique tasks in source order and the rejected records.
    """
    accepted: dict[TaskId, tuple[int, Task]] = {}
    rejected: list[RejectedRecord] = []
    for index, record in enumerate(records):
        task_or_reason = _to_task(record, as_of)
        if isinstance(task_or_reason, RejectedRecord):
            rejected.append(_with_index(task_or_reason, index))
            continue
        task = task_or_reason
        previous = accepted.get(task.task_id)
        if previous is None:
            accepted[task.task_id] = (index, task)
        elif _is_newer(task, previous[1]):
            rejected.append(_duplicate(previous[0], task.task_id))
            accepted[task.task_id] = (index, task)
        else:
            rejected.append(_duplicate(index, task.task_id))
    ordered = sorted(accepted.values(), key=lambda item: item[0])
    tasks = tuple(task for _, task in ordered)
    rejected.sort(key=lambda item: item.index)
    return NormalizationResult(tasks, tuple(rejected))


def _to_task(record: object, as_of: date) -> Task | RejectedRecord:
    if not isinstance(record, Mapping):
        return RejectedRecord(0, None, "not_an_object")
    task_id = _task_id(record.get("task_id"))
    if task_id is None:
        return RejectedRecord(0, None, "invalid_task_id")
    project = _label(record.get("project"))
    flags: set[Flag] = set()
    if not _canonical_project(record.get("project"), project):
        # Display cleanup must not create project authority, but the
        # task still needs its follow-up: route it, withhold authority.
        flags.add(Flag.NONCANONICAL_PROJECT)
    due_date = _date(record.get("due_date"), Flag.INVALID_DUE_DATE, flags)
    last_update = _date(
        record.get("last_update"), Flag.INVALID_LAST_UPDATE, flags
    )
    if last_update is not None and last_update > as_of:
        flags.add(Flag.FUTURE_LAST_UPDATE)
    notes = UntrustedText.of(record.get("notes"))
    if notes.truncated:
        flags.add(Flag.NOTE_TRUNCATED)
    return Task(
        task_id=task_id,
        project=project,
        title=_label(record.get("task"), MAX_TITLE_CHARS),
        owner=_owner(record.get("assigned_to")),
        due_date=due_date,
        status=_status(record.get("status")),
        last_update=last_update,
        notes=notes,
        flags=frozenset(flags),
        source_version=_source_version(record.get("last_update"), as_of),
    )


def project_authority(task: Task) -> str | None:
    """Return the project a task may claim, or ``None`` without one.

    An unknown or display-cleaned project still routes the follow-up but
    never grants project authority (source scope, API visibility).
    """
    if (
        task.project == UNKNOWN_LABEL
        or Flag.NONCANONICAL_PROJECT in task.flags
    ):
        return None
    return task.project


def _task_id(value: object) -> TaskId | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    if _TASK_ID.fullmatch(candidate) is None:
        return None
    return TaskId(candidate)


def _status(value: object) -> TaskStatus:
    if not isinstance(value, str):
        return TaskStatus.UNKNOWN
    key = _STATUS_SEPARATORS.sub(" ", value.casefold()).strip()
    return _STATUS_SYNONYMS.get(key, TaskStatus.UNKNOWN)


def _owner(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    owner = clean_line(value, MAX_NAME_CHARS)
    return None if fold(owner) in _NO_OWNER else owner


def _canonical_project(value: object, label: str) -> bool:
    """Return whether the cleaned label is exactly the source value."""
    return not isinstance(value, str) or label in {UNKNOWN_LABEL, value}


def _label(value: object, max_chars: int = MAX_NAME_CHARS) -> str:
    if not isinstance(value, str):
        return UNKNOWN_LABEL
    return clean_line(value, max_chars) or UNKNOWN_LABEL


def _date(value: object, invalid: Flag, flags: set[Flag]) -> date | None:
    if value is None:
        return None
    parsed = _parse_date(value) if isinstance(value, str) else None
    if parsed is None:
        flags.add(invalid)
    return parsed


def _parse_date(value: str) -> date | None:
    text = value.strip()
    try:
        if _ISO_DATE.fullmatch(text):
            return date.fromisoformat(text)
        if _ISO_DATETIME.fullmatch(text):
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(UTC).date()
    except ValueError:
        return None
    return None


def _is_newer(candidate: Task, current: Task) -> bool:
    if candidate.source_version is None:
        return False
    if current.source_version is None:
        return True
    return current.source_version.older_than(candidate.source_version)


def _source_version(value: object, as_of: date) -> SourceVersion | None:
    if not isinstance(value, str) or _parse_date(value) is None:
        return None
    text = value.strip()
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    if moment.date() > as_of:
        # A future claim would fence every real update until that day.
        # Cap it at the evaluation date, without intra-day authority.
        return SourceVersion(
            datetime.combine(as_of, datetime.min.time(), UTC), date_only=True
        )
    return SourceVersion(
        moment, date_only=_ISO_DATE.fullmatch(text) is not None
    )


def _with_index(record: RejectedRecord, index: int) -> RejectedRecord:
    return RejectedRecord(index, record.task_id, record.reason)


def _duplicate(index: int, task_id: TaskId) -> RejectedRecord:
    return RejectedRecord(index, task_id, "duplicate_task_id")
