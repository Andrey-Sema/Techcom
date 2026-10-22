"""End-to-end smoke test of the observability stack (make obs-smoke).

Checks, against a running ``compose.observability.yaml`` stack:

1. Prometheus, Loki, Alloy and Grafana are ready.
2. Prometheus loaded every alert rule from the rules file.
3. Grafana provisioned both datasources and the overview dashboard,
   and anonymous access is refused.
4. A log record pushed to Alloy over OTLP reaches Loki, and the
   redaction step removed the forbidden ``notes`` attribute.

Standard library only; developer tool, not shipped (ASVS 15.2.3).
"""

import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any

PROMETHEUS = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
ALLOY_OTLP = "http://127.0.0.1:4318"
GRAFANA = "http://127.0.0.1:3000"
EXPECTED_RULES = 8
DEADLINE_S = 120.0


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    # Local fixed URLs only; the scheme is always http://127.0.0.1.
    request = urllib.request.Request(  # noqa: S310
        url, data=data, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        return error.code, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode("utf-8", "replace")


def _wait(
    name: str, url: str, ready: frozenset[int] = frozenset({HTTPStatus.OK})
) -> None:
    deadline = time.monotonic() + DEADLINE_S
    while time.monotonic() < deadline:
        try:
            status, _ = _request(url)
        except OSError:
            status = 0
        if status in ready:
            print(f"ok    {name} ready")
            return
        time.sleep(2)
    sys.exit(f"FAIL  {name} not ready at {url}")


def _check(condition: object, message: str) -> None:
    if not condition:
        sys.exit(f"FAIL  {message}")
    print(f"ok    {message}")


def _grafana_auth() -> dict[str, str]:
    user = Path("secrets/grafana_admin_user").read_text().strip()
    password = Path("secrets/grafana_admin_password").read_text().strip()
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def check_prometheus() -> None:
    """Every alert rule from the rules file is loaded."""
    _, payload = _request(f"{PROMETHEUS}/api/v1/rules?type=alert")
    rules = [
        rule["name"]
        for group in payload["data"]["groups"]
        for rule in group["rules"]
    ]
    _check(
        len(rules) == EXPECTED_RULES,
        f"Prometheus loaded {len(rules)}/{EXPECTED_RULES} alert rules",
    )


def check_dashboard_queries() -> None:
    """Every PromQL expression of the dashboard parses in Prometheus."""
    path = Path("deploy/grafana/dashboards/followup-overview.json")
    dashboard = json.loads(path.read_text())
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if panel.get("datasource", {}).get("uid") == "prometheus"
    ]
    for expression in expressions:
        query = urllib.parse.urlencode({"query": expression})
        status, payload = _request(f"{PROMETHEUS}/api/v1/query?{query}")
        if status != HTTPStatus.OK or payload.get("status") != "success":
            sys.exit(f"FAIL  dashboard query rejected: {expression}")
    _check(expressions, f"{len(expressions)} dashboard queries are valid")


def check_grafana() -> None:
    """Datasources and dashboard exist; anonymous access is refused."""
    status, _ = _request(f"{GRAFANA}/api/search")
    _check(
        status == HTTPStatus.UNAUTHORIZED,
        "Grafana refuses anonymous API access",
    )
    default = base64.b64encode(b"admin:admin").decode()
    status, _ = _request(
        f"{GRAFANA}/api/search",
        headers={"Authorization": f"Basic {default}"},
    )
    _check(
        status == HTTPStatus.UNAUTHORIZED,
        "Grafana default admin/admin credentials are refused",
    )
    auth = _grafana_auth()
    for uid in ("prometheus", "loki"):
        status, _ = _request(
            f"{GRAFANA}/api/datasources/uid/{uid}", headers=auth
        )
        _check(
            status == HTTPStatus.OK, f"Grafana datasource {uid} provisioned"
        )
    status, payload = _request(
        f"{GRAFANA}/api/dashboards/uid/followup-overview", headers=auth
    )
    _check(
        status == HTTPStatus.OK and payload["dashboard"]["panels"],
        "Grafana dashboard followup-overview provisioned",
    )


def _otlp_record(marker: str) -> bytes:
    now_ns = str(time.time_ns())
    record = {
        "timeUnixNano": now_ns,
        "severityText": "WARNING",
        "body": {"stringValue": f"obs.smoke {marker}"},
        "attributes": [
            {"key": "event", "value": {"stringValue": "obs.smoke"}},
            {"key": "marker", "value": {"stringValue": marker}},
            # Must be stripped by the Alloy redaction processor.
            {"key": "notes", "value": {"stringValue": "must-not-be-stored"}},
        ],
    }
    resource = {
        "attributes": [
            {"key": "service.name", "value": {"stringValue": "followup"}},
        ],
    }
    payload = {
        "resourceLogs": [
            {"resource": resource, "scopeLogs": [{"logRecords": [record]}]},
        ],
    }
    return json.dumps(payload).encode()


def check_log_pipeline() -> None:
    """An OTLP record reaches Loki without its forbidden attributes."""
    marker = uuid.uuid4().hex
    status, _ = _request(
        f"{ALLOY_OTLP}/v1/logs",
        data=_otlp_record(marker),
        headers={"Content-Type": "application/json"},
    )
    _check(status == HTTPStatus.OK, "Alloy accepted an OTLP log record")
    query = urllib.parse.urlencode(
        {
            "query": f'{{service_name="followup"}} | marker="{marker}"',
            "limit": "5",
            "start": str(time.time_ns() - 600 * 10**9),
        }
    )
    deadline = time.monotonic() + DEADLINE_S
    while time.monotonic() < deadline:
        _, payload = _request(f"{LOKI}/loki/api/v1/query_range?{query}")
        streams = payload["data"]["result"] if payload else []
        if streams:
            labels = streams[0]["stream"]
            print("ok    log record arrived in Loki through Alloy")
            _check(
                "notes" not in labels,
                "redaction removed the 'notes' attribute",
            )
            _check(
                labels.get("event") == "obs.smoke",
                "ordinary attributes are kept",
            )
            return
        time.sleep(2)
    sys.exit("FAIL  log record did not reach Loki")


def main() -> None:
    """Run every check; exit non-zero on the first failure."""
    _wait("Prometheus", f"{PROMETHEUS}/-/ready")
    _wait("Loki", f"{LOKI}/ready")
    # The OTLP receiver answers GET with 405: listening, wrong method.
    _wait(
        "Alloy",
        f"{ALLOY_OTLP}/v1/logs",
        frozenset({HTTPStatus.METHOD_NOT_ALLOWED}),
    )
    _wait("Grafana", f"{GRAFANA}/api/health")
    check_prometheus()
    check_dashboard_queries()
    check_grafana()
    check_log_pipeline()
    print("observability smoke test passed")


if __name__ == "__main__":
    main()
