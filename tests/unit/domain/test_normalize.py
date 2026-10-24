from datetime import date
from typing import Any

import pytest

from support import AS_OF
from techcom_followup.domain.models import (
    MAX_NOTE_CHARS,
    Flag,
    TaskStatus,
    UntrustedText,
)
from techcom_followup.domain.normalize import UNKNOWN_LABEL, normalize


def record(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "task_id": "TC-1001",
        "project": "SURF Illinois",
        "task": "Fiber splicing",
        "assigned_to": "John Smith",
        "due_date": "2026-10-18",
        "status": "Working on it",
        "last_update": "2026-10-16",
        "notes": "No update since Monday.",
    }
    data.update(overrides)
    return data


def one(**overrides: Any) -> Any:
    result = normalize([record(**overrides)], AS_OF)
    assert result.rejected == ()
    (task,) = result.tasks
    return task


def test_fixture_normalizes_without_rejections(
    fixture_records: list[object],
) -> None:
    result = normalize(fixture_records, AS_OF)
    assert len(result.tasks) == 10
    assert result.rejected == ()
    assert [t.task_id for t in result.tasks][:2] == ["TC-1001", "TC-1002"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Working on it", TaskStatus.WORKING_ON_IT),
        ("  WORKING   on IT ", TaskStatus.WORKING_ON_IT),
        ("in-progress", TaskStatus.WORKING_ON_IT),
        ("Not_Started", TaskStatus.NOT_STARTED),
        ("todo", TaskStatus.NOT_STARTED),
        ("Stuck", TaskStatus.BLOCKED),
        ("Completed", TaskStatus.DONE),
        ("closed", TaskStatus.DONE),
        ("Paused by client", TaskStatus.UNKNOWN),
        (None, TaskStatus.UNKNOWN),
        (3, TaskStatus.UNKNOWN),
    ],
)
def test_status_synonyms(raw: object, expected: TaskStatus) -> None:
    assert one(status=raw).status is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  John   Smith ", "John Smith"),
        ("Jöhn\u200b Smith", "Jöhn Smith"),
        ("", None),
        ("  N/A ", None),
        ("TBD", None),
        ("unassigned", None),
        (None, None),
        (42, None),
    ],
)
def test_owner_placeholders_mean_no_owner(
    raw: object, expected: str | None
) -> None:
    assert one(assigned_to=raw).owner == expected


@pytest.mark.parametrize("raw", [" tc-1001 ", "TC-1001"])
def test_task_id_is_canonical(raw: str) -> None:
    assert one(task_id=raw).task_id == "TC-1001"


@pytest.mark.parametrize(
    "raw", [None, "", "1001", "TC1001", "T-1", 1001, "TC-\u0661\u0662"]
)
@pytest.mark.asvs("2.2.1")
def test_invalid_task_id_is_rejected(raw: object) -> None:
    result = normalize([record(task_id=raw)], AS_OF)
    assert result.tasks == ()
    (rejected,) = result.rejected
    assert rejected.reason == "invalid_task_id"
    assert rejected.index == 0


def test_non_object_records_are_rejected() -> None:
    result = normalize([record(), ["TC-2"], "TC-3"], AS_OF)
    assert [r.index for r in result.rejected] == [1, 2]
    assert {r.reason for r in result.rejected} == {"not_an_object"}


@pytest.mark.parametrize(
    "project",
    [" Project A", "Project A\n", "Project\u200b A", "A" * 80 + "B"],
)
def test_lossy_project_names_keep_the_task_without_authority(
    project: str,
) -> None:
    result = normalize([record(project=project)], AS_OF)
    (task,) = result.tasks
    assert result.rejected == ()
    assert Flag.NONCANONICAL_PROJECT in task.flags


def test_distinct_long_projects_cannot_share_truncated_authority() -> None:
    result = normalize(
        [
            record(task_id="TC-1", project="A" * 80 + "B"),
            record(task_id="TC-2", project="A" * 80 + "C"),
        ],
        AS_OF,
    )
    assert result.rejected == ()
    assert all(
        Flag.NONCANONICAL_PROJECT in task.flags for task in result.tasks
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-18", date(2026, 10, 18)),
        (" 2026-10-18 ", date(2026, 10, 18)),
        ("2026-10-18T23:30:00-05:00", date(2026, 10, 19)),
        ("2026-10-18T10:00:00", date(2026, 10, 18)),
        ("2026-10-18 10:00:00+00:00", date(2026, 10, 18)),
    ],
)
def test_dates_accept_iso_dates_and_datetimes(
    raw: str, expected: date
) -> None:
    task = one(due_date=raw)
    assert task.due_date == expected
    assert Flag.INVALID_DUE_DATE not in task.flags


