import tomllib
from pathlib import Path

import pytest

from support import ROOT
from techcom_followup.domain.models import BlockerCategory
from techcom_followup.infrastructure.policy_files import (
    load_decision_policy,
    load_policy_config,
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
