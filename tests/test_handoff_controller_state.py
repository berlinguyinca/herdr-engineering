"""Is an automatic handoff policy in place on this host: the controller plugin linked in Herdr, a controller running, the
side pane linked, the global switch. Pure mapping tests, real files and locks, and `herdr_server` tests that link the
real manifests into a real isolated Herdr server (no mocks)."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from herdr_engineering import handoff_controller_state as st
from herdr_engineering.handoff_controller.run import ControllerLock
from herdr_engineering.handoff_controller_state import ControllerState as C
from herdr_engineering.handoff_controller_state import SidePaneState as S
from tests.handoff_controller_herdr import isolated_herdr

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
herdr_server = pytest.mark.herdr_server


@pytest.mark.parametrize("linked,running,off,expected", [
    (True, True, False, C.RUNNING),
    (None, True, False, C.RUNNING),      # a running controller needs no answer from Herdr
    (False, True, False, C.RUNNING),
    (True, False, False, C.LINKED),
    (False, False, False, C.NOT_LINKED),
    (None, False, False, C.HERDR_UNAVAILABLE),
    (False, False, True, C.SWITCHED_OFF),
    (None, False, True, C.SWITCHED_OFF),
    (True, True, True, C.SWITCHED_OFF),   # deliberately off wins
])
def test_controller_state_mapping(linked, running, off, expected) -> None:
    assert st.classify_controller(linked=linked, running=running, switched_off=off) is expected


def test_side_pane_state_mapping() -> None:
    assert st.classify_side_pane(True) is S.LINKED
    assert st.classify_side_pane(False) is S.NOT_LINKED
    assert st.classify_side_pane(None) is S.HERDR_UNAVAILABLE


def test_only_a_missing_controller_blocks_and_each_state_carries_a_fix() -> None:
    blocking = {c for c in C if st.describe_controller(c, DEPLOY).blocking}
    assert blocking == {C.NOT_LINKED, C.HERDR_UNAVAILABLE}
    for c in (C.NOT_LINKED, C.LINKED, C.HERDR_UNAVAILABLE):
        assert st.describe_controller(c, DEPLOY).fix
    assert "herdr-eng handoff controller link" in st.describe_controller(C.NOT_LINKED, DEPLOY).fix
    assert "herdr plugin link" in st.describe_controller(C.NOT_LINKED, DEPLOY).fix
    assert "herdr-eng handoff controller run" in st.describe_controller(C.NOT_LINKED, DEPLOY).fix
    assert "restart Herdr" in st.describe_controller(C.LINKED, DEPLOY).fix
    assert not any(st.describe_side_pane(s, DEPLOY).blocking for s in S)


def test_rendered_manifests_use_the_absolute_herdr_eng_and_stay_valid() -> None:
    exe = "/opt/with space/bin/herdr-eng"
    for name in ("herdr-handoff-controller-plugin", "herdr-session-pane-plugin"):
        original = (DEPLOY / name / "herdr-plugin.toml").read_text()
        rendered = st.render_manifest(original, exe)
        before, after = tomllib.loads(original), tomllib.loads(rendered)
        assert after["id"] == before["id"]
        commands = [e["command"] for kind in ("startup", "panes", "actions") for e in after.get(kind, [])]
        assert commands and not any(c[0] == "herdr-eng" for c in commands)
        assert any(c[0] == exe for c in commands) or any(exe in " ".join(c) for c in commands)
        assert "herdr-eng" not in " ".join(" ".join(c).replace(exe, "") for c in commands)


def test_herdr_version_parse() -> None:
    assert st.parse_version("herdr 0.9.1") == (0, 9, 1)
    assert st.parse_version("herdr 0.10.0-rc1") == (0, 10, 0)
    assert st.parse_version("nonsense") is None
    assert st.parse_version("herdr 0.9.0") < st.MIN_HERDR


def test_a_held_lock_means_running_without_asking_herdr(tmp_path: Path) -> None:
    env = {"HERDR_ENGINEERING_STATE": str(tmp_path / "state"), "HOME": str(tmp_path)}
    lock = ControllerLock(tmp_path / "state")
    assert lock.acquire()
    try:
        controller, _ = st.controller_findings(env, herdr_bin=str(tmp_path / "no-such-herdr"), deploy_dir=DEPLOY)
        assert controller.state is C.RUNNING and not controller.blocking
    finally:
        lock.release()
    controller, side = st.controller_findings(env, herdr_bin=str(tmp_path / "no-such-herdr"), deploy_dir=DEPLOY)
    assert controller.state is C.HERDR_UNAVAILABLE and controller.blocking
    assert side.state is S.HERDR_UNAVAILABLE


def test_the_global_switch_is_a_deliberate_off(tmp_path: Path) -> None:
    env = {"HERDR_ENGINEERING_STATE": str(tmp_path / "state"), "HOME": str(tmp_path), "HERDR_HANDOFF_AUTO": "0"}
    controller, _ = st.controller_findings(env, herdr_bin=str(tmp_path / "no-such-herdr"), deploy_dir=DEPLOY)
    assert controller.state is C.SWITCHED_OFF and not controller.blocking


@herdr_server
def test_linking_both_plugins_into_a_real_server_is_idempotent_and_reported(tmp_path: Path) -> None:
    with isolated_herdr("ctlstate") as h:
        env = {**h.env, "HERDR_ENGINEERING_STATE": str(tmp_path / "state")}
        before = st.controller_findings(env, herdr_bin="herdr", deploy_dir=DEPLOY, cwd=h.root)
        assert before[0].state is C.NOT_LINKED and before[0].blocking and before[1].state is S.NOT_LINKED

        data = tmp_path / "plugins"
        first = st.link_plugins(DEPLOY, "/abs/herdr-eng", data_dir=data, herdr_bin="herdr", env=env, cwd=h.root)
        assert [i.action for i in first] == ["installed", "installed"], first
        controller, side = st.controller_findings(env, herdr_bin="herdr", deploy_dir=DEPLOY, cwd=h.root)
        assert controller.state is C.LINKED and not controller.blocking and side.state is S.LINKED
        assert "/abs/herdr-eng" in (data / "herdr-handoff-controller-plugin" / "herdr-plugin.toml").read_text()

        second = st.link_plugins(DEPLOY, "/abs/herdr-eng", data_dir=data, herdr_bin="herdr", env=env, cwd=h.root)
        assert [i.action for i in second] == ["unchanged", "unchanged"], second

        moved = st.link_plugins(DEPLOY, "/abs/herdr-eng", data_dir=tmp_path / "plugins2", herdr_bin="herdr", env=env, cwd=h.root)
        assert [i.action for i in moved] == ["installed", "installed"], moved  # relinked to the new release path
        roots = st.PluginHost("herdr", env, h.root).plugin_roots()
        assert roots["herdr.handoff-controller"] == str((tmp_path / "plugins2" / "herdr-handoff-controller-plugin").resolve())

        lock = ControllerLock(tmp_path / "state")
        assert lock.acquire()
        try:
            assert st.controller_findings(env, herdr_bin="herdr", deploy_dir=DEPLOY, cwd=h.root)[0].state is C.RUNNING
        finally:
            lock.release()


def test_linking_is_skipped_when_herdr_is_absent(tmp_path: Path) -> None:
    items = st.link_plugins(DEPLOY, "/abs/herdr-eng", data_dir=tmp_path / "p", herdr_bin=str(tmp_path / "none"), env={})
    assert [i.action for i in items] == ["skipped"] and "herdr" in items[0].detail
    assert not (tmp_path / "p").exists()


@herdr_server
def test_the_link_command_and_the_auto_check_against_a_real_server(tmp_path: Path) -> None:
    import subprocess
    import sys
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    with isolated_herdr("ctlcli") as h:
        env = {**h.env, "PYTHONPATH": str(REPO), "HERDR_ENGINEERING_STATE": str(tmp_path / "st"), "HERDR_HANDOFF_AUTO": "1",
               "HERDR_BIN": "herdr"}

        def run(*argv: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, "-c", code, *argv], env=env, cwd=h.root, capture_output=True, text=True, timeout=60)

        before = run("handoff", "auto", "check", "--home", str(tmp_path), "--json")
        assert before.returncode == 1 and '"controller"' in before.stdout and "not linked" in before.stdout
        linked = run("handoff", "controller", "link", "--deploy-dir", str(DEPLOY), "--herdr-eng", "/abs/herdr-eng")
        assert linked.returncode == 0 and linked.stdout.count("installed") == 2, linked.stdout + linked.stderr
        assert str(tmp_path / "plugins") in linked.stdout
        again = run("handoff", "controller", "link", "--deploy-dir", str(DEPLOY), "--herdr-eng", "/abs/herdr-eng")
        assert again.returncode == 0 and again.stdout.count("unchanged") == 2, again.stdout
        after = run("handoff", "auto", "check", "--home", str(tmp_path), "--json")
        assert '"controller"' in after.stdout and "restart Herdr" in after.stdout
        assert '"action": "missing", "detail": "controller' not in after.stdout
