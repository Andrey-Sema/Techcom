"""``followup maintenance prune`` against PostgreSQL 18."""

import asyncio
import json
from collections.abc import Iterator
from datetime import timedelta

import pytest
import structlog
from pydantic import SecretStr
from typer.testing import CliRunner

from persistence_support import NOW, migrated_engine
from support import ROOT
from techcom_followup import bootstrap
from techcom_followup.adapters.inbound import cli
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import AuditEvent
from techcom_followup.infrastructure.log_config import configure_logging

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def json_logs() -> Iterator[None]:
    # Logs go to stderr; stdout carries only the JSON report.
    previous = structlog.get_config()
    configure_logging()
    yield
    structlog.configure(**previous)


async def seed(url: str) -> None:
    async with migrated_engine(url) as engine:
        repository = PgRepository(engine)
        await repository.append_audit(
            AuditEvent("old.event", NOW - timedelta(days=400), {})
        )
        await repository.append_audit(AuditEvent("new.event", NOW, {}))


def test_prune_applies_the_committed_retention_policy(
    postgres_url: str,
) -> None:
    asyncio.run(seed(postgres_url))
    settings = bootstrap.RuntimeSettings(
        config_dir=ROOT / "config", database_url=SecretStr(postgres_url)
    )
    app = cli.create_cli(bootstrap.RuntimeBuilder(settings))
    result = CliRunner().invoke(app, ["maintenance", "prune"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["complete"] is True
    assert report["deleted"]["audit_events"] == 1
