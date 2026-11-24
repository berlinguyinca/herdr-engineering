"""The deploy-time check (``fleet_config_cli.artifact_host_slices``) and the host's own load (``host_config.parse``) must
refuse the same Cognito values, so a bad one fails the deploy up front instead of rolling back after the host refuses
to start.  The rule is one function, kept as two byte-identical copies (the host image does not ship herdr_engineering)."""

from __future__ import annotations

import copy
import secrets
from pathlib import Path

import pytest

from herdr_artifact_host import cognito_values as host_side
from herdr_artifact_host import host_config
from herdr_engineering import cognito_values as deploy_side
from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config
from tests.test_share_deploy_config import COLLAB, _central

ROOT = Path(__file__).resolve().parents[1]
SECTION = {
    "enabled": True,
    "public_host": "artifacts.example.test",
    "mcp_public_hosts": ["mcp.example.test"],
    "session_secret": secrets.token_hex(32),
    "claim_key": secrets.token_hex(32),
    "db_password": secrets.token_hex(16),
}
EXTRA = {
    "artifact_host": SECTION,
    "artifact_library": {"host_url": "http://artifact-host-relay:8091", "host_key": "k" * 40},
}
# (collab key in fleet-config.yaml, bad value, key in the host's cognito mapping)
BAD = [
    ("cognito_domain", "pool.auth.example.test", "domain"),
    ("cognito_domain", "https://pool.auth.example.test/path", "domain"),
    ("cognito_domain", "http://pool.auth.example.test", "domain"),
    ("cognito_pool", "nopool", "pool_id"),
    ("cognito_web_client", "bad client!", "web_client_id"),
    ("cognito_clients", ["ok", "bad id!"], "client_ids"),
    ("cognito_clients", ["ok", 7], "client_ids"),
]


def slices(tmp_path: Path, **collab: object) -> dict[str, dict]:
    cfg = load_fleet_config(_central(tmp_path, collab={**COLLAB, **collab}, extra=EXTRA))
    result = fcc.artifact_host_slices(cfg)
    assert result is not None
    return result


def test_a_valid_config_passes_both_checks(tmp_path: Path) -> None:
    host_config.parse(slices(tmp_path)["public"])


@pytest.mark.parametrize(("collab_key", "bad", "host_key"), BAD)
def test_deploy_check_and_host_load_refuse_the_same_values(
    tmp_path: Path, collab_key: str, bad: object, host_key: str
) -> None:
    with pytest.raises(FleetConfigError, match=collab_key):
        slices(tmp_path / "bad", **{collab_key: bad})
    good = slices(tmp_path / "good")["public"]
    mutated = copy.deepcopy(good)
    mutated["cognito"][host_key] = bad
    with pytest.raises(ValueError, match=f"cognito.{host_key}"):
        host_config.parse(mutated)


def test_the_two_copies_of_the_rule_are_identical() -> None:
    assert Path(host_side.__file__).read_bytes() == Path(deploy_side.__file__).read_bytes()
