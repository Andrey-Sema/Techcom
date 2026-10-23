"""Static checks of the observability stack; no Docker needed.

The live counterpart is ``make obs-smoke``; ``make obs-check`` runs
promtool, Loki and Alloy validators inside the pinned images.
"""

import json
import re
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from support import ROOT
from techcom_followup.adapters.inbound.api.observability import Observer
from techcom_followup.infrastructure.otlp import OtlpLogExporter

if TYPE_CHECKING:
    import httpx

DEPLOY = ROOT / "deploy"
RULES = DEPLOY / "prometheus" / "rules" / "followup.rules.yml"
RULE_TESTS = DEPLOY / "prometheus" / "rules" / "followup.rules.test.yml"
DASHBOARD = DEPLOY / "grafana" / "dashboards" / "followup-overview.json"
DATASOURCES = DEPLOY / "grafana" / "provisioning" / "datasources"
COMPOSE = ROOT / "compose.observability.yaml"
METRIC = re.compile(r"\bfollowup_[a-z_]+")
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def catalogue() -> set[str]:
    """Metric names declared in docs/14 section 8 (single source)."""
    text = (ROOT / "docs" / "14-logging-and-observability.md").read_text(
        encoding="utf-8"
    )
    section = text.split("## 8. Metrics catalogue", 1)[1].split("## 9.", 1)[0]
    return set(re.findall(r"^\| `(followup_[a-z_]+)`", section, re.MULTILINE))


def base_metric(name: str) -> str:
    for suffix in HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def alerts() -> list[dict[str, Any]]:
    return [
        rule
        for group in load_yaml(RULES)["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]


def dashboard_queries() -> list[str]:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]


def test_rules_and_dashboard_use_only_catalogued_metrics() -> None:
    expressions = [rule["expr"] for rule in alerts()] + dashboard_queries()
    used = {
        base_metric(name)
        for expression in expressions
        for name in METRIC.findall(expression)
    }
    assert used
    assert used <= catalogue(), used - catalogue()


def exported_families() -> set[str]:
    """Families the API process exports, counters with their suffix."""
    observer = Observer()
    # The exporter only registers its counters; it is never started.
    OtlpLogExporter(
        cast("httpx.AsyncClient", None),
        "https://alloy/v1/logs",
        observer.registry,
    )
    return {
        family.name + ("_total" if family.type == "counter" else "")
        for family in observer.registry.collect()
    }


def test_catalogue_is_exactly_what_the_service_exports() -> None:
    assert exported_families() == catalogue()


def test_every_alert_is_actionable() -> None:
    runbook = (ROOT / "docs" / "18-runbook.md").read_text(encoding="utf-8")
    anchors = set(re.findall(r"\{#([a-z0-9-]+)\}", runbook))
    for rule in alerts():
        assert rule["labels"]["severity"] in {"page", "ticket"}, rule
        assert rule["annotations"]["summary"], rule
        url = rule["annotations"]["runbook_url"]
        assert url.startswith("docs/18-runbook.md#"), url
        assert url.split("#", 1)[1] in anchors, url


def test_every_alert_has_a_promtool_test() -> None:
    tested = {
        case["alertname"]
        for test in load_yaml(RULE_TESTS)["tests"]
        for case in test.get("alert_rule_test", [])
        if case["exp_alerts"]
    }
    assert {rule["alert"] for rule in alerts()} == tested


def test_cost_alert_tracks_the_configured_budget() -> None:
    with (ROOT / "config" / "llm.toml").open("rb") as handle:
        budget = Decimal(tomllib.load(handle)["daily_budget_usd"])
    (rule,) = [r for r in alerts() if r["alert"] == "FollowupLLMCostBudget"]
    threshold = Decimal(rule["expr"].rsplit(">", 1)[1].strip())
    assert threshold == budget * Decimal("1.2")


def test_dashboard_uses_provisioned_datasources() -> None:
    provisioned = {
        source["uid"]
        for path in DATASOURCES.glob("*.yml")
        for source in load_yaml(path)["datasources"]
    }
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    used = {
        panel["datasource"]["uid"]
        for panel in dashboard["panels"]
        if "datasource" in panel
    }
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert used == provisioned
    assert len(ids) == len(set(ids))
    assert dashboard["editable"] is False


class TestComposeHardening:
    @pytest.fixture
    def services(self) -> dict[str, Any]:
        services: dict[str, Any] = load_yaml(COMPOSE)["services"]
        return services

    def test_images_are_pinned_by_digest(
        self, services: dict[str, Any]
    ) -> None:
        for name, service in services.items():
            assert re.fullmatch(
                r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", service["image"]
            ), name

    def test_no_docker_socket_and_read_only_mounts(
        self, services: dict[str, Any]
    ) -> None:
        for name, service in services.items():
            for volume in service.get("volumes", []):
                assert "docker.sock" not in volume, name
                if volume.startswith("./"):
                    assert volume.endswith(":ro"), (name, volume)

    def test_ports_bind_to_localhost_only(
        self, services: dict[str, Any]
    ) -> None:
        for name, service in services.items():
            for port in service.get("ports", []):
                assert port.startswith("127.0.0.1:"), (name, port)

    def test_containers_are_hardened(self, services: dict[str, Any]) -> None:
        for name, service in services.items():
            assert service["read_only"] is True, name
            assert service["cap_drop"] == ["ALL"], name
            assert "no-new-privileges:true" in service["security_opt"], name

    def test_grafana_has_no_inline_credentials(
        self, services: dict[str, Any]
    ) -> None:
        environment = services["grafana"]["environment"]
        assert environment["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
        assert environment["GF_USERS_ALLOW_SIGN_UP"] == "false"
        assert "GF_SECURITY_ADMIN_PASSWORD" not in environment
        assert environment["GF_SECURITY_ADMIN_PASSWORD__FILE"].startswith(
            "/run/secrets/"
        )
