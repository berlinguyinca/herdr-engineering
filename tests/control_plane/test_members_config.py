"""control_plane.collab.members: one parser, one limits type (Task 11)."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.artifact_member_service import MembersConfig as ServiceConfig
from herdr_engineering.control_plane.members_config import (
    MembersConfig,
    parse_members,
    valid_public_host,
)

SECRET = "s" * 40
HOST = "artifacts.example.org"


def section(**over):
    return {"enabled": True, "group": "staff", "session_secret": SECRET, **over}


def test_there_is_one_members_config_type():
    assert ServiceConfig is MembersConfig


def test_disabled_or_absent_is_off():
    assert parse_members(None) is None
    assert parse_members({"enabled": False, "group": "g"}) is None
    assert parse_members({"group": "g"}) is None
    assert parse_members({"enabled": False}) is None


@pytest.mark.parametrize("bad", [{"group": 5}, {"nonsense": 1}, {"session_secret": "short"}, {"public_host": "A.b.c"},
                                 {"groups_ttl": -1}, {"share_creates_per_hour": 0}, {"session_seconds": 5},
                                 {"request_limits": {"http_chunks_per_request": 33}}, {"request_limits": []}])
def test_a_disabled_section_is_validated_too(bad):
    with pytest.raises(ValueError):
        parse_members({"enabled": False, **bad})


def test_defaults_and_limits_object():
    got = parse_members(section(), public_host=HOST)
    assert got["session_seconds"] == 14400
    assert parse_members(section(), public_host=HOST, session_seconds=900)["session_seconds"] == 900
    assert got["limits"] == MembersConfig()
    assert got["public_host"] == HOST and got["session_secret"] == SECRET
    assert got["share_limits"] == {} and got["share_creates_per_hour"] == 20


def test_limit_sections_flow_into_the_limits_object():
    got = parse_members(section(request_limits={"http_chunks_per_request": 4, "result_wait_seconds": 2},
                                rate_limits={"writes_per_minute": 7, "session_exchanges_per_minute": 3},
                                staging={"staging_bytes_per_member": 99}, share_creates_per_hour=5), public_host=HOST)
    lim = got["limits"]
    assert (lim.http_chunks_per_request, lim.result_wait_seconds, lim.writes_per_minute,
            lim.session_exchanges_per_minute, lim.staging_bytes_per_member, lim.share_creates_per_hour) == (
        4, 2, 7, 3, 99, 5)


def test_null_share_limits_accepted():
    got = parse_members(section(share_limits={"max_share_bytes": None, "max_total_bytes": None}), public_host=HOST)
    assert got["share_limits"] == {"max_share_bytes": None, "max_total_bytes": None}


def test_null_upload_and_staging_limits_are_accepted_but_other_limits_still_refuse_null():
    got = parse_members(section(request_limits={"max_upload_bytes": None}, staging={"staging_bytes_per_member": None}),
                        public_host=HOST)
    assert got["limits"].max_upload_bytes is None and got["limits"].staging_bytes_per_member is None
    for bad in ({"request_limits": {"max_publish_files": None}}, {"rate_limits": {"writes_per_minute": None}},
                {"request_limits": {"max_upload_bytes": (1 << 40) + 1}}):
        with pytest.raises(ValueError):
            parse_members(section(**bad), public_host=HOST)


@pytest.mark.parametrize("bad", [
    {"session_secret": None}, {"session_secret": "short"}, {"session_secret": 5}, {"session_secret": "é" * 40},
    {"nope": 1}, {"group": ""}, {"enabled": "yes"},
    {"share_creates_per_hour": 0}, {"share_creates_per_hour": True},
    {"share_limits": {"max_share_bytes": 0}}, {"share_limits": []}, {"share_limits": {"x": 1}},
    {"request_limits": {"http_chunks_per_request": 0}}, {"request_limits": {"http_chunks_per_request": True}},
    {"request_limits": {"bogus": 1}}, {"request_limits": []}, {"request_limits": {"result_wait_seconds": 21}},
    {"rate_limits": {"writes_per_minute": 0}}, {"rate_limits": {"x": 1}}, {"staging": {"staging_bytes_per_member": 0}},
    {"max_artifacts": 0}, {"groups_ttl": 0}, {"removed_grace": -1},
])
def test_invalid_sections_are_refused_naming_the_key(bad):
    with pytest.raises(ValueError, match="control_plane.collab.members|member limit|must be"):
        parse_members(section(**bad), public_host=HOST)


@pytest.mark.parametrize("host", [None, "", "Artifacts.example.org", "https://a.example.org", "a.example.org:8443",
                                  "a.example.org/x", "a.example.org.", "localhost", "a b.example.org", "é.example.org",
                                  "user@a.example.org", "-a.example.org"])
def test_bad_host_forms_are_refused_when_enabled(host):
    with pytest.raises(ValueError, match="public_host"):
        parse_members(section(), public_host=host)
    assert valid_public_host(host) is False


def test_missing_secret_is_refused_when_enabled():
    raw = section()
    del raw["session_secret"]
    with pytest.raises(ValueError, match="session_secret"):
        parse_members(raw, public_host=HOST)


def test_slice_form_carries_public_host_inside_the_section():
    assert parse_members(section(public_host=HOST))["public_host"] == HOST


# --- the shipped example (Task 14) -------------------------------------------------------------------------------

def _uncommented_example(tmp_path):
    """The example fleet config with its commented member blocks switched on (real YAML, real loader)."""
    import yaml

    from herdr_engineering.fleet_config import load_fleet_config
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "deploy" / "control-plane" / "fleet-config.example.yaml"
    lines, out, mode = src.read_text().splitlines(), [], None
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("# @members-begin"):
            mode = "members"
            continue
        if stripped.startswith("# @members-end"):
            mode = None
            continue
        if mode == "members":
            indent = line[: len(line) - len(stripped)]
            out.append(indent + stripped[2:] if stripped.startswith("# ") else line)
            continue
        out.append(line)
    text = "\n".join(out) + "\n"
    data = yaml.safe_load(text)
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({**data, "hosts": {}}))      # the example's placeholder hosts are not loadable
    path.chmod(0o600)
    return data["sections"]["control_plane"]["values"], load_fleet_config(path)


def test_example_members_block_loads_through_members_config_and_share_values(tmp_path):
    from herdr_engineering import fleet_config_cli as fcc
    values, cfg = _uncommented_example(tmp_path)
    raw = values["collab"]["members"]
    assert raw["enabled"] is False
    assert raw["max_artifact_bytes"] is None and raw["max_member_bytes"] is None and raw["max_artifacts"] is None
    assert raw["share_limits"] == {"max_share_bytes": None, "max_total_bytes": None}
    assert parse_members(raw, public_host=values["share"]["public_host"]) is None           # disabled: validated, off
    on = parse_members({**raw, "enabled": True, "session_secret": SECRET}, public_host=values["share"]["public_host"])
    assert on is not None and on["limits"] == MembersConfig()                    # the example states the parsed defaults
    assert on["groups_ttl"] == 86400.0 and on["removed_grace"] == 3600.0 and on["share_creates_per_hour"] == 20
    assert "session_secret" not in raw                                      # the example never carries a secret value
    fcc.share_values(cfg)                                                                   # the deploy parser accepts it
