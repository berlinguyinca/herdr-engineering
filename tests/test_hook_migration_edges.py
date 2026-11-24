"""Legacy-hook migration edge cases, for both installers, as the real CLI in a subprocess under a temp HOME:
`handoff auto install` (removes the 50% context reminder) and `workflow install` (removes the managed PreCompact)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = str(Path(__file__).resolve().parents[1])
MARK = "herdr-handoff-auto"
LEGACY_AUTO = {"hooks": [{"type": "command", "command":
                          f'[ -x "$HOME/.local/bin/herdr-eng" ] && "$HOME/.local/bin/herdr-eng" handoff context-hook claude || true # {MARK}'}]}
LEGACY_WORKFLOW = {"hooks": [{"type": "command", "timeout": 3, "command":
                              "[ -x /opt/herdr-eng ] && /opt/herdr-eng workflow hook PreCompact || true # herdr-workflow:managed"}]}
CASES = {
    "auto": (["handoff", "auto", "install"], "UserPromptSubmit", LEGACY_AUTO),
    "workflow": (["workflow", "install"], "PreCompact", LEGACY_WORKFLOW),
}


@pytest.fixture(params=sorted(CASES))
def tool(request):
    return request.param


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    return tmp_path


def settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def run(tool: str, home: Path) -> subprocess.CompletedProcess[str]:
    argv, _, _ = CASES[tool]
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    return subprocess.run([sys.executable, "-c", code, *argv, "--home", str(home)], cwd=REPO, capture_output=True, text=True,
                          timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": str(home), "PYTHONPATH": REPO, "HERDR_HANDOFF_AUTO": "0"})


def clean(done: subprocess.CompletedProcess[str]) -> None:
    out = done.stdout + done.stderr
    assert "Traceback" not in out and "Error" not in out.replace("PermissionError", "") or "refused" in out or "cannot" in out, out


def test_settings_missing_is_created(tool, home) -> None:
    done = run(tool, home)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "hooks" in json.loads(settings(home).read_text())


def test_empty_settings_file_is_filled(tool, home) -> None:
    settings(home).write_text("")
    done = run(tool, home)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "hooks" in json.loads(settings(home).read_text())


def test_invalid_json_is_refused_and_left_alone(tool, home) -> None:
    settings(home).write_bytes(b'{"hooks": [oops')
    done = run(tool, home)
    assert done.returncode == 1 and "Traceback" not in done.stdout + done.stderr
    assert settings(home).read_bytes() == b'{"hooks": [oops'


@pytest.mark.parametrize("shape", [[], "x", 7])
def test_hooks_that_is_not_an_object_is_refused_with_a_message(tool, home, shape) -> None:
    raw = json.dumps({"model": "m", "hooks": shape}).encode()
    settings(home).write_bytes(raw)
    done = run(tool, home)
    out = done.stdout + done.stderr
    assert done.returncode == 1 and "Traceback" not in out and "AttributeError" not in out, out
    assert "settings.json" in out
    assert settings(home).read_bytes() == raw


def test_an_event_left_empty_after_removal_is_deleted(tool, home) -> None:
    _, event, legacy = CASES[tool]
    settings(home).write_text(json.dumps({"hooks": {event: [legacy]}}))
    assert run(tool, home).returncode == 0
    assert event not in json.loads(settings(home).read_text())["hooks"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_read_only_directory_gives_one_clear_line_and_no_partial_write(tool, home) -> None:
    _, event, legacy = CASES[tool]
    raw = json.dumps({"hooks": {event: [legacy]}}).encode()
    settings(home).write_bytes(raw)
    (home / ".claude").chmod(0o555)
    try:
        done = run(tool, home)
    finally:
        (home / ".claude").chmod(0o755)
    out = (done.stdout + done.stderr).strip()
    assert done.returncode == 1 and "Traceback" not in out, out
    assert len(out.splitlines()) <= 2 and "settings.json" in out, out
    assert settings(home).read_bytes() == raw
    assert [p.name for p in (home / ".claude").iterdir()] == ["settings.json"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_read_only_file_is_not_replaced(tool, home) -> None:
    _, event, legacy = CASES[tool]
    raw = json.dumps({"hooks": {event: [legacy]}}).encode()
    settings(home).write_bytes(raw)
    settings(home).chmod(0o444)
    done = run(tool, home)
    out = (done.stdout + done.stderr).strip()
    assert done.returncode == 1 and "Traceback" not in out and "read-only" in out, out
    assert settings(home).read_bytes() == raw


def test_a_symlinked_settings_file_stays_a_link_and_the_target_is_written(tool, home) -> None:
    _, event, legacy = CASES[tool]
    real = home / "dotfiles" / "claude-settings.json"
    real.parent.mkdir()
    original = {"model": "m", "hooks": {event: [legacy]}}
    real.write_text(json.dumps(original, indent=2))
    settings(home).symlink_to(real)
    done = run(tool, home)
    assert done.returncode == 0, done.stdout + done.stderr
    assert settings(home).is_symlink() and settings(home).resolve() == real.resolve()
    data = json.loads(real.read_text())
    assert event not in data["hooks"] and data["model"] == "m" and len(data["hooks"]) >= 1
    backups = [p for p in (home / ".local/share/herdr-engineering").rglob("settings.json*") if p.is_file()]
    assert backups and not any(p.is_symlink() for p in backups)
    assert json.loads(sorted(backups)[0].read_text()) == original  # the real file's content, not a link
    assert not [p for p in real.parent.iterdir() if p != real] and not [p for p in (home / ".claude").iterdir() if p.name != "settings.json"]
