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
from herdr_engineering.handoff_controller_state import Presence as P
from herdr_engineering.handoff_controller_state import SidePaneState as S
from tests.handoff_controller_herdr import isolated_herdr

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
herdr_server = pytest.mark.herdr_server


def make_exe(directory: Path, name: str = "herdr-eng") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


@pytest.mark.parametrize("plugin,running,off,expected", [
    (P.ENABLED, True, False, C.RUNNING),
    (P.UNAVAILABLE, True, False, C.RUNNING),      # a running controller needs no answer from Herdr
    (P.ABSENT, True, False, C.RUNNING),
    (P.ENABLED, False, False, C.LINKED),
    (P.ABSENT, False, False, C.NOT_LINKED),
    (P.DISABLED, False, False, C.DISABLED),
    (P.UNAVAILABLE, False, False, C.HERDR_UNAVAILABLE),
    (P.NOT_UNDERSTOOD, False, False, C.REPLY_NOT_UNDERSTOOD),
    (P.ABSENT, False, True, C.SWITCHED_OFF),
    (P.UNAVAILABLE, False, True, C.SWITCHED_OFF),
    (P.ENABLED, True, True, C.SWITCHED_OFF),      # deliberately off wins
])
def test_controller_state_mapping(plugin, running, off, expected) -> None:
    assert st.classify_controller(plugin=plugin, running=running, switched_off=off) is expected


def test_side_pane_state_mapping() -> None:
    assert st.classify_side_pane(P.ENABLED) is S.LINKED
    assert st.classify_side_pane(P.ABSENT) is S.NOT_LINKED
    assert st.classify_side_pane(P.DISABLED) is S.DISABLED
    assert st.classify_side_pane(P.UNAVAILABLE) is S.HERDR_UNAVAILABLE
    assert st.classify_side_pane(P.NOT_UNDERSTOOD) is S.REPLY_NOT_UNDERSTOOD


def test_a_disabled_plugin_names_the_enable_command_and_an_unreadable_reply_is_its_own_state() -> None:
    disabled = st.describe_controller(C.DISABLED, DEPLOY)
    assert disabled.blocking and "herdr plugin enable herdr.handoff-controller" in disabled.fix
    pane = st.describe_side_pane(S.DISABLED, DEPLOY)
    assert not pane.blocking and "herdr plugin enable herdr.session-pane" in pane.fix
    garbled = st.describe_controller(C.REPLY_NOT_UNDERSTOOD, DEPLOY)
    assert garbled.blocking and "not understood" in garbled.detail and "not answering" not in garbled.detail
    assert "not answering" in st.describe_controller(C.HERDR_UNAVAILABLE, DEPLOY).detail


def test_only_a_missing_controller_blocks_and_each_state_carries_a_fix() -> None:
    blocking = {c for c in C if st.describe_controller(c, DEPLOY).blocking}
    assert blocking == {C.NOT_LINKED, C.HERDR_UNAVAILABLE, C.DISABLED, C.REPLY_NOT_UNDERSTOOD}
    for c in (C.NOT_LINKED, C.LINKED, C.HERDR_UNAVAILABLE, C.DISABLED, C.REPLY_NOT_UNDERSTOOD):
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
        exe = str(make_exe(tmp_path / "bin"))
        first = st.link_plugins(DEPLOY, exe, data_dir=data, herdr_bin="herdr", env=env, cwd=h.root)
        assert [i.action for i in first] == ["installed", "installed"], first
        controller, side = st.controller_findings(env, herdr_bin="herdr", deploy_dir=DEPLOY, cwd=h.root)
        assert controller.state is C.LINKED and not controller.blocking and side.state is S.LINKED
        assert exe in (data / "herdr-handoff-controller-plugin" / "herdr-plugin.toml").read_text()

        second = st.link_plugins(DEPLOY, exe, data_dir=data, herdr_bin="herdr", env=env, cwd=h.root)
        assert [i.action for i in second] == ["unchanged", "unchanged"], second

        moved = st.link_plugins(DEPLOY, exe, data_dir=tmp_path / "plugins2", herdr_bin="herdr", env=env, cwd=h.root)
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
    items = st.link_plugins(DEPLOY, str(make_exe(tmp_path / "bin")), data_dir=tmp_path / "p", herdr_bin=str(tmp_path / "none"), env={})
    assert [i.action for i in items] == ["skipped"] and "herdr" in items[0].detail
    assert not (tmp_path / "p").exists()


