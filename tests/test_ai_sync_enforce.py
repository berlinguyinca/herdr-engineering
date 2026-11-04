"""ai-sync enforcement: force every non-publisher host back to the shared standard, then prove it loads.

The ``ai-sync`` here is a real executable script that records its argv, so the tests assert the exact commands the
enforcer spawns without ever touching a real ``~/.claude`` (HOME is always inside ``tmp_path``).
"""
import fcntl
import json
import stat

import pytest

from herdr_engineering import ai_sync_enforce as enforce

VERIFY_OK = json.dumps({"ok": True, "errors": [], "warnings": [], "checked": {"skills": 3, "agents": 1, "commands": 2, "hooks": 1}})
VERIFY_BAD = json.dumps({"ok": False, "errors": [{"path": "skills/x/SKILL.md", "problem": "missing SKILL.md"}], "warnings": [],
                         "checked": {"skills": 3, "agents": 1, "commands": 2, "hooks": 1}})


def fake_ai_sync(tmp_path, *, pull_exit=0, verify_out=VERIFY_OK, verify_exit=0):
    log = tmp_path / "argv.log"
    path = tmp_path / "bin" / "ai-sync"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$*" in
  *pull*) exit {pull_exit} ;;
  *verify*) printf '%s' '{verify_out}'; exit {verify_exit} ;;
esac
exit 9
""")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path), log


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


def run(tmp_path, binary, *extra):
    return enforce.main(["--ai-sync-bin", binary, "--home", str(tmp_path), *extra])


def test_pulls_with_force_then_verifies_each_env_in_order(tmp_path, capsys):
    binary, log = fake_ai_sync(tmp_path)
    assert run(tmp_path, binary, "--env", "claude", "--env", "pi") == 0
    seen = calls(log)
    assert len(seen) == 4
    assert "pull --force --env claude" in seen[0] and "verify --env claude --json" in seen[1]
    assert "pull --force --env pi" in seen[2] and "verify --env pi --json" in seen[3]
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True and set(summary["envs"]) == {"claude", "pi"}


def test_defaults_to_the_claude_env(tmp_path):
    binary, log = fake_ai_sync(tmp_path)
    assert run(tmp_path, binary) == 0
    assert "pull --force --env claude" in calls(log)[0]


def test_never_pushes_or_runs_other_subcommands(tmp_path):
    binary, log = fake_ai_sync(tmp_path)
    run(tmp_path, binary, "--env", "claude")
    for line in calls(log):
        assert not {"push", "init", "bootstrap", "migrate"} & set(line.split())


def test_publisher_host_is_refused_before_ai_sync_runs(tmp_path, capsys):
    marker = tmp_path / ".config" / "herdr-engineering" / "ai-sync-publisher"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    binary, log = fake_ai_sync(tmp_path)
    assert run(tmp_path, binary) == 3
    assert calls(log) == []
    assert "publisher" in capsys.readouterr().err.lower()


def test_failed_verify_makes_the_run_fail_and_names_the_problem(tmp_path, capsys):
    binary, log = fake_ai_sync(tmp_path, verify_out=VERIFY_BAD, verify_exit=1)
    assert run(tmp_path, binary) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is False
    assert summary["envs"]["claude"]["verify_ok"] is False
    assert summary["envs"]["claude"]["error_sample"] == ["skills/x/SKILL.md: missing SKILL.md"]


def test_failed_pull_fails_the_run_but_still_reports_verify(tmp_path, capsys):
    binary, log = fake_ai_sync(tmp_path, pull_exit=2)
    assert run(tmp_path, binary) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["envs"]["claude"]["pull_ok"] is False and summary["envs"]["claude"]["verify_ok"] is True
    assert len(calls(log)) == 2


def test_unknown_env_is_rejected_before_anything_runs(tmp_path):
    binary, log = fake_ai_sync(tmp_path)
    with pytest.raises(SystemExit):
        run(tmp_path, binary, "--env", "claude; rm -rf /")
    assert calls(log) == []


def test_missing_ai_sync_is_a_clear_failure(tmp_path, capsys):
    assert enforce.main(["--ai-sync-bin", str(tmp_path / "nope"), "--home", str(tmp_path)]) == 2
    assert "not installed" in capsys.readouterr().err


def test_overlapping_run_is_skipped_not_stacked(tmp_path, capsys):
    binary, log = fake_ai_sync(tmp_path)
    lock = tmp_path / ".cache" / "herdr-engineering" / "ai-sync-enforce.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert run(tmp_path, binary) == 0
    assert calls(log) == []
    assert json.loads(capsys.readouterr().out)["skipped"] == "another enforcement run is in progress"
