"""Headless /summary runner: fake harness binaries, no network, no real sessions."""
import json
import stat
import subprocess
import sys

import pytest

from herdr_engineering import summary_report as sr

REPORT = "# Session summary\n**Overall status:** completed\n\nEvidence coverage: complete\n"


def _fake(tmp_path, name, body):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(bindir)


def _env(bindir, **extra):
    return {"PATH": bindir + ":/usr/bin:/bin", "HOME": "/nonexistent", **extra}


def _claude_ok(tmp_path, result=REPORT):
    return _fake(tmp_path, "claude", "import json,os,sys\n"
                 "open(os.environ['PROBE'],'w').write(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),"
                 "'pane':os.environ.get('HERDR_PANE_ID')}))\n"
                 f"print(json.dumps({{'is_error':False,'result':{result!r}}}))\n")


def _which(bindir):
    import shutil
    return lambda name: shutil.which(name, path=bindir)


def _home_with_session(tmp_path, cwd=None, sid="abc-123"):
    home = tmp_path / "home"
    row = {"type": "user", "timestamp": "t1", "message": {"content": "goal: ship it"}}
    if cwd:
        row["cwd"] = str(cwd)
    path = home / ".claude/projects/-w" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row) + "\n")
    return home


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    (repo / "dirty.txt").write_text("x")
    return repo


def test_claude_report_gets_evidence_no_tools_and_a_scrubbed_scratch_run(tmp_path):
    bindir = _claude_ok(tmp_path)
    repo = _repo(tmp_path)
    home = _home_with_session(tmp_path, cwd=repo)
    probe = tmp_path / "probe.json"
    out = sr.run_report("claude", "abc-123", home=home, environ=_env(bindir, PROBE=str(probe), HERDR_PANE_ID="w1:p1"),
                        which=_which(bindir))
    assert out["ok"] and out["report"].startswith("# Session summary") and out["harness"] == "claude"
    seen = json.loads(probe.read_text())
    argv = seen["argv"]
    assert argv[0] == "-p" and argv[1].startswith("/summary claude abc-123\n")
    evidence = json.loads(argv[1].split("\n", 3)[3])
    assert "goal: ship it" in json.dumps(evidence["history"]["events"])
    assert evidence["history"]["method"] == "explicit" and evidence["history"]["cwd"] == str(repo)
    assert evidence["git"]["available"] and "dirty.txt" in evidence["git"]["status"]
    assert "first" in evidence["git"]["recent_commits"]
    assert argv[argv.index("--tools") + 1] == ""                                 # no tools: read-only by construction
    assert "--allowedTools" not in argv
    for flag in ("--no-session-persistence", "--strict-mcp-config"):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--disable-slash-commands" not in argv and "--bare" not in argv      # the skill must load
    assert seen["pane"] is None                                                 # never the summarizer's own pane
    assert seen["cwd"] != str(tmp_path) and "herdr-summary-report-" in seen["cwd"]


def test_git_reads_are_fixed_argv_and_disable_repo_hooks(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="x\n", stderr="")
    facts = sr._git_facts(str(tmp_path), run)
    assert facts["available"]
    for argv in calls:
        assert argv[:5] == ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C"]
        assert argv[5] == str(tmp_path) and argv[6] in {"rev-parse", "branch", "status", "log", "diff"}
    assert all("--no-ext-diff" in a for a in calls if a[6] == "diff")


def test_unknown_directory_or_non_repo_is_reported_not_guessed(tmp_path):
    assert sr._git_facts(None, subprocess.run)["available"] is False
    assert sr._git_facts(str(tmp_path / "gone"), subprocess.run)["available"] is False
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sr._git_facts(str(plain), subprocess.run)["reason"] == "not a git repository"


def test_no_session_record_never_starts_a_summarizer(tmp_path):
    def boom(*a, **k):
        raise AssertionError("must not run")
    with pytest.raises(sr.ReportError) as err:
        sr.run_report("claude", "missing-id", home=tmp_path, run=boom, which=lambda n: "/x")
    assert err.value.code == "no_session_record"


def test_codex_report_reads_the_last_message_file(tmp_path):
    bindir = _fake(tmp_path, "codex", "import sys\nargv=sys.argv\n"
                   "assert argv[1:3]==['exec','-s'] and argv[3]=='read-only' and '--ephemeral' in argv\n"
                   "assert argv[-1].startswith('$summary claude abc-123')\n"
                   f"open(argv[argv.index('--output-last-message')+1],'w').write({REPORT!r})\n")
    home = _home_with_session(tmp_path)
    out = sr.run_report("claude", "abc-123", harness="codex", home=home, environ=_env(bindir), which=_which(bindir))
    assert out["ok"] and out["harness"] == "codex"


