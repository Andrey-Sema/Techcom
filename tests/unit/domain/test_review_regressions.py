from datetime import UTC, date, datetime

import pytest

from techcom_followup.domain.interpretation import prefilter
from techcom_followup.domain.models import TaskStatus, UntrustedText
from techcom_followup.domain.normalize import normalize
from techcom_followup.domain.source_version import SourceVersion


@pytest.mark.parametrize("reverse", [False, True])
def test_latest_timestamp_wins_within_one_calendar_day(reverse: bool) -> None:
    records = [
        {"task_id": "TC-1", "status": "Working", "last_update": stamp}
        for stamp in ["2026-10-21T09:00:00Z", "2026-10-21T15:00:00Z"]
    ]
    records[1]["status"] = "Done"
    if reverse:
        records.reverse()
    (task,) = normalize(records, date(2026, 10, 21)).tasks
    assert task.status is TaskStatus.DONE


@pytest.mark.parametrize(
    "text",
    [
        "If materials are delayed, call the warehouse; work is normal.",
        "Training example: waiting for materials.",
        "Potential risk next month: waiting for materials.",
        "If the permit is delayed, contact the coordinator.",
        "Hypothetical scenario: site access denied.",
    ],
)
def test_nonassertive_notes_do_not_get_confident_keyword_hits(
    text: str,
) -> None:
    assert prefilter(UntrustedText.of(text)) is None


@pytest.mark.parametrize(
    ("candidate", "current", "older", "advances"),
    [
        ("2026-10-20T12:00:00Z", "2026-10-21T09:00:00Z", True, False),
        ("2026-10-22", "2026-10-21T09:00:00Z", False, True),
        ("2026-10-21T08:00:00Z", "2026-10-21T09:00:00Z", True, False),
        ("2026-10-21T09:00:00Z", "2026-10-21T09:00:00Z", False, False),
        ("2026-10-21T10:00:00Z", "2026-10-21T09:00:00Z", False, True),
        ("2026-10-21", "2026-10-21T09:00:00Z", False, False),
        ("2026-10-21T09:00:00Z", "2026-10-21", False, True),
        ("2026-10-21", "2026-10-21", False, False),
        ("2026-10-22T00:30:00+03:00", "2026-10-21T21:00:00Z", False, True),
    ],
)
def test_source_order_respects_utc_and_precision(
    candidate: str, current: str, older: bool, advances: bool
) -> None:
    tasks = [
        normalize(
            [{"task_id": "TC-1", "last_update": value}], date(2026, 10, 24)
        ).tasks[0]
        for value in (candidate, current)
    ]
    first, second = (task.source_version for task in tasks)
    assert first is not None
    assert second is not None
    assert first.older_than(second) is older
    assert first.advances(second) is advances
    assert first.advances(None)


def test_source_versions_require_aware_instants() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SourceVersion(datetime(2026, 10, 21, tzinfo=UTC).replace(tzinfo=None))
    assert (
        SourceVersion(datetime(2026, 10, 21, tzinfo=UTC)).updated_at.tzinfo
        is UTC
    )


def test_future_source_version_is_capped_at_the_evaluation_date() -> None:
    as_of = date(2026, 10, 21)
    (future, real) = (
        normalize([{"task_id": "TC-1", "last_update": value}], as_of)
        .tasks[0]
        .source_version
        for value in ("2099-01-01T00:00:00Z", "2026-10-21T15:00:00Z")
    )
    assert future == SourceVersion(datetime(2026, 10, 21, tzinfo=UTC), True)
    assert real is not None
    assert not real.older_than(future)
    assert not future.advances(real)
