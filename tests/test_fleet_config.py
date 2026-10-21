"""Central fleet config: validation, revision, per-role scoping."""

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

SECRET = "sk-super-secret-value"


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def base() -> dict:
    return {
        "version": 1,
        "sections": {
            "common": {"roles": ["all"], "values": {"region": "us"}},
            "llm": {"roles": ["builder"], "values": {"upstream_key": SECRET, "upstream_url": "http://x"}},
            "ci": {"roles": ["ci"], "values": {"token": "ci-token-value"}},
            "control_plane": {"roles": ["control-plane"], "values": {"pg_dsn": "postgres://u:pw@h/db"}},
        },
        "hosts": {
            "bender": {"roles": ["builder"], "token_sha256": sha("tok-bender")},
            "bare": {"roles": [], "token_sha256": sha("tok-bare")},
            "beast": {
                "roles": ["builder", "ci"],
                "token_sha256": sha("tok-beast"),
                "overrides": {"common": {"region": "eu"}},
            },
        },
    }


def write(tmp_path: Path, data, mode=0o600, text: str | None = None) -> Path:
    p = tmp_path / "fleet-config.yaml"
    p.write_text(text if text is not None else yaml.safe_dump(data))
    p.chmod(mode)
    return p


def test_slice_contains_only_allowed_sections(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    s = cfg.slice_for("bender")
    assert set(s) == {"common", "llm"}
    assert s["llm"]["upstream_key"] == SECRET
    assert "ci" not in s and "control_plane" not in s


def test_empty_roles_host_gets_only_all_sections(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert set(cfg.slice_for("bare")) == {"common"}


def test_overrides_win(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert cfg.slice_for("beast")["common"]["region"] == "eu"
    assert cfg.slice_for("bender")["common"]["region"] == "us"


def test_control_plane_section_never_in_a_host_slice(tmp_path):
    d = base()
    d["sections"]["control_plane"]["roles"] = ["builder"]
    cfg = load_fleet_config(write(tmp_path, d))
    assert "control_plane" not in cfg.slice_for("bender")
    assert cfg.section("control_plane")["pg_dsn"].startswith("postgres://")


def test_control_plane_section_cannot_be_all(tmp_path):
    d = base()
    d["sections"]["control_plane"]["roles"] = ["all"]
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, d))


def test_section_returns_values_regardless_of_roles(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert cfg.section("ci") == {"token": "ci-token-value"}
    with pytest.raises(FleetConfigError):
        cfg.section("nope")


def test_duplicate_token_hash_rejected(tmp_path):
    d = base()
    d["hosts"]["bare"]["token_sha256"] = d["hosts"]["bender"]["token_sha256"]
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, d))


def test_group_readable_file_rejected(tmp_path):
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, base(), mode=0o640))
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, base(), mode=0o604))


def test_revision_changes_with_content_not_comments(tmp_path):
    text = yaml.safe_dump(base())
    a = load_fleet_config(write(tmp_path, None, text=text)).revision
    b = load_fleet_config(write(tmp_path, None, text="# a comment\n" + text)).revision
    assert a == b and len(a) == 12
    d = base()
    d["sections"]["common"]["values"]["region"] = "ap"
    assert load_fleet_config(write(tmp_path, d)).revision != a


def test_host_for_token_unknown_returns_none(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert cfg.host_for_token("tok-beast") == "beast"
    assert cfg.host_for_token("nope") is None
    assert cfg.host_for_token("") is None


def test_status_has_no_values(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    out = json.dumps(cfg.status())
    assert SECRET not in out and "ci-token-value" not in out and sha("tok-bender") not in out
    st = cfg.status()
    assert st["revision"] == cfg.revision
    assert st["hosts"]["beast"] == ["builder", "ci"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(version=2),
    lambda d: d.pop("sections"),
    lambda d: d["hosts"]["bender"].update(token_sha256="short"),
    lambda d: d["hosts"]["bender"].update(roles="builder"),
    lambda d: d["sections"]["llm"].pop("values"),
])
def test_invalid_shapes_rejected(tmp_path, mutate):
    d = base()
    mutate(d)
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, d))


def test_errors_do_not_leak_values(tmp_path):
    p = write(tmp_path, None, text=f"version: 1\nsections: [{SECRET}\n")
    with pytest.raises(FleetConfigError) as e:
        load_fleet_config(p)
    assert SECRET not in str(e.value)


def test_missing_file(tmp_path):
    with pytest.raises(FleetConfigError):
        load_fleet_config(tmp_path / "absent.yaml")


def test_non_json_date_value_rejected_without_leak(tmp_path):
    text = yaml.safe_dump(base()) + ""
    text = text.replace("region: us", "region: us\n      since: 2026-10-21")
    assert "since: 2026-10-21" in text
    with pytest.raises(FleetConfigError) as e:
        load_fleet_config(write(tmp_path, None, text=text))
    assert "2026" not in str(e.value) and "JSON" in str(e.value)


def test_mixed_key_types_rejected_without_leak(tmp_path):
    d = base()
    d["sections"]["common"]["values"]["m"] = {1: "a", "b": SECRET}
    with pytest.raises(FleetConfigError) as e:
        load_fleet_config(write(tmp_path, d))
    assert SECRET not in str(e.value)
