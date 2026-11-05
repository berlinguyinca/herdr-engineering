"""Explorer policy and the existing optional backend share one memory block."""
import pytest

from herdr_engineering.fleet_config import FleetConfigError
from herdr_engineering.fleet_memory import parse_memory_section


def test_existing_memory_schema_preserves_explorer_fields():
    config = {
        "enabled": False,
        "viewers": {"key:aaaaaaaaaaaa": {"projects": ["canary"], "scopes": ["global"]}},
        "command": ["herdr-memory"],
        "client_config_source": "/private/client.json",
    }
    assert parse_memory_section(config) == config


@pytest.mark.parametrize("changes", [
    {"viewers": []},
    {"viewers": {"owner": {"projects": "bad", "scopes": []}}},
    {"viewers": {"owner": {"projects": ["canary"], "scopes": ["personal"]}}},
    {"command": "secret-credential-value"},
    {"command": []},
    {"client_config_source": "secret-credential-value"},
    {"client_config_source": "/private/\ninvalid.json"},
])
def test_invalid_explorer_configuration_is_rejected_without_values(changes):
    with pytest.raises(FleetConfigError) as error:
        parse_memory_section({"enabled": False, **changes})
    assert "secret-credential-value" not in str(error.value)
