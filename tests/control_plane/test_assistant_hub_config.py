"""``personal_assistant.hub`` config normalization."""
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant import assistant_config


def _hub(hub: Any = None, **section: Any) -> dict[str, Any]:
    pa: dict[str, Any] = dict(section)
    if hub is not None:
        pa["hub"] = hub
    return assistant_config({"personal_assistant": pa})["hub"]


def test_hub_defaults_off() -> None:
    assert _hub()["enabled"] is False
    assert assistant_config({})["hub"]["enabled"] is False


def test_hub_defaults_values() -> None:
    hub = _hub()
    assert hub == {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 9483,
        "allowed_networks": ["127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"],
        "allowed_hosts": [],
        "enrollment_ttl_seconds": 900,
        "worker_ttl_days": 90,
        "tenant": "default",
    }


def test_hub_tenant_defaults_to_default_tenant() -> None:
    assert _hub(tenancy={"default_tenant": "acme"})["tenant"] == "acme"


def test_hub_overrides() -> None:
    hub = _hub({"enabled": True, "port": 1234, "allowed_networks": ["10.0.0.0/8"], "allowed_hosts": ["a.ts.net"],
                "enrollment_ttl_seconds": 60, "worker_ttl_days": 7, "host": "0.0.0.0", "tenant": "t1"})
    assert hub["enabled"] is True and hub["port"] == 1234 and hub["allowed_networks"] == ["10.0.0.0/8"]
    assert hub["allowed_hosts"] == ["a.ts.net"] and hub["tenant"] == "t1" and hub["host"] == "0.0.0.0"


def test_hub_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        _hub({"bogus": 1})


def test_hub_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        _hub(["x"])


@pytest.mark.parametrize("port", [0, 70000, "9483", True])
def test_hub_rejects_bad_port(port: Any) -> None:
    with pytest.raises(ValueError, match="port"):
        _hub({"port": port})


@pytest.mark.parametrize("nets", ["not-a-cidr", ["not-a-cidr"], ["10.0.0.1/8"], "10.0.0.0/8", [1]])
def test_hub_rejects_bad_network(nets: Any) -> None:
    with pytest.raises(ValueError, match="allowed_networks"):
        _hub({"allowed_networks": nets})


@pytest.mark.parametrize("value", ["true", 1, None])
def test_hub_enabled_must_be_bool(value: Any) -> None:
    with pytest.raises(ValueError, match="enabled"):
        _hub({"enabled": value})


def test_hub_tenant_validated_with_existing_tenant_rule() -> None:
    with pytest.raises(ValueError, match="tenant id"):
        _hub({"tenant": "bad tenant!"})


@pytest.mark.parametrize("key", ["enrollment_ttl_seconds", "worker_ttl_days"])
@pytest.mark.parametrize("value", [0, -1, "5", True])
def test_hub_rejects_bad_ttls(key: str, value: Any) -> None:
    with pytest.raises(ValueError, match=key):
        _hub({key: value})


def test_hub_rejects_bad_allowed_hosts() -> None:
    with pytest.raises(ValueError, match="allowed_hosts"):
        _hub({"allowed_hosts": [""]})