@herdr_server
def test_the_link_command_and_the_auto_check_against_a_real_server(tmp_path: Path) -> None:
    import subprocess
    import sys
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    exe = make_exe(tmp_path / "bin")
    with isolated_herdr("ctlcli") as h:
        env = {**h.env, "PYTHONPATH": str(REPO), "HERDR_ENGINEERING_STATE": str(tmp_path / "st"), "HERDR_HANDOFF_AUTO": "1",
               "HERDR_BIN": "herdr"}

        def run(*argv: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, "-c", code, *argv], env=env, cwd=h.root, capture_output=True, text=True, timeout=60)

        before = run("handoff", "auto", "check", "--home", str(tmp_path), "--json")
        assert before.returncode == 1 and '"controller"' in before.stdout and "not linked" in before.stdout
        linked = run("handoff", "controller", "link", "--deploy-dir", str(DEPLOY), "--herdr-eng", str(exe))
        assert linked.returncode == 0 and linked.stdout.count("installed") == 2, linked.stdout + linked.stderr
        assert str(tmp_path / "plugins") in linked.stdout
        again = run("handoff", "controller", "link", "--deploy-dir", str(DEPLOY), "--herdr-eng", str(exe))
        assert again.returncode == 0 and again.stdout.count("unchanged") == 2, again.stdout
        after = run("handoff", "auto", "check", "--home", str(tmp_path), "--json")
        assert '"controller"' in after.stdout and "restart Herdr" in after.stdout
        assert '"action": "missing", "detail": "controller' not in after.stdout


# --- the entry point the manifests name ---------------------------------------------------------

def test_the_stable_link_is_named_when_it_points_at_this_install(tmp_path: Path) -> None:
    own = make_exe(tmp_path / "venv" / "bin")
    link = tmp_path / "local" / "herdr-eng"
    link.parent.mkdir()
    link.symlink_to(own)
    chosen = st.choose_entry_point(link, own, editable=False)
    assert chosen.path == str(link) and not chosen.notes


def test_a_foreign_link_is_left_alone_and_the_installs_own_binary_is_named_with_a_warning(tmp_path: Path) -> None:
    own = make_exe(tmp_path / "venv" / "bin")
    other = make_exe(tmp_path / "release" / "bin")
    link = tmp_path / "local" / "herdr-eng"
    link.parent.mkdir()
    link.symlink_to(other)
    chosen = st.choose_entry_point(link, own, editable=False)
    assert chosen.path == str(own)
    assert any("warning" in n and str(link) in n and str(own) in n for n in chosen.notes)


def test_a_regular_file_at_the_link_path_is_foreign_too(tmp_path: Path) -> None:
    own = make_exe(tmp_path / "venv" / "bin")
    link = make_exe(tmp_path / "local")
    assert st.choose_entry_point(link, own, editable=False).path == str(own)


def test_no_link_names_the_own_binary_with_a_note(tmp_path: Path) -> None:
    own = make_exe(tmp_path / "venv" / "bin")
    chosen = st.choose_entry_point(tmp_path / "local" / "herdr-eng", own, editable=False)
    assert chosen.path == str(own) and any("not linked" in n for n in chosen.notes)


