"""``herdr-eng handoff auto install|uninstall|check``: real files under a temp HOME, real CLI subprocess."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import handoff_auto_setup as auto_setup
from herdr_engineering.handoff_auto_setup import HOOK_COMMAND, HOOK_MARKER, START_COMMAND, STOP_COMMAND

REPO = str(Path(__file__).resolve().parents[1])
ENTRY = {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
START = {"hooks": [{"type": "command", "command": START_COMMAND}]}
STOP = {"hooks": [{"type": "command", "command": STOP_COMMAND}]}
ALL = {"UserPromptSubmit": [ENTRY], "SubagentStart": [START], "SubagentStop": [STOP]}


def cli(home: Path, *argv: str) -> tuple[int, dict]:
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    p = subprocess.run([sys.executable, "-c", code, "handoff", "auto", *argv, "--home", str(home), "--json"], cwd=REPO,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONPATH": REPO}, capture_output=True, text=True, timeout=60)
    return p.returncode, json.loads(p.stdout)


def settings_path(home: Path) -> Path:
    return home / ".claude/settings.json"


def action(out: dict) -> str:
    """The single action all three hook items share (fails if they differ)."""
    actions = {r["action"] for r in out["results"]}
    assert len(out["results"]) in (1, 3) and len(actions) == 1, out
    return actions.pop()


def actions(out: dict) -> list[str]:
    return [r["action"] for r in out["results"]]


def test_command_matches_documented_snippet() -> None:
    assert HOOK_COMMAND == ('[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng" handoff context-hook claude '
                            '|| true # herdr-handoff-auto')
    assert HOOK_MARKER == "herdr-handoff-auto"
    assert START_COMMAND == ('[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng" handoff subagent-hook start '
                             '|| true # herdr-handoff-auto')
    assert STOP_COMMAND == START_COMMAND.replace("hook start", "hook stop")


def test_install_into_missing_settings(tmp_path: Path) -> None:
    rc, out = cli(tmp_path, "install")
    assert rc == 0 and action(out) == "installed"
    assert json.loads(settings_path(tmp_path).read_text()) == {"hooks": ALL}


def test_install_into_empty_file(tmp_path: Path) -> None:
    settings_path(tmp_path).parent.mkdir(parents=True)
    settings_path(tmp_path).write_text("")
    assert action(cli(tmp_path, "install")[1]) == "installed"
    assert json.loads(settings_path(tmp_path).read_text())["hooks"]["UserPromptSubmit"] == [ENTRY]


def test_install_preserves_other_hooks_keys_and_order(tmp_path: Path) -> None:
    other = {"matcher": "x", "hooks": [{"type": "command", "command": "echo hi"}]}
    start = {"model": "opus", "hooks": {"SessionStart": [other], "UserPromptSubmit": [other]}, "env": {"A": "1"}}
    settings_path(tmp_path).parent.mkdir(parents=True)
    settings_path(tmp_path).write_text(json.dumps(start, indent=4) + "\n")
    assert action(cli(tmp_path, "install")[1]) == "installed"
    text = settings_path(tmp_path).read_text()
    data = json.loads(text)
    assert list(data) == ["model", "hooks", "env"]
    assert data["hooks"]["SessionStart"] == [other]
    assert data["hooks"]["UserPromptSubmit"] == [other, ENTRY]
    assert data["hooks"]["SubagentStart"] == [START] and data["hooks"]["SubagentStop"] == [STOP]
    assert data["env"] == {"A": "1"} and text.splitlines()[1].startswith("    \"")


def test_second_install_is_idempotent(tmp_path: Path) -> None:
    cli(tmp_path, "install")
    before = settings_path(tmp_path).read_bytes()
    rc, out = cli(tmp_path, "install")
    assert rc == 0 and action(out) == "unchanged"
    assert settings_path(tmp_path).read_bytes() == before


def test_hand_added_identical_entry_not_duplicated(tmp_path: Path) -> None:
    settings_path(tmp_path).parent.mkdir(parents=True)
    raw = json.dumps({"hooks": ALL}, indent=2) + "\n"
    settings_path(tmp_path).write_text(raw)
    assert action(cli(tmp_path, "install")[1]) == "unchanged"
    assert settings_path(tmp_path).read_text() == raw


def test_marked_entry_with_other_command_is_preserved(tmp_path: Path) -> None:
    custom = {"hooks": [{"type": "command", "command": f"my-own-thing # {HOOK_MARKER}"}]}
    settings_path(tmp_path).parent.mkdir(parents=True)
    raw = json.dumps({"hooks": {"UserPromptSubmit": [custom], "SubagentStart": [custom], "SubagentStop": [custom]}}) + "\n"
    settings_path(tmp_path).write_text(raw)
    assert action(cli(tmp_path, "install")[1]) == "preserved"
    assert settings_path(tmp_path).read_text() == raw


def test_uninstall_removes_only_marked_entry(tmp_path: Path) -> None:
    other = {"hooks": [{"type": "command", "command": "echo hi"}]}
    settings_path(tmp_path).parent.mkdir(parents=True)
    settings_path(tmp_path).write_text(json.dumps({"model": "m", "hooks": {"UserPromptSubmit": [other, ENTRY], "SubagentStart": [START], "SubagentStop": [other, STOP], "Stop": [other]}}, indent=2))
    rc, out = cli(tmp_path, "uninstall")
    assert rc == 0 and action(out) == "removed"
    assert json.loads(settings_path(tmp_path).read_text()) == {"model": "m", "hooks": {"UserPromptSubmit": [other], "SubagentStop": [other], "Stop": [other]}}
    assert action(cli(tmp_path, "uninstall")[1]) == "unchanged"


def test_uninstall_drops_empty_containers(tmp_path: Path) -> None:
    cli(tmp_path, "install")
    assert action(cli(tmp_path, "uninstall")[1]) == "removed"
    assert json.loads(settings_path(tmp_path).read_text()) == {}


def test_invalid_json_is_refused_and_untouched(tmp_path: Path) -> None:
    settings_path(tmp_path).parent.mkdir(parents=True)
    raw = b'{"hooks": {oops'
    settings_path(tmp_path).write_bytes(raw)
    for verb in ("install", "uninstall", "check"):
        rc, out = cli(tmp_path, verb)
        assert rc == 1 and action(out) == "refused"
        assert settings_path(tmp_path).read_bytes() == raw


def test_check_ok_and_missing(tmp_path: Path) -> None:
    rc, out = cli(tmp_path, "check")
    assert rc == 1 and action(out) == "missing"
    cli(tmp_path, "install")
    rc, out = cli(tmp_path, "check")
    assert rc == 0 and action(out) == "ok"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    assert action(cli(tmp_path, "install", "--dry-run")[1]) == "installed"
    assert not settings_path(tmp_path).exists()


def test_old_install_gets_the_two_new_entries(tmp_path: Path) -> None:
    settings_path(tmp_path).parent.mkdir(parents=True)
    settings_path(tmp_path).write_text(json.dumps({"hooks": {"UserPromptSubmit": [ENTRY]}}))
    rc, out = cli(tmp_path, "check")
    assert rc == 1 and actions(out) == ["ok", "missing", "missing"]
    rc, out = cli(tmp_path, "install")
    assert rc == 0 and actions(out) == ["unchanged", "installed", "installed"]
    assert json.loads(settings_path(tmp_path).read_text()) == {"hooks": ALL}
    assert actions(cli(tmp_path, "check")[1]) == ["ok", "ok", "ok"]


def test_check_missing_when_only_one_event_lacks_entry(tmp_path: Path) -> None:
    settings_path(tmp_path).parent.mkdir(parents=True)
    settings_path(tmp_path).write_text(json.dumps({"hooks": {"UserPromptSubmit": [ENTRY], "SubagentStart": [START]}}))
    rc, out = cli(tmp_path, "check")
    assert rc == 1 and actions(out) == ["ok", "ok", "missing"]


def test_uninstall_removes_subagent_entries_too(tmp_path: Path) -> None:
    cli(tmp_path, "install")
    assert actions(cli(tmp_path, "uninstall")[1]) == ["removed"] * 3
    assert json.loads(settings_path(tmp_path).read_text()) == {}


# --- install_subagent_hooks: SubagentStart/SubagentStop only, real files under .herdr-test/home/ ---------------------
TEST_HOME = Path(REPO) / ".herdr-test" / "home"


@pytest.fixture
def home():
    shutil.rmtree(TEST_HOME, ignore_errors=True)
    TEST_HOME.mkdir(parents=True)
    yield TEST_HOME
    shutil.rmtree(TEST_HOME, ignore_errors=True)


def test_install_subagent_hooks_writes_start_and_stop_only(home: Path) -> None:
    items = auto_setup.install_subagent_hooks(home)
    assert [i.action for i in items] == ["installed", "installed"]
    assert json.loads(settings_path(home).read_text()) == {"hooks": {"SubagentStart": [START], "SubagentStop": [STOP]}}
    assert [i.action for i in auto_setup.check(home)] == ["missing", "ok", "ok"]
    assert {i.action for i in auto_setup.uninstall(home)} == {"removed", "unchanged"}
    assert json.loads(settings_path(home).read_text()) == {}


def test_second_install_is_noop(home: Path) -> None:
    auto_setup.install_subagent_hooks(home)
    before = settings_path(home).read_bytes()
    assert [i.action for i in auto_setup.install_subagent_hooks(home)] == ["unchanged", "unchanged"]
    assert settings_path(home).read_bytes() == before


def test_foreign_hooks_untouched(home: Path) -> None:
    other = {"matcher": "x", "hooks": [{"type": "command", "command": "echo hi"}]}
    settings_path(home).parent.mkdir(parents=True)
    settings_path(home).write_text(json.dumps({"model": "m", "hooks": {"SubagentStart": [other], "Stop": [other]}}, indent=2))
    auto_setup.install_subagent_hooks(home)
    data = json.loads(settings_path(home).read_text())
    assert data["model"] == "m" and data["hooks"]["Stop"] == [other]
    assert data["hooks"]["SubagentStart"] == [other, START] and data["hooks"]["SubagentStop"] == [STOP]
    assert "UserPromptSubmit" not in data["hooks"]


def test_invalid_json_not_rewritten(home: Path) -> None:
    settings_path(home).parent.mkdir(parents=True)
    raw = b'{"hooks": {oops'
    settings_path(home).write_bytes(raw)
    assert [i.action for i in auto_setup.install_subagent_hooks(home)] == ["refused"]
    assert settings_path(home).read_bytes() == raw


def test_cli_subagents_only_installs_just_the_two_hooks(home: Path) -> None:
    rc, out = cli(home, "install", "--subagents-only")
    assert rc == 0 and actions(out) == ["installed", "installed"]
    assert set(json.loads(settings_path(home).read_text())["hooks"]) == {"SubagentStart", "SubagentStop"}


def _installed_command(home: Path, event: str) -> str:
    return json.loads(settings_path(home).read_text())["hooks"][event][0]["hooks"][0]["command"]


def _run_hook(home: Path, event: str, payload: str, state: Path, bin_exists: bool) -> subprocess.CompletedProcess:
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "herdr-eng"
    if bin_exists:
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -c "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))" "$@"\n')
        wrapper.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONPATH": REPO, "HERDR_PANE_ID": "w1:p1", "HERDR_ENGINEERING_STATE": str(state)}
    return subprocess.run(["sh", "-c", _installed_command(home, event)], input=payload, env=env, capture_output=True, text=True, timeout=60)


def test_installed_start_hook_registers_agent(home: Path) -> None:
    auto_setup.install_subagent_hooks(home)
    state = home / "state"
    payload = json.dumps({"agent_id": "a1", "agent_type": "general-purpose"})
    done = _run_hook(home, "SubagentStart", payload, state, bin_exists=True)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    assert (state / "subagents" / "w1:p1" / "a1").is_file()
    done = _run_hook(home, "SubagentStop", payload, state, bin_exists=True)
    assert done.returncode == 0 and not (state / "subagents" / "w1:p1" / "a1").exists()


def test_installed_hooks_exit_zero_silently_when_binary_is_missing(home: Path) -> None:
    auto_setup.install_subagent_hooks(home)
    for event in ("SubagentStart", "SubagentStop"):
        done = _run_hook(home, event, "{}", home / "state", bin_exists=False)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
        assert not (home / "state").exists()


def test_install_sh_has_the_subagent_hooks_step() -> None:
    script = (Path(REPO) / "scripts/install.sh").read_text()
    assert 'WITH_SUBAGENT_HOOKS="${HERDR_INSTALL_SUBAGENT_HOOKS:-1}"' in script
    assert "--no-subagent-hooks) WITH_SUBAGENT_HOOKS=0" in script
    assert "handoff auto install --subagents-only" in script
    assert subprocess.run(["bash", "-n", str(Path(REPO) / "scripts/install.sh")]).returncode == 0
