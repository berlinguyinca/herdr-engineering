"""The corrections recorder: deterministic extraction, opt-in storage, read-only scanning of real transcript shapes."""
import json
import os

import pytest

from herdr_engineering import corrections as cx


def ev(kind, text="", **extra):
    return {"kind": kind, "text": text, "ts": "2026-11-01T10:00:00Z", **extra}


def kinds(events):
    return [c.kind for c in cx.extract(events, agent="claude", session_id="s1")]


def test_a_negation_after_an_agent_turn_is_a_correction():
    assert kinds([ev("user", "add a cache"), ev("assistant", "done, added Redis"), ev("user", "No, use a dict instead")]) == ["negation"]
    assert kinds([ev("user", "wrong file, the other one")]) == []        # nothing to correct yet
    assert kinds([ev("assistant", "ok"), ev("user", "nothing wrong here, thanks")]) == []        # not a leading negation
    assert kinds([ev("assistant", "ok"), ev("user", "Not now, later")]) == []                    # 'not now' is not 'not that'


def test_a_near_duplicate_ask_is_a_reask_but_a_short_or_distant_one_is_not():
    q = "please rename the helper function in parser.py to parse_line"
    assert kinds([ev("user", q), ev("assistant", "done"), ev("user", "rename the helper function in parser.py to parse_line please")]) == ["reask"]
    assert kinds([ev("user", "fix it"), ev("assistant", "x"), ev("user", "fix it")]) == []
    far = [ev("user", q)] + [ev("assistant", "x")] * (cx.REASK_WINDOW + 2) + [ev("user", q)]
    assert kinds(far) == []


def test_interrupts_and_rejected_tools_are_found():
    assert kinds([ev("assistant", "running"), ev("user", "[Request interrupted by user]")]) == ["interrupt"]
    got = cx.extract([ev("assistant", "I will delete it"), ev("tool_result", "The user doesn't want to proceed with this tool use.",
                                                              tool="Bash", ok=False)], agent="claude", session_id="s1")
    assert [c.kind for c in got] == ["rejected_tool"] and got[0].context.startswith("Bash: I will delete it")
    assert kinds([ev("tool_result", "doesn't want to proceed", tool="Bash", ok=True)]) == []     # a success is not a rejection


def test_ids_are_stable_so_a_second_scan_records_nothing_new():
    events = [ev("assistant", "done"), ev("user", "No, revert that")]
    a, b = cx.extract(events, agent="codex", session_id="s"), cx.extract(events, agent="codex", session_id="s")
    assert [c.id for c in a] == [c.id for c in b] and len({c.id for c in a}) == 1
    assert cx.extract(events, agent="claude", session_id="s")[0].id != a[0].id


def test_excerpts_are_bounded():
    long = "No " + "x" * 1000
    (c,) = cx.extract([ev("assistant", "y" * 1000), ev("user", long)], agent="pi", session_id="s")
    assert len(c.excerpt) == cx.EXCERPT and len(c.context) == cx.CONTEXT


@pytest.fixture()
def log(tmp_path):
    return cx.CorrectionLog(tmp_path / "state")


def corr(i=0, session="s1"):
    return cx.extract([ev("assistant", "done"), ev("user", f"No, redo {i}")], agent="claude", session_id=session)


def test_recording_refuses_until_enabled_and_files_are_private(log):
    with pytest.raises(PermissionError):
        log.record(corr())
    log.enable()
    assert log.record(corr()) == 1
    assert oct(os.stat(log.path).st_mode & 0o777) == "0o600" and oct(os.stat(log.dir).st_mode & 0o777) == "0o700"


def test_record_is_idempotent_and_disable_keeps_what_was_kept_but_blocks_more(log):
    log.enable()
    assert (log.record(corr()), log.record(corr())) == (1, 0)
    log.disable()
    assert len(log.read()) == 1
    with pytest.raises(PermissionError):
        log.record(corr(1))


def test_forget_one_session_or_everything_even_while_disabled(log):
    log.enable()
    log.record(corr(0, "s1") + corr(1, "s2"))
    log.disable()
    assert log.forget(session_id="s1") == 1 and [r["session_id"] for r in log.read()] == ["s2"]
    assert log.forget(session_id="nope") == 0
    with pytest.raises(ValueError):
        log.forget()                                    # naming nothing never wipes the log
    assert log.stats()["total"] == 1
    assert log.forget(everything=True) == 1 and not log.path.exists()