def test_a_checkout_install_says_so(tmp_path: Path) -> None:
    own = make_exe(tmp_path / "checkout" / ".venv" / "bin")
    link = tmp_path / "local" / "herdr-eng"
    link.parent.mkdir()
    link.symlink_to(own)
    chosen = st.choose_entry_point(link, own, editable=True)
    assert chosen.path == str(link) and any("checkout install" in n and str(own) in n for n in chosen.notes)


@herdr_server
@pytest.mark.parametrize("make", ["missing", "not_executable", "directory"])
def test_a_herdr_eng_that_cannot_run_links_nothing(tmp_path: Path, make: str) -> None:
    path = tmp_path / "herdr-eng"
    if make == "not_executable":
        path.write_text("x")
        path.chmod(0o644)
    elif make == "directory":
        path.mkdir()
    with isolated_herdr("ctlbad") as h:
        items = st.link_plugins(DEPLOY, str(path), data_dir=tmp_path / "p", herdr_bin="herdr", env=h.env, cwd=h.root)
        assert [i.action for i in items] == ["unresolved"] and "herdr-eng" in items[0].detail
        assert not (tmp_path / "p").exists()
        assert st.PluginHost("herdr", h.env, h.root).plugin_roots() == {}


@herdr_server
def test_a_write_failure_for_one_plugin_is_one_line_and_the_other_is_still_linked(tmp_path: Path) -> None:
    exe = str(make_exe(tmp_path / "bin"))
    data = tmp_path / "plugins"
    data.mkdir()
    (data / st.CONTROLLER_PLUGIN).write_text("a file where the directory should be")
    with isolated_herdr("ctlwrite") as h:
        env = {**h.env, "HERDR_ENGINEERING_STATE": str(tmp_path / "state")}
        items = st.link_plugins(DEPLOY, exe, data_dir=data, herdr_bin="herdr", env=env, cwd=h.root)
        assert [i.action for i in items] == ["unresolved", "installed"], items
        assert "\n" not in items[0].detail and "cannot write" in items[0].detail
        assert not [p for p in data.rglob(".*") if p.is_file()]  # no stray temp file


def test_the_manifest_is_written_atomically(tmp_path: Path) -> None:
    target = tmp_path / "m" / "herdr-plugin.toml"
    target.parent.mkdir()
    target.write_text("old")
    st.write_atomically(target, "new")
    assert target.read_text() == "new" and [p.name for p in target.parent.iterdir()] == ["herdr-plugin.toml"]


@herdr_server
def test_a_disabled_plugin_is_reported_as_disabled(tmp_path: Path) -> None:
    import subprocess
    exe = str(make_exe(tmp_path / "bin"))
    with isolated_herdr("ctldis") as h:
        env = {**h.env, "HERDR_ENGINEERING_STATE": str(tmp_path / "state")}
        st.link_plugins(DEPLOY, exe, data_dir=tmp_path / "plugins", herdr_bin="herdr", env=env, cwd=h.root)
        subprocess.run(["herdr", "plugin", "disable", "herdr.handoff-controller"], env=env, cwd=h.root, check=True, capture_output=True)
        controller, side = st.controller_findings(env, herdr_bin="herdr", deploy_dir=DEPLOY, cwd=h.root)
        assert controller.state is C.DISABLED and controller.blocking and side.state is S.LINKED


def test_a_reply_that_is_not_json_is_not_understood_and_differs_from_no_answer(tmp_path: Path) -> None:
    junk = tmp_path / "herdr"
    junk.write_text("#!/bin/sh\necho 'this is not json'\n")
    junk.chmod(0o755)
    env = {"HERDR_ENGINEERING_STATE": str(tmp_path / "state"), "HOME": str(tmp_path)}
    controller, _ = st.controller_findings(env, herdr_bin=str(junk), deploy_dir=DEPLOY)
    assert controller.state is C.REPLY_NOT_UNDERSTOOD
    none, _ = st.controller_findings(env, herdr_bin=str(tmp_path / "absent"), deploy_dir=DEPLOY)
    assert none.state is C.HERDR_UNAVAILABLE
