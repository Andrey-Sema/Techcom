"""Load the decision policy and routing table from TOML files.

Both files are validated at startup; a typo fails fast instead of
silently changing who gets messaged (AC-F7).
"""

import tomllib
from pathlib import Path

from techcom_followup.application.retention import RetentionPolicy
from techcom_followup.domain.policy import (
    DecisionPolicy,
    PolicyConfig,
    RoutingTable,
)


def load_policy_config(path: Path) -> PolicyConfig:
    """Read and validate ``config/policy.toml``.

    Raises:
        OSError: If the file cannot be read.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        pydantic.ValidationError: If the content violates the schema.
    """
    with path.open("rb") as handle:
        return PolicyConfig.model_validate(tomllib.load(handle))


def load_routing_table(path: Path) -> RoutingTable:
    """Read and validate ``config/routing.toml``.

    Raises:
        OSError: If the file cannot be read.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        pydantic.ValidationError: If the content violates the schema.
    """
    with path.open("rb") as handle:
        return RoutingTable.model_validate(tomllib.load(handle))


def load_decision_policy(config_dir: Path) -> DecisionPolicy:
    """Build the policy from ``policy.toml`` and ``routing.toml``."""
    return DecisionPolicy(
        load_policy_config(config_dir / "policy.toml"),
        load_routing_table(config_dir / "routing.toml"),
    )


def load_retention_policy(path: Path) -> RetentionPolicy:
    """Read and validate ``config/retention.toml``.

    Raises:
        OSError: If the file cannot be read.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        pydantic.ValidationError: If the content violates the schema.
    """
    with path.open("rb") as handle:
        return RetentionPolicy.model_validate(tomllib.load(handle))