def test_permission_denials_are_disclosed(tmp_path):
    bindir = _fake(tmp_path, "claude", "import json\nprint(json.dumps({'is_error':False,'permission_denials':[{}],"
                   f"'result':{REPORT!r}}}))\n")
    home = _home_with_session(tmp_path)
    out = sr.run_report("claude", "abc-123", home=home, environ=_env(bindir), which=_which(bindir))
    assert "denied" in out["report"]


@pytest.mark.parametrize("agent,sid,code", [
    ("gemini", "abc", "invalid_agent"),
    ("claude", "--resume", "invalid_session_id"),
    ("claude", "a b", "invalid_session_id"),
    ("claude", "a;rm -rf /", "invalid_session_id"),
    ("claude", "", "invalid_session_id"),
])
def test_bad_targets_never_reach_a_process(tmp_path, agent, sid, code):
    def boom(*a, **k):
        raise AssertionError("must not run")
    with pytest.raises(sr.ReportError) as err:
        sr.run_report(agent, sid, run=boom, which=lambda n: "/x")
    assert err.value.code == code


def test_unsupported_and_missing_harness(tmp_path):
    with pytest.raises(sr.ReportError) as err:
        sr.run_report("claude", "a", harness="pi", which=lambda n: "/x")
    assert err.value.code == "unsupported_harness"
    with pytest.raises(sr.ReportError) as err:
        sr.run_report("claude", "a", which=lambda n: None)
    assert err.value.code == "harness_missing"


def test_output_must_follow_the_skill_contract(tmp_path):
    bindir = _claude_ok(tmp_path, "Sure! Here is what I found.")
    with pytest.raises(sr.ReportError) as err:
        sr.run_report("claude", "abc-123", home=_home_with_session(tmp_path), environ=_env(bindir, PROBE=str(tmp_path / "p")), which=_which(bindir))
    assert err.value.code == "malformed_report"


def test_report_is_redacted(tmp_path):
    leaked = REPORT + "\nkey sk-ant-api03-SECRETSECRETSECRETSECRET1234\n"
    bindir = _claude_ok(tmp_path, leaked)
    out = sr.run_report("claude", "abc-123", home=_home_with_session(tmp_path),
                        environ=_env(bindir, PROBE=str(tmp_path / "p")), which=_which(bindir))
    assert "SECRETSECRET" not in out["report"]


def test_nonzero_exit_and_error_result_are_failures(tmp_path):
    home = _home_with_session(tmp_path)
    bindir = _fake(tmp_path, "claude", "import sys\nsys.exit(3)\n")
    with pytest.raises(sr.ReportError) as err:
        sr.run_report("claude", "abc-123", home=home, environ=_env(bindir), which=_which(bindir))
    assert err.value.code == "summarizer_failed"
    bindir = _fake(tmp_path, "claude", "import json\nprint(json.dumps({'is_error':True,'subtype':'error_max_turns'}))\n")
    with pytest.raises(sr.ReportError) as err:
        sr.run_report("claude", "abc-123", home=home, environ=_env(bindir), which=_which(bindir))
    assert err.value.code == "summarizer_failed"


def test_timeout_kills_the_run(tmp_path):
    bindir = _fake(tmp_path, "claude", "import time\ntime.sleep(30)\n")
    with pytest.raises(sr.ReportError) as err:
        sr.run_report("claude", "abc-123", home=_home_with_session(tmp_path), timeout=1, environ=_env(bindir), which=_which(bindir))
    assert err.value.code == "timeout"


def test_cli_prints_json_and_fails_cleanly(tmp_path):
    bindir = _claude_ok(tmp_path)
    home = _home_with_session(tmp_path)
    env = {**_env(bindir, PROBE=str(tmp_path / "p")), "HOME": str(home),
           "PYTHONPATH": str(sr.Path(sr.__file__).parent.parent)}
    cmd = [sys.executable, "-m", "herdr_engineering.cli", "summary", "report", "--agent", "claude"]
    ok = subprocess.run([*cmd, "--session", "abc-123"], env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)["ok"] is True
    missing = subprocess.run([*cmd, "--session", "nope"], env=env, capture_output=True, text=True)
    assert missing.returncode == 1 and json.loads(missing.stdout)["error"] == "no_session_record"
    bad = subprocess.run([*cmd, "--session", "--x"], env=env, capture_output=True, text=True)
    assert bad.returncode != 0
