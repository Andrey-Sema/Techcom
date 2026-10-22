from collections.abc import AsyncIterator

import pytest

from persistence_support import migrated_engine, postgres_url
from techcom_followup.adapters.outbound.in_memory_repository import (
    InMemoryRepository,
)
from techcom_followup.adapters.outbound.pg_repository import PgRepository
from techcom_followup.application.repository import Repository

__all__ = ["postgres_url"]


@pytest.fixture(
    params=["memory", pytest.param("postgres", marks=pytest.mark.integration)]
)
async def repository(
    request: pytest.FixtureRequest,
) -> AsyncIterator[Repository]:
    if request.param == "memory":
        yield InMemoryRepository()
    else:
        url: str = request.getfixturevalue("postgres_url")
        async with migrated_engine(url) as engine:
            yield PgRepository(engine)
