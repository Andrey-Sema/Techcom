"""Security controls that CI enforces with linters and scanners.

These tests do not re-run the tools; they pin the configuration that
makes the tools a gate, so a control cannot be switched off silently
(for example by an ignore entry or a dropped Makefile prerequisite).
"""

import re
import tomllib
from typing import Any

import pytest
import yaml

from support import ROOT

PYPROJECT: dict[str, Any] = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
CI = yaml.safe_load(
    (ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
)
# Ruff/Bandit rules behind ASVS controls: SQL built from strings, OS
# commands, eval/exec, unsafe deserialization, weak hashes, TLS off.
SECURITY_RULES = {
    "S102", "S301", "S307", "S324", "S501", "S506",
    "S602", "S603", "S604", "S605", "S606", "S607", "S608",
}  # fmt: skip


def make_target(name: str) -> str:
    match = re.search(
        rf"^{name}:.*?(?=^\S|\Z)", MAKEFILE, re.MULTILINE | re.DOTALL
    )
    assert match, name
    return match.group(0)


def ci_runs(job: str) -> list[str]:
    return [step["run"] for step in CI["jobs"][job]["steps"] if "run" in step]


@pytest.mark.asvs("1.2.4", "1.2.5", "1.3.2", "1.5.2", "11.4.1", "12.3.2")
def test_ruff_security_rules_apply_to_product_code() -> None:
    lint = PYPROJECT["tool"]["ruff"]["lint"]
    assert lint["select"] == ["ALL"]
    assert not SECURITY_RULES & set(lint["ignore"])
    for pattern, codes in lint["per-file-ignores"].items():
        if SECURITY_RULES & set(codes):
            assert pattern.startswith("tests/"), pattern
    assert "ruff check" in make_target("quality")
    assert "make quality" in ci_runs("quality")


@pytest.mark.asvs("1.2.5", "1.3.2")
def test_bandit_scans_product_code_without_skips() -> None:
    assert "skips" not in PYPROJECT["tool"]["bandit"]
    assert "bandit -c pyproject.toml -r src mocks/src" in make_target(
        "quality"
    )


@pytest.mark.asvs("1.2.4", "12.3.2", "15.3.2")
def test_semgrep_invariants_are_tested_and_gated() -> None:
    rules = yaml.safe_load(
        (ROOT / ".semgrep" / "project-invariants.yml").read_text(
            encoding="utf-8"
        )
    )
    ids = {rule["id"] for rule in rules["rules"]}
    assert {
        "no-raw-sql-interpolation",
        "no-tls-verify-false",
        "no-follow-redirects",
    } <= ids
    gates = make_target("gates")
    assert "semgrep-test" in gates
    assert "semgrep " in gates
    assert "make gates" in ci_runs("gates")


@pytest.mark.asvs("12.3.2")
def test_no_product_code_disables_certificate_checks() -> None:
    disabling = re.compile(r"verify\s*=\s*False|CERT_NONE|_create_unverified")
    for path in [*ROOT.glob("src/**/*.py"), *ROOT.glob("mocks/src/**/*.py")]:
        assert not disabling.search(path.read_text(encoding="utf-8")), path


@pytest.mark.asvs("15.2.1")
def test_dependency_and_image_scans_fail_the_build() -> None:
    assert "audit" in make_target("gates")
    assert "pip-audit --strict --require-hashes" in make_target("audit")
    assert "--exit-code 1 --severity HIGH,CRITICAL" in MAKEFILE
    assert "make image-scan" in ci_runs("image")


@pytest.mark.asvs("15.2.1")
def test_scanners_are_pinned_by_digest() -> None:
    # Trivy releases 0.69.4-0.69.6 were compromised (docs/15 s. 10b).
    for name in ("TRIVY_IMAGE", "HADOLINT_IMAGE", "GITLEAKS_IMAGE"):
        match = re.search(rf"^{name} = (\S+)$", MAKEFILE, re.MULTILINE)
        assert match, name
        assert re.search(r"@sha256:[0-9a-f]{64}$", match.group(1)), name


@pytest.mark.asvs("15.1.2")
def test_sboms_are_built_and_kept_as_artefacts() -> None:
    assert "--format cyclonedx1.5" in make_target("sbom")
    assert "--format cyclonedx" in make_target("image-scan")
    assert "make sbom" in ci_runs("gates")
    uploads = [
        step["with"]["path"]
        for job in CI["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert uploads == [
        "var/sbom/techcom-followup.cdx.json",
        "var/bench/results.json",
        "var/sbom/runtime-image.cdx.json",
    ]


def test_secret_scan_covers_the_whole_history() -> None:
    checkout = CI["jobs"]["secrets"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
    assert "make secrets-scan" in ci_runs("secrets")
    assert "--exit-code 1" in make_target("secrets-scan")
    assert "--staged" in make_target("secrets-staged")