@pytest.mark.parametrize(
    "raw", ["18/10/2026", "2026-13-40", "2026-10-18Tnoon", "soon", 20261018]
)
@pytest.mark.asvs("2.2.3")
def test_bad_due_date_becomes_none_and_is_flagged(raw: object) -> None:
    task = one(due_date=raw)
    assert task.due_date is None
    assert Flag.INVALID_DUE_DATE in task.flags


def test_missing_due_date_is_not_a_data_quality_flag() -> None:
    task = one(due_date=None)
    assert task.due_date is None
    assert task.flags == frozenset()


def test_bad_last_update_is_flagged() -> None:
    task = one(last_update="yesterday")
    assert task.last_update is None
    assert task.flags == {Flag.INVALID_LAST_UPDATE}


@pytest.mark.asvs("2.2.3")
def test_future_last_update_is_kept_and_flagged() -> None:
    task = one(last_update="2026-10-25")
    assert task.last_update == date(2026, 10, 25)
    assert Flag.FUTURE_LAST_UPDATE in task.flags


@pytest.mark.asvs("1.1.1")
def test_labels_are_single_line_and_never_empty() -> None:
    task = one(project="  ", task="Splice\r\nBcc: x\tverify")
    assert task.project == UNKNOWN_LABEL
    assert task.title == "Splice Bcc: x verify"
    assert one(project=None).project == UNKNOWN_LABEL


def test_long_title_is_capped() -> None:
    assert len(one(task="x" * 500).title) == 80


@pytest.mark.asvs("1.3.3")
def test_notes_are_untrusted_cleaned_and_capped() -> None:
    task = one(notes="  line one\r\nline\u202e two\x00  ")
    assert task.notes == UntrustedText("line one\nline two")
    long_task = one(notes="a" * (MAX_NOTE_CHARS + 1))
    assert long_task.notes.truncated
    assert len(long_task.notes.value) == MAX_NOTE_CHARS
    assert Flag.NOTE_TRUNCATED in long_task.flags
    assert one(notes=None).notes == UntrustedText("")
    assert not one(notes={"x": 1}).notes


@pytest.mark.asvs("1.1.1")
def test_full_width_text_is_nfkc_normalised() -> None:
    assert (
        one(
            notes="\uff4d\uff41\uff54\uff45\uff52\uff49\uff41\uff4c\uff53"
        ).notes.value
        == "materials"
    )


def test_extra_fields_are_ignored() -> None:
    assert one(priority="urgent", owner_email="x@example.com").owner == (
        "John Smith"
    )


class TestDuplicates:
    @pytest.mark.asvs("2.2.3")
    def test_latest_update_wins(self) -> None:
        records = [
            record(last_update="2026-10-10", notes="old"),
            record(last_update="2026-10-16", notes="new"),
        ]
        result = normalize(records, AS_OF)
        (task,) = result.tasks
        assert task.notes.value == "new"
        assert [(r.index, r.reason) for r in result.rejected] == [
            (0, "duplicate_task_id")
        ]

    def test_tie_keeps_first_occurrence(self) -> None:
        records = [record(notes="first"), record(notes="second")]
        result = normalize(records, AS_OF)
        assert result.tasks[0].notes.value == "first"
        assert result.rejected[0].index == 1

    def test_missing_update_never_beats_a_dated_one(self) -> None:
        records = [record(notes="dated"), record(last_update=None)]
        assert normalize(records, AS_OF).tasks[0].notes.value == "dated"

    def test_dated_update_beats_a_missing_one(self) -> None:
        records = [record(last_update=None), record(notes="dated")]
        result = normalize(records, AS_OF)
        assert result.tasks[0].notes.value == "dated"
        assert result.rejected[0].index == 0

    def test_ids_are_compared_after_canonicalisation(self) -> None:
        records = [record(task_id="tc-1001"), record(task_id="TC-1001 ")]
        assert len(normalize(records, AS_OF).tasks) == 1

    def test_source_order_is_kept(self) -> None:
        records = [
            record(task_id="TC-2"),
            record(task_id="TC-1", last_update=None),
            record(task_id="TC-1"),
        ]
        result = normalize(records, AS_OF)
        assert [t.task_id for t in result.tasks] == ["TC-2", "TC-1"]
