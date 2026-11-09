import re
import stat
from pathlib import Path

import pytest
import yaml

from herdr_engineering import settings_catalog as cat
from herdr_engineering.cli import main

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """\
# fleet config
fabric:
  url: https://example.test   # keep
control_plane:
  public_url: https://cp.example.test
  share:
    enabled: false   # off for now
  memory:
    note: x
  # trailing comment
hosts:
  bender: [builder]
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "fleet-config.yaml"
    p.write_text(CONFIG)
    return p


def test_catalog_matches_js():
    js = (ROOT / "packages" / "service-catalog.js").read_text()
    body = js.split("export const SERVICES = [", 1)[1].split("\n];", 1)[0]
    pairs = {m.group(1): m.group(2) for m in re.finditer(r"capability: '([^']+)'.*?configKey: '([^']*)'", body)}
    assert pairs == {s.capability: s.config_key for s in cat.SERVICES}
    always = set(re.findall(r"capability: '([^']+)'[^}]*alwaysOn: true", body))
    assert always == {s.capability for s in cat.SERVICES if s.always_on}


def test_needs_values_are_not_plain_enabled_flags():
    for s in cat.SERVICES:
        if not s.always_on:
            assert s.needs_values == (not s.config_key.endswith(".enabled"))


def test_status_names_only(cfg, capsys):
    assert main(["settings", "status", "--fleet-config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^off +review_gate_v1", out, re.M)
    assert re.search(r"^needs-values +skill_registry_v1", out, re.M)
    assert "example.test" not in out


def test_dry_run_changes_nothing(cfg, capsys):
    assert main(["settings", "enable", "share_v1", "--fleet-config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "+    enabled: true   # off for now" in out and "dry run" in out
    assert cfg.read_text() == CONFIG


def test_write_preserves_comments_backs_up_and_is_idempotent(cfg, capsys):
    assert main(["settings", "enable", "share_v1", "--fleet-config", str(cfg), "--write"]) == 0
    assert cfg.read_text() == CONFIG.replace("enabled: false", "enabled: true")
    backups = list(cfg.parent.glob("fleet-config.yaml.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == CONFIG
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    capsys.readouterr()
    assert main(["settings", "enable", "share_v1", "--fleet-config", str(cfg), "--write"]) == 0
    assert "nothing to change" in capsys.readouterr().out
    assert len(list(cfg.parent.glob("fleet-config.yaml.bak.*"))) == 1


def test_inserts_missing_keys_without_touching_others(cfg):
    assert main(["settings", "enable", "review_gate_v1", "--fleet-config", str(cfg), "--write"]) == 0
    assert main(["settings", "enable", "memory_v1", "--fleet-config", str(cfg), "--write"]) == 0
    data = yaml.safe_load(cfg.read_text())
    assert data["control_plane"]["review_gate"] == {"enabled": True}
    assert data["control_plane"]["memory"] == {"note": "x", "enabled": True}
    expected = yaml.safe_load(CONFIG)
    del data["control_plane"]["review_gate"]
    data["control_plane"]["memory"].pop("enabled")
    assert data == expected
    assert "# trailing comment" in cfg.read_text() and "# fleet config" in cfg.read_text()


def test_all_safe_enables_only_flag_services(cfg):
    assert main(["settings", "enable", "--all-safe", "--fleet-config", str(cfg), "--write"]) == 0
    data = yaml.safe_load(cfg.read_text())
    for s in cat.safe_services():
        node = data
        for part in s.config_key.split("."):
            node = node[part]
        assert node is True
    cp = data["control_plane"]
    for key in ("ci_actions", "skill_registry", "deployment_actions", "deployment_definitions", "artifact_delete"):
        assert key not in cp
    assert "explain" not in cp.get("architecture", {})


def test_needs_values_refused(cfg, capsys):
    assert main(["settings", "enable", "skill_registry_v1", "--fleet-config", str(cfg), "--write"]) == 1
    assert "control_plane.skill_registry" in capsys.readouterr().err
    assert cfg.read_text() == CONFIG


def test_unknown_missing_file_and_args(cfg, tmp_path, capsys):
    assert main(["settings", "enable", "nope", "--fleet-config", str(cfg)]) == 1
    assert main(["settings", "enable", "--fleet-config", str(cfg)]) == 1
    assert main(["settings", "status", "--fleet-config", str(tmp_path / "missing.yaml")]) == 1
    assert main(["settings", "enable", "collab_v1", "--fleet-config", str(cfg)]) == 1


def test_top_level_missing_and_empty_file(tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("")
    assert main(["settings", "enable", "share_v1", "--fleet-config", str(p), "--write"]) == 0
    assert yaml.safe_load(p.read_text()) == {"control_plane": {"share": {"enabled": True}}}
