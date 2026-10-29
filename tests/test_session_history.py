"""Read-only session-history helper across Claude Code, Pi, Codex and OpenCode."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from herdr_engineering import session_history as sh


def _jl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _tree(root: Path):
    return {str(p): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}


def claude_fixture(home, cwd="/work/proj", sid="c1"):
    key = cwd.replace("/", "-")
    rows = [
        {"type": "user", "timestamp": "t1", "message": {"content": "goal A: add retries"}},
        {"type": "assistant", "timestamp": "t2", "message": {"content": [
            {"type": "text", "text": "Plan: edit client.py. Tests passed."},
            {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "timestamp": "t3", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "is_error": True, "content": "1 failed"}]}},
        {"type": "user", "timestamp": "t4", "message": {"content": "actually goal B: add caching"}},
        {"type": "assistant", "timestamp": "t5", "message": {"content": [
            {"type": "tool_use", "id": "u2", "name": "Task", "input": {"description": "review cache"}}]}},
        {"type": "assistant", "timestamp": "t6", "isSidechain": True, "message": {"content": "child work"}},
        {"type": "user", "timestamp": "t7", "isCompactSummary": True, "message": {"content": "summary of earlier"}},
        {"type": "user", "timestamp": "t8", "message": {"content": "sk-ant-api03-SECRETSECRETSECRETSECRET1234"}},
    ]
    return _jl(home / ".claude/projects" / key / f"{sid}.jsonl", rows)


def test_claude_goal_change_failure_child_compaction_and_redaction(tmp_path):
    claude_fixture(tmp_path)
    res = sh.resolve(agent="claude", session="c1", home=tmp_path)
    out = sh.read_history(res, home=tmp_path)
    kinds = [e["kind"] for e in out["events"]]
    assert kinds.count("user") == 3 and "child_dispatch" in kinds and "compaction" in kinds
    texts = " ".join(e["text"] for e in out["events"])
    assert "goal A" in texts and "goal B" in texts
    assert "SECRETSECRET" not in texts
    failed = [e for e in out["events"] if e["kind"] == "tool_result"]
    assert failed and failed[0]["ok"] is False  # "Tests passed" claim has a failing result
    assert out["child_records"] == 1
    # Pre-compaction turns and the sidechain record are both readable, so nothing is missing.
    assert out["coverage"] == {"verdict": "complete", "gaps": []}


def test_child_dispatch_without_child_record_is_partial(tmp_path):
    rows = [claude_fixture(tmp_path, sid="tmpl")]
    lines = [json.loads(l) for l in rows[0].read_text().splitlines() if '"isSidechain"' not in l]
    _jl(tmp_path / ".claude/projects/-w/nochild.jsonl", lines)
    out = sh.read_history(sh.resolve(agent="claude", session="nochild", home=tmp_path), home=tmp_path)
    assert out["coverage"]["verdict"] == "partial"
    assert any("child-agent" in g for g in out["coverage"]["gaps"])


def test_pi_compaction_without_earlier_turns_is_flagged(tmp_path):
    d = tmp_path / ".pi/agent/sessions/--work-proj--"
    _jl(d / "2026_s1.jsonl", [
        {"type": "session", "id": "s1", "cwd": "/work/proj"},
        {"type": "compaction", "summary": "earlier work", "tokensBefore": 9000, "timestamp": "t0"},
        {"type": "message", "timestamp": "t1", "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]}},
        {"type": "message", "timestamp": "t2", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "a", "name": "bash", "arguments": {"command": "npm test"}}]}},
        {"type": "message", "timestamp": "t3", "message": {"role": "toolResult", "toolCallId": "a", "toolName": "bash",
                                                          "isError": False, "content": [{"type": "text", "text": "ok 12"}]}},
    ])
    res = sh.resolve(agent="pi", session="s1", home=tmp_path)
    out = sh.read_history(res, home=tmp_path)
    assert out["compactions"] == 1
    assert out["coverage"]["verdict"] == "partial"
    assert any("before compaction" in g for g in out["coverage"]["gaps"])
    assert [e["ok"] for e in out["events"] if e["kind"] == "tool_result"] == [True]


def test_codex_child_messages_marked_final_only(tmp_path):
    p = tmp_path / ".codex/sessions/2026/10/28/rollout-x-c9.jsonl"
    _jl(p, [
        {"type": "response_item", "timestamp": "t1", "payload": {"type": "message", "role": "user",
                                                                  "content": [{"type": "input_text", "text": "do it"}]}},
        {"type": "response_item", "timestamp": "t2", "payload": {"type": "function_call", "name": "shell",
                                                                  "arguments": "{\"command\":[\"ls\"]}"}},
        {"type": "response_item", "timestamp": "t3", "payload": {"type": "function_call_output",
                                                                  "output": "{\"output\":\"a\",\"metadata\":{\"exit_code\":0}}"}},
        {"type": "response_item", "timestamp": "t4", "payload": {"type": "agent_message", "message": "child done"}},
    ])
    out = sh.read_history(sh.resolve(agent="codex", session="c9", home=tmp_path), home=tmp_path)
    assert out["child_records"] == 1
    assert any("child-agent" in g for g in out["coverage"]["gaps"])
    assert out["coverage"]["verdict"] == "partial"


def test_opencode_sqlite_read_only_with_child_session(tmp_path):
    db = tmp_path / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript("""create table session(id text primary key, parent_id text, time_updated integer);
        create table message(id text primary key, session_id text, time_created integer, data text);
        create table part(id text primary key, message_id text, session_id text, time_created integer, data text);""")
    con.execute("insert into session values('s1',null,2)")
    con.execute("insert into session values('kid','s1',3)")
    con.execute("insert into message values('m1','s1',1,?)", (json.dumps({"role": "user"}),))
    con.execute("insert into message values('m2','s1',2,?)", (json.dumps({"role": "assistant"}),))
    con.execute("insert into part values('p1','m1','s1',1,?)", (json.dumps({"type": "text", "text": "hello"}),))
    con.execute("insert into part values('p2','m2','s1',2,?)", (json.dumps(
        {"type": "tool", "tool": "bash", "state": {"status": "completed", "input": {"command": "make"}, "output": "done"}}),))
    con.commit(); con.close()
    before = db.read_bytes()
    out = sh.read_history(sh.resolve(agent="opencode", session="s1", home=tmp_path), home=tmp_path)
    assert [e["kind"] for e in out["events"]] == ["user", "tool_call", "tool_result"]
    assert out["child_records"] == 1 and out["coverage"]["verdict"] == "complete"
    assert db.read_bytes() == before


def test_empty_and_missing_sessions_do_not_hallucinate(tmp_path):
    out = sh.read_history(sh.resolve(agent="claude", cwd="/nowhere", home=tmp_path, env={}), home=tmp_path)
    assert out["events"] == [] and out["coverage"]["verdict"] == "limited" and out["coverage"]["gaps"]
    _jl(tmp_path / ".claude/projects/-w/e1.jsonl", [{"type": "summary"}])
    out = sh.read_history(sh.resolve(agent="claude", session="e1", home=tmp_path), home=tmp_path)
    assert out["events"] == [] and out["coverage"]["verdict"] == "limited"


def test_directory_heuristic_is_labelled_and_never_complete(tmp_path):
    claude_fixture(tmp_path, cwd="/work/proj", sid="a")
    claude_fixture(tmp_path, cwd="/work/proj", sid="b")
    res = sh.resolve(agent="claude", cwd="/work/proj", home=tmp_path, env={})
    assert res.method.startswith("newest-in-cwd") and len(res.candidates) == 2
    out = sh.read_history(res, home=tmp_path)
    assert out["coverage"]["verdict"] == "limited"
    assert any("heuristic" in g for g in out["coverage"]["gaps"])


def test_herdr_pane_metadata_wins_over_heuristic(tmp_path):
    claude_fixture(tmp_path, sid="pane-sid")
    claude_fixture(tmp_path, sid="newer")
    fake = tmp_path / "herdr"
    fake.write_text("#!/bin/sh\necho '{\"result\":{\"pane\":{\"agent_session\":{\"agent\":\"claude\",\"kind\":\"id\",\"value\":\"pane-sid\"}}}}'\n")
    fake.chmod(0o755)
    res = sh.resolve(cwd="/work/proj", home=tmp_path, env={"HERDR_PANE_ID": "p1"}, herdr_bin=str(fake))
    assert res.method == "herdr-pane" and res.session_id == "pane-sid"
    assert sh.read_history(res, home=tmp_path)["coverage"]["verdict"] == "complete"


def test_history_command_is_side_effect_free(tmp_path):
    claude_fixture(tmp_path)
    before = _tree(tmp_path)
    for _ in range(2):
        r = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "summary", "history", "--agent", "claude",
                            "--session", "c1", "--home", str(tmp_path)],
                           capture_output=True, text=True, cwd=str(tmp_path),
                           env={"PYTHONPATH": str(Path(sh.__file__).parents[1]), "PATH": "/usr/bin:/bin",
                                "HERDR_ENGINEERING_STATE": str(tmp_path / "state")})
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["read_only"] is True and data["agent"] == "claude"
    assert _tree(tmp_path) == before
    assert not (tmp_path / "state").exists()


def test_middle_of_long_sessions_is_elided_and_disclosed(tmp_path):
    rows = [{"type": "user", "timestamp": f"t{i}", "message": {"content": f"m{i}"}} for i in range(500)]
    _jl(tmp_path / ".claude/projects/-w/long.jsonl", rows)
    out = sh.read_history(sh.resolve(agent="claude", session="long", home=tmp_path), home=tmp_path, max_events=100)
    assert out["events_total"] == 500 and out["events_omitted_from_middle"] == 400 and len(out["events"]) == 100


# --- idle Codex panes: Herdr reports no conversation, Codex's own process log does ---

def _codex_idle_fixture(tmp_path, *, started=1000, rows=None, header=None, pid=4242):
    home = tmp_path / "home"
    thread = "01a1795b-b6bc-7ff0-aa04-61b9bf926077"
    rollout = home / f".codex/sessions/2026/10/26/rollout-2026-10-26T10-56-56-{thread}.jsonl"
    _jl(rollout, [header or {"type": "session_meta", "payload": {"id": thread, "source": "cli"}},
                  {"type": "response_item", "timestamp": "t1", "payload": {"type": "message", "role": "user",
                                                                           "content": [{"type": "input_text", "text": "do it"}]}}])
    db = home / ".codex/logs_2.sqlite"
    con = sqlite3.connect(db)
    con.execute("create table logs (id integer primary key autoincrement, ts integer, thread_id text, process_uuid text)")
    for ts, tid, owner in (rows if rows is not None else [(1500, thread, f"pid:{pid}:u")]):
        con.execute("insert into logs (ts, thread_id, process_uuid) values (?,?,?)", (ts, tid, owner))
    con.commit(); con.close()
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    (proc / "stat").write_text("cpu 1\nbtime 900\n")
    (proc / str(pid) / "stat").write_text(f"{pid} (codex) S " + " ".join(["0"] * 18) + f" {round((started - 900) * 100)} 0")
    fake = tmp_path / "herdr"
    fake.write_text("#!/bin/sh\ncase \"$2\" in\n"
                    "get) echo '{\"result\":{\"pane\":{\"agent\":\"codex\",\"agent_session\":null}}}';;\n"
                    f"process-info) echo '{{\"result\":{{\"process_info\":{{\"foreground_processes\":[{{\"pid\":{pid}}}]}}}}}}';;\n"
                    "esac\n")
    fake.chmod(0o755)
    return home, proc, fake, rollout, thread


def test_idle_codex_pane_resolves_from_the_process_log(tmp_path):
    home, proc, fake, rollout, thread = _codex_idle_fixture(tmp_path)
    res = sh.resolve(home=home, env={"HERDR_PANE_ID": "w1:p2"}, herdr_bin=str(fake), cwd="/elsewhere", proc=proc)
    assert res.agent == "codex" and res.path == str(rollout) and res.session_id == thread
    assert res.method == "herdr-pane (codex process log)"
    out = sh.read_history(res, home=home)
    assert out["events_total"] == 1 and out["coverage"]["verdict"] == "complete"


def test_idle_codex_reused_pid_is_not_evidence(tmp_path):
    home, proc, fake, rollout, thread = _codex_idle_fixture(tmp_path, started=2000)
    res = sh.resolve(home=home, env={"HERDR_PANE_ID": "w1:p2"}, herdr_bin=str(fake), proc=proc)
    assert res.path is None and "did not identify" in res.note
    out = sh.read_history(res, home=home)
    assert out["events"] == [] and out["coverage"]["verdict"] == "limited"


def test_idle_codex_helper_thread_and_ties_are_not_guessed(tmp_path):
    sub = {"type": "session_meta", "payload": {"id": "01a1795b-b6bc-7ff0-aa04-61b9bf926077",
                                               "source": {"subagent": {"thread_spawn": {}}}}}
    home, proc, fake, rollout, thread = _codex_idle_fixture(tmp_path, header=sub)
    assert sh.resolve(home=home, env={"HERDR_PANE_ID": "w1:p2"}, herdr_bin=str(fake), proc=proc).path is None
    other = "01a1795b-aaaa-7ff0-aa04-61b9bf926077"
    _jl(home / f".codex/sessions/2026/10/26/rollout-2026-10-26T11-00-00-{other}.jsonl",
        [{"type": "session_meta", "payload": {"id": other, "source": "cli"}}])
    home2, proc2, fake2, rollout2, thread2 = _codex_idle_fixture(
        tmp_path / "t2", rows=[(1500, "01a1795b-b6bc-7ff0-aa04-61b9bf926077", "pid:4242:u"), (1500, other, "pid:4242:u")])
    _jl(home2 / f".codex/sessions/2026/10/26/rollout-2026-10-26T11-00-00-{other}.jsonl",
        [{"type": "session_meta", "payload": {"id": other, "source": "cli"}}])
    assert sh.resolve(home=home2, env={"HERDR_PANE_ID": "w1:p2"}, herdr_bin=str(fake2), proc=proc2).path is None




def test_history_reports_the_cwd_recorded_in_the_session(tmp_path):
    rows = [{"type": "user", "timestamp": "t1", "cwd": "/work/proj", "message": {"content": "hi"}}]
    _jl(tmp_path / ".claude/projects/-work-proj/cwd1.jsonl", rows)
    out = sh.read_history(sh.resolve(agent="claude", session="cwd1", home=tmp_path), home=tmp_path)
    assert out["cwd"] == "/work/proj"


def test_history_cwd_is_null_when_the_record_names_none(tmp_path):
    claude_fixture(tmp_path)
    out = sh.read_history(sh.resolve(agent="claude", session="c1", home=tmp_path), home=tmp_path)
    assert out["cwd"] is None


def test_session_id_without_agent_searches_every_store(tmp_path):
    claude_fixture(tmp_path, sid="only-here")
    res = sh.resolve(session="only-here", home=tmp_path, env={}, cwd="/somewhere/else")
    assert (res.agent, res.method) == ("claude", "explicit")