def test_stats_count_by_agent_and_kind(log):
    log.enable()
    log.record(corr(0) + cx.extract([ev("assistant", "x"), ev("user", "[Request interrupted by user]")], agent="codex", session_id="c"))
    assert log.stats() == {"enabled": True, "total": 2, "by_agent": {"claude": 1, "codex": 1},
                           "by_kind": {"interrupt": 1, "negation": 1}, "sessions": 2}


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_scan_reads_a_claude_transcript_read_only_and_records(log, tmp_path):
    home = tmp_path / "home"
    f = home / ".claude/projects/-w/abc123.jsonl"
    write(f, [
        {"type": "user", "timestamp": "t1", "message": {"content": "add a cache"}},
        {"type": "assistant", "timestamp": "t2", "message": {"content": [{"type": "text", "text": "added Redis"}]}},
        {"type": "user", "timestamp": "t3", "message": {"content": "No, use a dict"}},
    ])
    before = f.read_bytes()
    log.enable()
    out = cx.scan_session(log, agent="claude", session="abc123", home=home)
    assert (out["agent"], out["session_id"], out["found"], out["new"]) == ("claude", "abc123", 1, 1)
    assert cx.scan_session(log, agent="claude", session="abc123", home=home)["new"] == 0
    assert f.read_bytes() == before


def test_scan_reads_codex_and_pi_shapes(log, tmp_path):
    home = tmp_path / "home"
    write(home / ".codex/sessions/2026/rollout-cx1.jsonl", [
        {"type": "response_item", "timestamp": "t", "payload": {"type": "message", "role": "assistant",
                                                                 "content": [{"type": "output_text", "text": "done"}]}},
        {"type": "response_item", "timestamp": "t", "payload": {"type": "message", "role": "user",
                                                                 "content": [{"type": "input_text", "text": "Actually revert it"}]}}])
    write(home / ".pi/agent/sessions/--w--/pi1.jsonl", [
        {"type": "message", "timestamp": "t", "message": {"role": "assistant", "content": "done"}},
        {"type": "message", "timestamp": "t", "message": {"role": "user", "content": "Don't do that"}}])
    log.enable()
    assert cx.scan_session(log, agent="codex", session="cx1", home=home)["new"] == 1
    assert cx.scan_session(log, agent="pi", session="pi1", home=home)["new"] == 1
    assert log.stats()["by_agent"] == {"codex": 1, "pi": 1}


def test_scan_refuses_while_disabled_and_reports_a_missing_session(log, tmp_path):
    with pytest.raises(PermissionError):
        cx.scan_session(log, agent="claude", session="x", home=tmp_path)
    log.enable()
    out = cx.scan_session(log, agent="claude", session="nope", home=tmp_path)
    assert out["found"] == 0 and out["coverage"]["gaps"]


def test_cli_enable_scan_list_forget_round_trip(tmp_path, monkeypatch, capsys):
    from herdr_engineering import cli
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    home = tmp_path / "home"
    write(home / ".claude/projects/-w/abc.jsonl", [
        {"type": "assistant", "timestamp": "t", "message": {"content": [{"type": "text", "text": "done"}]}},
        {"type": "user", "timestamp": "t", "message": {"content": "No, redo it"}}])
    monkeypatch.setenv("HOME", str(home))

    def run(*argv):
        capsys.readouterr()
        code = cli.main(["corrections", *argv])
        return code, json.loads(capsys.readouterr().out)

    code, out = run("scan", "--agent", "claude", "--session", "abc")
    assert code == 1 and out["error"]["code"] == "disabled"
    assert run("enable")[1]["result"]["enabled"] is True
    assert run("scan", "--agent", "claude", "--session", "abc")[1]["result"]["new"] == 1
    assert run("list")[1]["result"]["total"] == 1
    assert run("forget")[0] == 1                                      # naming nothing is refused
    assert run("forget", "--all")[1]["result"]["removed"] == 1
    assert run("status")[1]["result"]["total"] == 0
