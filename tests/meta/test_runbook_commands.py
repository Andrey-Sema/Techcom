"""Every ``followup`` command the runbook names exists in the CLI."""

import re
from typing import Any

import typer

from support import ROOT
from techcom_followup import bootstrap
from techcom_followup.adapters.inbound import cli

RUNBOOK = ROOT / "docs" / "18-runbook.md"
COMMAND = re.compile(r"followup((?: [a-z][a-z-]*)+)")


def command_paths(group: Any, prefix: str = "") -> set[str]:
    # Typer groups expose their subcommands as ``commands``.
    paths = set()
    for name, command in getattr(group, "commands", {}).items():
        path = f"{prefix} {name}".strip()
        paths.add(path)
        paths |= command_paths(command, path)
    return paths


def test_runbook_commands_exist() -> None:
    app = cli.create_cli(bootstrap.RuntimeBuilder())
    known = command_paths(typer.main.get_command(app))
    assert "maintenance prune" in known
    text = RUNBOOK.read_text(encoding="utf-8")
    named = {match.group(1).strip() for match in COMMAND.finditer(text)}
    named = {name for name in named if not name.startswith("followup")}
    assert named
    for name in named:
        words = name.split()
        assert any(
            " ".join(words[:size]) in known
            for size in range(1, len(words) + 1)
        ), name
