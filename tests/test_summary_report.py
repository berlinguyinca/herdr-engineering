"""Headless /summary runner: fake harness binaries, no network, no real sessions."""
import json
import os
import stat
from pathlib import Path
import subprocess
import sys

import pytest

from herdr_engineering import summary_report as sr
from herdr_engineering.md_render import visible_width

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


def test_a_session_that_moved_into_a_worktree_is_judged_by_the_worktree(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()                                   # not a repository, like ~/IdeaProjects
    repo = _repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-q"], check=True, capture_output=True)
    git, others = sr._worktree_facts([str(other), str(parent), str(repo)], subprocess.run)
    assert git["available"] and git["cwd"] == str(repo) and "dirty.txt" in git["status"]
    assert [o["cwd"] for o in others] == [str(other)]      # the earlier distinct worktree is kept
    only_parent, none = sr._worktree_facts([str(parent)], subprocess.run)
    assert only_parent["available"] is False and none == []
    assert sr._worktree_facts([], subprocess.run)[0]["available"] is False
    # the same repository reached through two directories is reported once
    sub = repo / "sub"
    sub.mkdir()
    git, others = sr._worktree_facts([str(sub), str(repo)], subprocess.run)
    assert git["available"] and others == []


# -- publishing from a terminal ------------------------------------------------------------------
class _Cfg:
    control_plane_url = "http://cp.test"
    host_id = "bender"
    api_key = "k"


def _fake_herdr(calls, alive=True):
    def run(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["pane", "split"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": {"pane": {"pane_id": "w1:p9"}}}), stderr="")
        if argv[1:3] == ["pane", "get"]:
            return subprocess.CompletedProcess(argv, 0 if alive else 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
    return run


def test_publish_sends_to_the_dashboard_and_a_side_pane_and_prints_one_line(tmp_path):
    calls, posted = [], []
    out = sr.publish_report("claude", "abc-123", REPORT, home=tmp_path, environ={"HERDR_PANE_ID": "w1:p1"},
                            run=_fake_herdr(calls), load_config=lambda: _Cfg,
                            post=lambda cfg, body: posted.append(body) or (200, {"ok": True}))
    assert out["ok"] and out["dashboard"]["ok"] and out["pane"] == {"ok": True, "pane_id": "w1:p9"}
    assert posted[0]["agent"] == "claude" and posted[0]["target"] == "abc-123" and posted[0]["report"].startswith("# Session summary")
    assert "\n" not in out["message"] and "dashboard" in out["message"] and "side pane" in out["message"]
    split = next(c for c in calls if c[1:3] == ["pane", "split"])
    assert split[split.index("--pane") + 1] == "w1:p1" and "--no-focus" in split and "right" in split
    shown = next(c for c in calls if c[1:3] == ["pane", "run"])
    assert shown[3] == "w1:p9"
    # the pane runs the viewer on the report and tells it which pane to close when the reader quits
    assert shown[4:6] == [sys.executable, "-m"] and shown[-5:] == ["summary", "view", out["file"], "--close-pane", "w1:p9"]
    assert (Path(out["file"]).stat().st_mode & 0o777) == 0o600


def test_publish_replaces_a_pane_that_is_still_open_rather_than_typing_into_it(tmp_path):
    calls = []
    kw = dict(home=tmp_path, environ={"HERDR_PANE_ID": "w1:p1"}, run=_fake_herdr(calls), load_config=lambda: _Cfg,
              post=lambda cfg, body: (200, {}))
    sr.publish_report("claude", "abc-123", REPORT, **kw)
    assert not any(c[1:3] == ["pane", "close"] for c in calls)
    sr.publish_report("claude", "abc-123", REPORT, **kw)
    # a live pane is a pager still being read; keystrokes sent to it would be pager commands
    assert [c[3] for c in calls if c[1:3] == ["pane", "close"]] == ["w1:p9"]
    assert sum(1 for c in calls if c[1:3] == ["pane", "split"]) == 2
    assert sum(1 for c in calls if c[1:3] == ["pane", "run"]) == 2


def test_publish_opens_a_new_pane_when_the_old_one_was_closed(tmp_path):
    calls = []
    kw = dict(home=tmp_path, environ={"HERDR_PANE_ID": "w1:p1"}, run=_fake_herdr(calls, alive=False),
              load_config=lambda: _Cfg, post=lambda cfg, body: (200, {}))
    sr.publish_report("claude", "abc-123", REPORT, **kw)
    sr.publish_report("claude", "abc-123", REPORT, **kw)
    assert not any(c[1:3] == ["pane", "close"] for c in calls)
    assert sum(1 for c in calls if c[1:3] == ["pane", "split"]) == 2


class _Tty:
    def __init__(self, tty):
        self.tty, self.text = tty, ""

    def isatty(self):
        return self.tty

    def write(self, s):
        self.text += s


def _closes(calls):
    return [c for c in calls if c[1:3] == ["pane", "close"]]


def _recorder(calls, fail_on=None):
    def run(argv, **kw):
        calls.append(argv)
        if fail_on and argv[-1] == fail_on:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such pane")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return run


def test_view_renders_through_the_viewer_loop_then_closes_its_pane_and_its_peer(tmp_path):
    f = tmp_path / "r.md"
    f.write_text("# Title\n\n" + "Some **bold** text that is long enough to need wrapping at a narrow width. " * 3)
    calls, drawn = [], {}

    def viewer(render, **kw):
        drawn["lines"], drawn["kw"] = render(50, 20), kw
        return 0
    assert sr.view_report(f, close_pane="w1:p9", peer_pane="w1:pA", run=_recorder(calls), out=_Tty(True), viewer=viewer) == 0
    assert drawn["kw"]["scroll"] is True
    text = "\n".join(drawn["lines"])
    assert "**" not in text and "# " not in text and "\x1b[" in text
    assert all(visible_width(line) <= 50 for line in drawn["lines"])
    assert [c[3] for c in _closes(calls)] == ["w1:pA", "w1:p9"]          # the peer first: closing our own pane may end this process


def test_view_honours_no_color(tmp_path, monkeypatch):
    f = tmp_path / "r.md"
    f.write_text("# Title\n\n**bold**\n")
    monkeypatch.setenv("NO_COLOR", "1")
    drawn = {}
    sr.view_report(f, run=_recorder([]), out=_Tty(True), viewer=lambda render, **kw: drawn.update(l=render(60, 10)) or 0)
    assert not any("\x1b" in line for line in drawn["l"])


def test_view_still_closes_both_panes_when_the_viewer_fails_or_is_interrupted(tmp_path):
    f = tmp_path / "r.md"
    f.write_text("# Title\n")
    for exc in (KeyboardInterrupt, RuntimeError):
        calls = []

        def viewer(render, **kw):
            raise exc
        assert sr.view_report(f, close_pane="w1:p9", peer_pane="w1:pA", run=_recorder(calls), out=_Tty(True), viewer=viewer) == 1
        assert [c[3] for c in _closes(calls)] == ["w1:pA", "w1:p9"]


def test_a_peer_pane_that_is_already_gone_is_not_an_error(tmp_path):
    f = tmp_path / "r.md"
    f.write_text("# Title\n")
    calls = []
    assert sr.view_report(f, close_pane="w1:p9", peer_pane="w1:pA", run=_recorder(calls, fail_on="w1:pA"),
                          out=_Tty(True), viewer=lambda render, **kw: 0) == 0
    assert [c[3] for c in _closes(calls)] == ["w1:pA", "w1:p9"]

    def exploding(argv, **kw):
        raise FileNotFoundError("herdr")
    assert sr.view_report(f, close_pane="w1:p9", peer_pane="w1:pA", run=exploding, out=_Tty(True),
                          viewer=lambda render, **kw: 0) == 0


def test_view_without_a_terminal_prints_and_does_not_start_the_viewer(tmp_path):
    f = tmp_path / "r.md"
    f.write_text("# Title\n")
    out, calls = _Tty(False), []

    def viewer(render, **kw):
        raise AssertionError("no terminal: the viewer must not start")
    assert sr.view_report(f, run=_recorder(calls), out=out, viewer=viewer) == 0
    assert "Title" in out.text and calls == []


def test_view_closes_its_panes_even_if_the_report_is_missing(tmp_path):
    calls = []
    assert sr.view_report(tmp_path / "gone.md", close_pane="w1:p9", peer_pane="w1:pA", run=_recorder(calls),
                          out=_Tty(True), viewer=lambda render, **kw: 0) == 1
    assert [c[3] for c in _closes(calls)] == ["w1:pA", "w1:p9"]


GRAPH = {"total_prompts": 12, "drift": {"at": 6, "note": "web page task"}, "segments": [
    {"label": "USB stick", "from_prompt": 1, "to_prompt": 5, "count": 5, "start": "2026-01-01T09:00:00Z", "agents": None},
    {"label": "web page", "from_prompt": 6, "to_prompt": 12, "count": 7, "start": "2026-01-01T10:00:00Z", "agents": 2}]}


def _with_sidecar(tmp_path, graph=GRAPH):
    f = tmp_path / "claude-s1.md"
    f.write_text("# Session summary\n")
    (tmp_path / "claude-s1.md.graph.json").write_text(json.dumps(graph))
    return f


def test_graph_viewer_draws_the_sidecar_and_closes_itself_then_its_peer(tmp_path):
    f, calls, drawn = _with_sidecar(tmp_path), [], {}

    def viewer(render, **kw):
        drawn["lines"], drawn["kw"] = render(90, 12), kw
        return 0
    assert sr.view_graph(f, close_pane="w1:pB", peer_pane="w1:p9", run=_recorder(calls), out=_Tty(True), viewer=viewer) == 0
    text = "\n".join(drawn["lines"])
    assert "USB stick" in text and "web page" in text and "Looks like a new task" in text
    assert drawn["kw"]["scroll"] is True                                   # a column graph can be taller than its pane
    assert [c[3] for c in _closes(calls)] == ["w1:p9", "w1:pB"]


def test_graph_viewer_follows_the_pane_width_and_output_encoding(tmp_path):
    f = _with_sidecar(tmp_path)
    ascii_out, drawn = _Tty(True), {}
    ascii_out.encoding = "ascii"
    sr.view_graph(f, run=_recorder([]), out=ascii_out, viewer=lambda render, **kw: drawn.update(wide=render(120, 20), narrow=render(32, 40)) or 0)
    assert all(line.isascii() for line in drawn["wide"] + drawn["narrow"])
    assert all(visible_width(line) <= 32 for line in drawn["narrow"])


@pytest.mark.parametrize("content", [None, "not json", "[]"])
def test_graph_viewer_without_a_usable_sidecar_closes_and_returns_1(tmp_path, content):
    f = tmp_path / "claude-s1.md"
    f.write_text("# Session summary\n")
    if content is not None:
        (tmp_path / "claude-s1.md.graph.json").write_text(content)
    calls = []
    assert sr.view_graph(f, close_pane="w1:pB", peer_pane="w1:p9", run=_recorder(calls), out=_Tty(True),
                         viewer=lambda render, **kw: 0) == 1
    assert [c[3] for c in _closes(calls)] == ["w1:p9", "w1:pB"]


def test_graph_viewer_without_a_terminal_prints_the_graph(tmp_path):
    out = _Tty(False)
    assert sr.view_graph(_with_sidecar(tmp_path), run=_recorder([]), out=out) == 0
    assert "USB stick" in out.text


def test_cli_graph_and_view_accept_a_peer(tmp_path):
    f = _with_sidecar(tmp_path)
    for argv in (["summary", "graph", str(f), "--peer", "none"], ["summary", "view", str(f), "--peer", "none"]):
        done = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", *argv], capture_output=True, text=True,
                              timeout=60, env={**os.environ, "PATH": "/nonexistent"})
        assert done.returncode == 0, done.stderr
    assert "USB stick" in subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "summary", "graph", str(f)],
                                         capture_output=True, text=True, timeout=60).stdout
def test_publish_one_destination_failing_still_reports_the_other(tmp_path):
    out = sr.publish_report("claude", "abc-123", REPORT, home=tmp_path, environ={}, run=_fake_herdr([]),
                            load_config=lambda: _Cfg, post=lambda cfg, body: (200, {}))
    assert out["ok"] and out["pane"]["error"] == "not_in_a_herdr_pane" and "dashboard" in out["message"] and "pane" not in out["message"]
    out = sr.publish_report("claude", "abc-123", REPORT, home=tmp_path, environ={"HERDR_PANE_ID": "w1:p1"},
                            run=_fake_herdr([]), load_config=lambda: _Cfg, post=lambda cfg, body: (404, {"reason": "no_session"}))
    assert out["ok"] and out["dashboard"]["error"] == "no_session" and "side pane" in out["message"]


def test_publish_with_nowhere_to_put_it_asks_the_caller_to_print_it(tmp_path):
    def down(cfg, body):
        raise OSError("offline")
    out = sr.publish_report("claude", "abc-123", REPORT, home=tmp_path, environ={}, run=_fake_herdr([]),
                            load_config=lambda: _Cfg, post=down)
    assert out["ok"] is False and out["message"] == ""


def test_publish_refuses_a_non_report_and_bad_ids(tmp_path):
    with pytest.raises(sr.ReportError) as err:
        sr.publish_report("claude", "abc-123", "hello", home=tmp_path)
    assert err.value.code == "malformed_report"
    with pytest.raises(sr.ReportError):
        sr.publish_report("claude", "../x", REPORT, home=tmp_path)
    assert not (tmp_path / ".cache").exists() or not list((tmp_path / ".cache").rglob("*.md"))


# -- publishing the drift graph ------------------------------------------------------------------
BLOCK = json.dumps({"segments": [{"label": "USB stick", "from_prompt": 1, "to_prompt": 3},
                                  {"label": "web page", "from_prompt": 4, "to_prompt": 6}],
                    "drift_at": 4, "drift_note": "a new task"})
WITH_BLOCK = REPORT + "\n```drift-graph\n" + BLOCK + "\n```\n"


def _history(n=6, boom=None):
    def history(agent, session_id):
        if boom:
            raise boom
        return {"prompts": [{"n": i, "ts": f"2026-01-01T09:0{i}:00Z"} for i in range(1, n + 1)],
                "prompts_total": n, "dispatch_ts": []}
    return history


def _graph_fake(calls, alive=True):
    """Like ``_fake_herdr`` but hands out a new pane id per split, so the summary and graph panes differ."""
    ids = iter(["w1:p9", "w1:pA", "w1:pB", "w1:pC"])

    def run(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["pane", "split"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": {"pane": {"pane_id": next(ids)}}}), stderr="")
        if argv[1:3] == ["pane", "get"]:
            return subprocess.CompletedProcess(argv, 0 if alive else 1, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
    return run


def _publish(tmp_path, calls, text=WITH_BLOCK, history=None, posted=None, **kw):
    posted = posted if posted is not None else []
    return sr.publish_report("claude", "abc-123", text, home=tmp_path, environ={"HERDR_PANE_ID": "w1:p1"},
                             run=_graph_fake(calls, kw.pop("alive", True)), load_config=lambda: _Cfg,
                             post=lambda cfg, body: posted.append(body) or (200, {}),
                             history=history or _history(), **kw)


def _runs(calls):
    return [c for c in calls if c[1:3] == ["pane", "run"]]


def test_publish_strips_the_block_before_storing_posting_and_rendering(tmp_path):
    calls, posted = [], []
    out = _publish(tmp_path, calls, posted=posted)
    assert "drift-graph" not in Path(out["file"]).read_text() and "drift-graph" not in posted[0]["report"]
    assert posted[0]["report"].startswith("# Session summary") and "Evidence coverage" in posted[0]["report"]
    assert out["message"] == "Session summary published to the dashboard Summary view and a side pane; it is not repeated here."


def test_publish_writes_sidecar_and_opens_graph_pane_below(tmp_path):
    calls = []
    out = _publish(tmp_path, calls)
    side = Path(out["file"] + ".graph.json")
    graph = json.loads(side.read_text())
    assert [s["label"] for s in graph["segments"]] == ["USB stick", "web page"] and graph["drift"]["at"] == 4
    assert (side.stat().st_mode & 0o777) == 0o600
    assert out["graph"] == {"ok": True, "pane_id": "w1:pA"}
    splits = [c for c in calls if c[1:3] == ["pane", "split"]]
    assert splits[0][splits[0].index("--direction") + 1] == "right"
    down = splits[1]
    assert down[down.index("--pane") + 1] == "w1:p9" and down[down.index("--direction") + 1] == "down" and "--no-focus" in down
    summary_run, graph_run = _runs(calls)
    assert summary_run[3] == "w1:p9" and "view" in summary_run and out["file"] in summary_run
    assert summary_run[-4:] == ["--close-pane", "w1:p9", "--peer", "w1:pA"]
    assert graph_run[3] == "w1:pA" and graph_run[-4:] == ["--close-pane", "w1:pA", "--peer", "w1:p9"]
    assert "graph" in graph_run and out["file"] in graph_run


@pytest.mark.parametrize("block", ["not json", json.dumps({"segments": [{"label": "x", "from_prompt": 1, "to_prompt": 2}]})])
def test_invalid_block_still_publishes_without_graph_pane(tmp_path, block):
    calls = []
    out = _publish(tmp_path, calls, text=REPORT + "\n```drift-graph\n" + block + "\n```\n")
    assert out["ok"] and out["pane"]["ok"] and out["graph"]["ok"] is False
    assert sum(1 for c in calls if c[1:3] == ["pane", "split"]) == 1
    assert len(_runs(calls)) == 1 and "--peer" not in _runs(calls)[0]
    assert not Path(out["file"] + ".graph.json").exists()


def test_history_failure_is_soft(tmp_path):
    calls = []
    out = _publish(tmp_path, calls, history=_history(boom=RuntimeError("boom")))
    assert out["ok"] and out["pane"]["ok"] and out["graph"]["ok"] is False and out["graph"]["error"] == "RuntimeError"
    assert len(_runs(calls)) == 1 and not Path(out["file"] + ".graph.json").exists()


def test_republish_closes_both_old_panes_first(tmp_path):
    calls = []
    _publish(tmp_path, calls)
    assert not any(c[1:3] == ["pane", "close"] for c in calls)
    _publish(tmp_path, calls)
    assert sorted(c[3] for c in calls if c[1:3] == ["pane", "close"]) == ["w1:p9", "w1:pA"]
    closes = [i for i, c in enumerate(calls) if c[1:3] == ["pane", "close"]]
    second_split = [i for i, c in enumerate(calls) if c[1:3] == ["pane", "split"]][2]
    assert max(closes) < second_split                                    # old pair gone before the new one opens


def test_no_pane_flag_skips_the_graph_pane(tmp_path):
    calls = []
    out = _publish(tmp_path, calls, pane=False)
    assert calls == [] and out["graph"] == {"ok": False, "skipped": True}
    assert not Path(out["file"] + ".graph.json").exists()


def test_fewer_than_five_prompts_opens_no_graph_pane(tmp_path):
    calls = []
    out = _publish(tmp_path, calls, history=_history(n=4))
    assert out["graph"]["ok"] is False and sum(1 for c in calls if c[1:3] == ["pane", "split"]) == 1
    assert not Path(out["file"] + ".graph.json").exists()


def test_sidecar_removed_when_a_later_publish_has_no_graph(tmp_path):
    calls = []
    first = _publish(tmp_path, calls)
    assert Path(first["file"] + ".graph.json").exists()
    second = _publish(tmp_path, calls, text=REPORT)
    assert second["graph"]["ok"] is False and not Path(second["file"] + ".graph.json").exists()


def test_panes_inherit_the_publishers_pythonpath_so_the_viewer_matches_the_publishing_code(tmp_path):
    calls = []
    sr.publish_report("claude", "abc-123", WITH_BLOCK, home=tmp_path,
                      environ={"HERDR_PANE_ID": "w1:p1", "PYTHONPATH": "/work/tree"}, run=_graph_fake(calls),
                      load_config=lambda: _Cfg, post=lambda cfg, body: (200, {}), history=_history())
    splits = [c for c in calls if c[1:3] == ["pane", "split"]]
    assert len(splits) == 2
    for split in splits:
        assert split[split.index("--env") + 1] == "PYTHONPATH=/work/tree"
    calls.clear()
    _publish(tmp_path, calls)                                  # environ without PYTHONPATH: no --env at all
    assert not any("--env" in c for c in calls)


def test_sidecar_carries_time_and_token_metrics_from_the_history(tmp_path):
    def history(agent, session_id):
        data = _history()(agent, session_id)
        data.update(activity_ts=[f"2026-01-01T09:0{i}:00Z" for i in range(1, 7)], child_records=2,
                    usage=[{"ts": "2026-01-01T09:01:30Z", "fresh": 100, "cached": 900},
                           {"ts": "2026-01-01T09:05:30Z", "fresh": 40, "cached": 10}])
        return data
    out = _publish(tmp_path, [], history=history)
    graph = json.loads(Path(out["file"] + ".graph.json").read_text())
    assert [s["fresh"] for s in graph["segments"]] == [100, 40]
    assert graph["segments"][0]["worked_s"] == 180 and graph["drift"]["fresh"] == 40
    assert graph["totals"]["fresh"] == 140 and graph["totals"]["main_only"] is True


def test_a_history_without_metrics_still_publishes_the_graph(tmp_path):
    out = _publish(tmp_path, [])
    graph = json.loads(Path(out["file"] + ".graph.json").read_text())
    assert out["graph"]["ok"] is True and graph["segments"][0]["fresh"] is None and graph["totals"]["worked_s"] is None
