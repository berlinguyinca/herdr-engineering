"""Powerpack lifecycle tests (spec 0050)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.config import load_config
from herdr_engineering.powerpack import PowerpackManager


def test_enable_disable_policy(tmp_path):
    pp = PowerpackManager(state_dir=tmp_path, lock_path=tmp_path / "lock.yaml")
    pp.enable("herdr-browser", reason="review")
    assert "herdr-browser" in pp.enabled_plugins()
    assert pp.disable("herdr-browser") is True
    assert pp.disable("herdr-browser") is False


def test_snapshot_and_rollback(tmp_path):
    lock = tmp_path / "lock.yaml"
    lock.write_text("version: 1\ndependencies: {}\n")
    pp = PowerpackManager(state_dir=tmp_path / "state", lock_path=lock)
    snap = pp.snapshot("known-good")
    # mutate lock
    lock.write_text("version: 1\ndependencies: {broken: true}\n")
    pp.rollback(snap)
    assert "broken" not in lock.read_text()


def test_snapshot_and_rollback_restores_plugin_policy(tmp_path):
    """enable/disable state must survive the up/down drill (the 'down' half)."""
    pp = PowerpackManager(state_dir=tmp_path / "state",
                          lock_path=tmp_path / "lock.yaml")
    pp.snapshot("clean")          # nothing enabled yet
    pp.enable("plannotator", reason="drill")
    assert pp.enabled_plugins() == ["plannotator"]
    pp.rollback("clean")
    assert pp.enabled_plugins() == []


def test_rollback_accepts_str_path_and_tag(tmp_path):
    lock = tmp_path / "lock.yaml"
    lock.write_text("version: 1\ndependencies: {good: true}\n")
    pp = PowerpackManager(state_dir=tmp_path / "state", lock_path=lock)
    snap = pp.snapshot("drill")
    lock.write_text("version: 1\ndependencies: {broken: true}\n")
    # CLI passes --snapshot as a string (possibly with ~): must not crash
    pp.rollback(str(snap))
    assert "broken" not in lock.read_text()
    # tag form: most recent snapshot-drill-*
    lock.write_text("version: 1\ndependencies: {broken: true}\n")
    pp.rollback("drill")
    assert "broken" not in lock.read_text()
    from herdr_engineering.errors import InvalidError
    try:
        pp.rollback("no-such-tag")
        assert False, "expected InvalidError"
    except InvalidError:
        pass


def test_update_preflight_blocks_incompatible(tmp_path):
    lock = tmp_path / "lock.yaml"
    lock.write_text(
        "version: 1\ndependencies:\n  bad-plugin:\n    decision: REJECT\n")
    pp = PowerpackManager(state_dir=tmp_path / "state", lock_path=lock)
    res = pp.update_preflight()
    assert res.ok is False
    assert any("bad-plugin" in i for i in res.incompatible)


def test_config_load_and_merge(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("version: 1\nweb:\n  port: 9999\n")
    cfg = load_config([cfg_file])
    assert cfg["web"]["port"] == 9999
    # deep merge preserves unrelated defaults
    assert cfg["dev_fabric"]["enabled"] is True
