"""``herdr-eng handoff auto install|uninstall|check``: real files under a temp HOME, real CLI subprocess."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from herdr_engineering.handoff_auto_setup import HOOK_COMMAND, HOOK_MARKER

REPO = str(Path(__file__).resolve().parents[1])
ENTRY = {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}


def cli(home: Path, *argv: str) -> tuple[int, dict]:
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    p = subprocess.run([sys.executable, "-c", code, "handoff", "auto", *argv, "--home", str(home), "--json"], cwd=REPO,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONPATH": REPO}, capture_output=True, text=True, timeout=60)
    return p.returncode, json.loads(p.stdout)


def settings_path(home: Path) -> Path:
    return home / ".claude/settings.json"


def action(out: dict) -> str:
    return out["results"][0]["action"]


def test_command_matches_documented_snippet() -> None:
    assert HOOK_COMMAND == ('[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng" handoff context-hook claude '
                            '|| true # herdr-handoff-auto')
    assert HOOK_MARKER == "herdr-handoff-auto"


def test_install_into_missing_settings(tmp_path: Path) -> None:
    rc, out = cli(tmp_path, "install")
    assert rc == 0 and action(out) == "installed"
    assert json.loads(settings_path(tmp_path).read_text()) == {"hooks": {"UserPromptSubmit": [ENTRY]}}


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
    assert data["env"] == {"A": "1"} and text.splitlines()[1].startswith("    \"")


def test_second_install_is_idempotent(tmp_path: Path) -> None:
    cli(tmp_path, "install")
    before = settings_path(tmp_path).read_bytes()
    rc, out = cli(tmp_path, "install")
    assert rc == 0 and action(out) == "unchanged"
    assert settings_path(tmp_path).read_bytes() == before


def test_hand_added_identical_entry_not_duplicated(tmp_path: Path) -> None:
    settings_path(tmp_path).parent.mkdir(parents=True)
    raw = json.dumps({"hooks": {"UserPromptSubmit": [ENTRY]}}, indent=2) + "\n"
    settings_path(tmp_path).write_text(raw)
    assert action(cli(tmp_path, "install")[1]) == "unchanged"
    assert settings_path(tmp_path).read_text() == raw


def test_marked_entry_with_other_command_is_preserved(tmp_path: Path) -> None:
    custom = {"hooks": [{"type": "command", "command": f"my-own-thing # {HOOK_MARKER}"}]}
    settings_path(tmp_path).parent.mkdir(parents=True)
    raw = json.dumps({"hooks": {"UserPromptSubmit": [custom]}}) + "\n"
    settings_path(tmp_path).write_text(raw)
    assert action(cli(tmp_path, "install")[1]) == "preserved"
    assert settings_path(tmp_path).read_text() == raw


def test_uninstall_removes_only_marked_entry(tmp_path: Path) -> None:
    other = {"hooks": [{"type": "command", "command": "echo hi"}]}
    settings_path(tmp_path).parent.mkdir(parents=True)
    settings_path(tmp_path).write_text(json.dumps({"model": "m", "hooks": {"UserPromptSubmit": [other, ENTRY], "Stop": [other]}}, indent=2))
    rc, out = cli(tmp_path, "uninstall")
    assert rc == 0 and action(out) == "removed"
    assert json.loads(settings_path(tmp_path).read_text()) == {"model": "m", "hooks": {"UserPromptSubmit": [other], "Stop": [other]}}
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
