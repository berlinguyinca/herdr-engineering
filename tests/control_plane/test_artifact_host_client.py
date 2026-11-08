"""``ArtifactHostClient`` and the ``control_plane.artifact_library`` settings: validation without any network."""
from __future__ import annotations

import pytest
import yaml

from herdr_engineering import cli
from herdr_engineering.control_plane.artifact_host_client import (
    ArtifactHostClient,
    BadReference,
    HostRejected,
    HostUnavailable,
    clean_path,
    csp_sandboxes,
    parse_library_host,
    valid_id,
)
from herdr_engineering.fleet_config import FleetConfigError
from herdr_engineering.fleet_config_cli import validate_config

KEY = "k" * 40


def test_ids_and_paths_are_validated() -> None:
    assert valid_id("artifact_ab-12") == "artifact_ab-12"
    for bad in ("", "..", "a/b", "a b", "-x", "a" * 129, None, 5, "a\x00"):
        with pytest.raises(BadReference):
            valid_id(bad)
    assert clean_path("data/big.bin") == ["data", "big.bin"]
    for bad in ("", "/x", "x/", "a//b", "a/../b", "./a", "a\\b", "a\x00", "x" * 2000, None):
        with pytest.raises(BadReference):
            clean_path(bad)


def test_the_content_url_is_always_host_url_plus_the_content_path() -> None:
    client = ArtifactHostClient("http://host.internal:9000/base/", KEY)
    assert client.content_url("a1", "v1", "d/x y.html") == "http://host.internal:9000/base/v1/content/a1/v1/d/x%20y.html"
    with pytest.raises(BadReference):
        client.content_url("a1", "v1", "../x")
    assert KEY not in repr(client) and KEY not in str(client.__dict__.get("_scheme"))


@pytest.mark.parametrize("url", ["", "ftp://h", "http://", "http://u:p@h", "http://h?x=1", "http://h#f", "h:80"])
def test_bad_host_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        ArtifactHostClient(url, KEY)


@pytest.mark.parametrize("key", ["", "a b", "a\nb", "kéy", None])
def test_bad_keys_are_refused(key) -> None:
    with pytest.raises(ValueError):
        ArtifactHostClient("http://h:1", key)


def test_a_dead_port_is_unavailable_and_unknown_parameters_are_rejected_before_sending() -> None:
    client = ArtifactHostClient("http://127.0.0.1:1", KEY, timeout=1)
    with pytest.raises(HostUnavailable):
        client.feed({})
    with pytest.raises(HostRejected):
        client.feed({"sources_x": "a"})
    with pytest.raises(BadReference):
        client.item("../x")


@pytest.mark.parametrize("policy, ok", [
    ("sandbox; default-src 'none'", True), ("default-src 'none'; sandbox", True), ("SANDBOX", False),
    (None, False), ("", False), ("default-src 'none'", False), ("sandbox allow-same-origin", False),
    ("sandbox; x-sandbox-note", False), ("sandboxed", False),
    ("sandbox allow-scripts; default-src 'none'", False), ("sandbox allow-forms; default-src 'none'", False),
    ("sandbox allow-popups-to-escape-sandbox; default-src 'none'", False), ("sandbox; default-src *", False),
    ("sandbox; default-src 'none' https://x", False), ("sandbox; img-src data:", False),
    ("sandbox; default-src 'none'; img-src data:", True),
])
def test_only_a_fully_sandboxing_policy_counts(policy, ok) -> None:
    assert csp_sandboxes(policy) is ok


def test_settings_parse_and_refuse() -> None:
    assert parse_library_host(None) is None and parse_library_host({}) is None
    settings = parse_library_host({"host_url": "http://h:9", "host_key": KEY, "timeout_seconds": 2})
    assert settings is not None and settings.timeout_seconds == 2.0 and isinstance(settings.client(), ArtifactHostClient)
    for bad in ({"host_url": "http://h"}, {"host_key": KEY}, {"host_url": "http://h", "host_key": "short"},
                {"host_url": "ftp://h", "host_key": KEY}, {"host_url": 5, "host_key": KEY}, {"x": 1}, "nope",
                {"host_url": "http://h", "host_key": KEY, "timeout_seconds": 0},
                {"host_url": "http://h", "host_key": KEY, "timeout_seconds": 999},
                {"host_url": "http://h", "host_key": KEY, "timeout_seconds": True}):
        with pytest.raises(ValueError) as caught:
            parse_library_host(bad)
        assert KEY not in str(caught.value)


def test_cp_web_wiring_builds_the_client_and_stops_on_bad_settings() -> None:
    assert cli._library_wiring({}) is None
    assert cli._library_wiring({"artifact_library": {}}) is None
    with pytest.raises(SystemExit, match="viewers"):                 # the old per-viewer ACL key is gone
        cli._library_wiring({"artifact_library": {"viewers": {}, "host_url": "http://h:9", "host_key": KEY}})
    client = cli._library_wiring({"artifact_library": {"host_url": "http://h:9", "host_key": KEY}})
    assert isinstance(client, ArtifactHostClient)
    with pytest.raises(SystemExit, match="host_key"):
        cli._library_wiring({"artifact_library": {"host_url": "http://h:9", "host_key": "short"}})


def test_fleet_config_validation_checks_the_library_keys(tmp_path) -> None:
    from tests.test_fleet_config_cli import make
    path = make(tmp_path)
    doc = yaml.safe_load(path.read_text())
    values = doc["sections"]["control_plane"]["values"]
    values["artifact_library"] = {"host_url": "http://h:9", "host_key": KEY, "timeout_seconds": 3}
    path.write_text(yaml.safe_dump(doc))
    assert validate_config(path)["hosts"] >= 0
    for bad, match in (({"host_url": "http://h:9"}, "host_key"), ({"host_url": "ftp://h", "host_key": KEY}, "host_url"),
                       ({"host_url": "http://h", "host_key": "short"}, "host_key"),
                       ({"host_url": "http://h", "host_key": KEY, "extra": 1}, "extra")):
        values["artifact_library"] = bad
        path.write_text(yaml.safe_dump(doc))
        with pytest.raises(FleetConfigError, match=match) as caught:
            validate_config(path)
        assert KEY not in str(caught.value)
