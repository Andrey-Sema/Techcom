import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from support import ROOT
from techcom_followup.domain.models import BlockerCategory
from techcom_followup.infrastructure.policy_files import (
    load_decision_policy,
    load_policy_config,
    load_retention_policy,
    load_routing_table,
)


def test_repository_config_loads() -> None:
    config = load_policy_config(ROOT / "config" / "policy.toml")
    assert config.escalate_after_days == 5
    assert config.due_soon_days == 1
    routing = load_routing_table(ROOT / "config" / "routing.toml")
    assert routing.blocker_teams[BlockerCategory.MATERIALS] == "Warehouse team"


def test_every_fixture_project_has_contacts(
    fixture_records: list[dict[str, str]],
) -> None:
    routing = load_routing_table(ROOT / "config" / "routing.toml")
    for record in fixture_records:
        assert routing.coordinator(record["project"]).name.endswith(
            "coordinator"
        )


def test_policy_is_built_from_a_directory(tmp_path: Path) -> None:
    (tmp_path / "policy.toml").write_text('version = "2026-11-01.2"\n')
    (tmp_path / "routing.toml").write_text('fallback_team = "Ops"\n')
    assert load_decision_policy(tmp_path).version == "2026-11-01.2"


def test_typo_in_config_fails_fast(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text('version = "2026-11-01.2"\nescalate_after = 3\n')
    with pytest.raises(ValueError, match="escalate_after"):
        load_policy_config(path)


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "routing.toml"
    path.write_text("fallback_team = \n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_routing_table(path)


def test_committed_retention_matches_the_data_classification() -> None:
    policy = load_retention_policy(ROOT / "config" / "retention.toml")
    # docs/15 s. 8: internal data 30-90 days, personal data one year.
    assert policy.decision_days <= 90
    assert policy.action_days == policy.audit_days == 365


@pytest.mark.parametrize(
    "change",
    [{"decision_days": 365}, {"batch_size": 0}, {"surprise": 1}],
)
def test_retention_bounds_are_enforced(
    tmp_path: Path, change: dict[str, int]
) -> None:
    text = (ROOT / "config" / "retention.toml").read_text(encoding="utf-8")
    values = tomllib.loads(text) | change
    path = tmp_path / "retention.toml"
    path.write_text(
        "\n".join(f"{key} = {value!r}" for key, value in values.items()),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="validation error"):
        load_retention_policy(path)


def test_cutoffs_require_an_aware_clock() -> None:
    policy = load_retention_policy(ROOT / "config" / "retention.toml")
    with pytest.raises(ValueError, match="timezone"):
        policy.cutoffs(datetime(2026, 10, 22))  # noqa: DTZ001
