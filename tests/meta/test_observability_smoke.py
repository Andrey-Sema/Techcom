"""The smoke check verifies the configured alert names."""

import importlib.util
from types import ModuleType
from typing import Any

import pytest
from test_observability_config import alerts

from support import ROOT


def load_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "obs_smoke", ROOT / "scripts" / "obs_smoke.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


obs_smoke = load_smoke()


def prometheus_response(names: list[str]) -> tuple[int, Any]:
    return 200, {"data": {"groups": [{"rules": [{"name": n} for n in names]}]}}


def test_smoke_accepts_exactly_the_configured_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [rule["alert"] for rule in alerts()]
    monkeypatch.setattr(
        obs_smoke, "_request", lambda _url: prometheus_response(names)
    )
    obs_smoke.check_prometheus()


def test_smoke_rejects_a_missing_alert_even_if_the_count_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [rule["alert"] for rule in alerts()]
    names[0] = "UnrelatedAlert"
    monkeypatch.setattr(
        obs_smoke, "_request", lambda _url: prometheus_response(names)
    )
    with pytest.raises(SystemExit, match="FAIL"):
        obs_smoke.check_prometheus()
