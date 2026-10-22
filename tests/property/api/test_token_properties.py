from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api_support import NOW, Harness, seed_run
from techcom_followup.adapters.inbound.api.security import AuthenticationError

if TYPE_CHECKING:
    from pydantic import JsonValue


@pytest.mark.asvs("9.1.1", "9.1.2")
@given(st.text(max_size=10000))
@settings(max_examples=150)
def test_arbitrary_text_cannot_authenticate_or_crash(token: str) -> None:
    verifier = Harness().verifier
    with pytest.raises(AuthenticationError):
        verifier.verify(token, NOW)


@pytest.mark.asvs("8.2.2")
@given(st.sets(st.sampled_from(["A", "B", "C"]), min_size=1))
@settings(max_examples=15)
async def test_project_membership_only_filters_server_relationships(
    projects: set[str],
) -> None:
    harness = Harness()
    await seed_run(harness, ("A", "B", "C", None))
    memberships: list[JsonValue] = []
    memberships.extend(sorted(projects))
    token = harness.token(sub="scoped", projects=memberships)
    async with harness.client(token) as client:
        response = await client.get("/v1/actions")
    assert response.status_code == 200
    observed = {row["target"]["project"] for row in response.json()}
    assert observed == projects
