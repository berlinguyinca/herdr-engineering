"""Spec 08: agent mission/session trace on recorded builds. Real subprocesses."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import build_metrics as bm
from herdr_engineering.build_coord import run_gated

REPO = Path(__file__).resolve().parents[1]
SECRET = "S3CR3T-TOKEN-xyz123"


def _events(state: Path) -> list[dict]:
    return [json.loads(x) for x in (state / "builds.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("cmd,kind", [
    (["cargo", "nextest", "run"], "cargo nextest"),
    (["rustc", "main.rs"], "rustc"),
    (["/usr/bin/rustc", "--edition", "2021", "x.rs"], "rustc"),
    (["cargo", "fmt"], "other"),
])
def test_classify_new_kinds(cmd, kind):
    assert bm.classify_cmd(cmd) == kind


@pytest.mark.parametrize("value", [
    "sess-1234", "0f8fad5b-d9cb-469f-a165-0305e2bce2b7", "m_42.a:b",
])
def test_trace_id_accepted(value):
    assert bm.sanitize_trace_id(value) == value


@pytest.mark.parametrize("value", [
    None, "", "a" * 65, "has space", "a=b", "x;rm", "$(id)", "tok\n", "../etc",
    "sk-abcdef0123456789", "ghp_abcdefghijklmnop", "my-token-1", "Password1",
    "secretvalue", "api-key-1", "AKIAABCDEFGHIJKLMNOP",
])
def test_trace_id_rejected(value):
    assert bm.sanitize_trace_id(value) is None


def test_trace_in_events_without_argv_or_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_API_TOKEN", SECRET)
    state = tmp_path / "s"
    trace = bm.make_trace("sess-1", "mission-7")
    code = run_gated(["sh", "-c", f"exit 0 # {SECRET}"], slots=2, max_load=0,
                     state_dir=state, record=True, trace=trace)
    assert code == 0
    evs = _events(state)
    assert [e["event"] for e in evs] == ["build_started", "build_finished"]
    for e in evs:
        assert e["trace"] == {"session": "sess-1", "mission": "mission-7", "source": "pi"}
        assert e["host"] and e["ts"] and e["cmd_kind"] == "other"
    raw = (state / "builds.jsonl").read_text()
    assert SECRET not in raw and "SOME_API_TOKEN" not in raw and "exit 0" not in raw


def test_trace_populates_registry_ids(tmp_path):
    state = tmp_path / "s"
    run_gated(["true"], slots=2, max_load=0, state_dir=state, record=True,
              trace=bm.make_trace("sess-2", "mission-9"))
    con = sqlite3.connect(state / "build-registry.db")
    try:
        row = con.execute("SELECT owner_session, mission_id FROM builds").fetchone()
    finally:
        con.close()
    assert row == ("sess-2", "mission-9")


def test_no_trace_means_no_field(tmp_path):
    state = tmp_path / "s"
    run_gated(["true"], slots=2, max_load=0, state_dir=state, record=True)
    assert all("trace" not in e for e in _events(state))


def test_make_trace_drops_invalid_parts():
    assert bm.make_trace("ok-1", "bad value") == {"session": "ok-1", "source": "pi"}
    assert bm.make_trace("bad value", None) is None


def _cli(args, state, home, extra_env=None):
    env = {**os.environ, "HERDR_ENGINEERING_STATE": str(state), "HOME": str(home),
           "PYTHONPATH": str(REPO)}
    env.pop("HERDR_ENGINEERING_CONFIG", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "build-coordinate", *args],
                          capture_output=True, text=True, timeout=60, env=env, cwd=home)


def test_cli_require_cache_enabled_disabled_runs_plain_and_unrecorded(tmp_path):
    state = tmp_path / "state"
    r = _cli(["--require-cache-enabled", "--record", "--trace-session", "s1", "--",
              "sh", "-c", "echo hi; exit 3"], state, tmp_path)
    assert r.returncode == 3 and "hi" in r.stdout
    assert not (state / "build-gates").exists()  # no gate, no lock, no events


def test_cli_trace_flags_recorded(tmp_path):
    state = tmp_path / "state"
    r = _cli(["--record", "--max-load", "0", "--trace-session", "s1", "--trace-mission", "m1",
              "--", "true"], state, tmp_path)
    assert r.returncode == 0, r.stderr
    evs = _events(state / "build-gates")
    assert evs[0]["trace"] == {"session": "s1", "mission": "m1", "source": "pi"}


def test_cli_bad_trace_value_is_dropped_not_fatal(tmp_path):
    state = tmp_path / "state"
    r = _cli(["--record", "--max-load", "0", "--trace-session", "ghp_abcdefghijklmnop",
              "--", "true"], state, tmp_path)
    assert r.returncode == 0
    assert all("trace" not in e for e in _events(state / "build-gates"))
    assert "ghp_abcdefghijklmnop" not in r.stderr + r.stdout


def test_cli_does_not_nest_a_gate(tmp_path):
    state = tmp_path / "state"
    r = _cli(["--record", "--max-load", "0", "--", "sh", "-c", "exit 0"], state, tmp_path)  # control: gated
    assert (state / "build-gates" / "builds.jsonl").exists()
    state2 = tmp_path / "state2"
    r = _cli(["--record", "--max-load", "0", "--", "sccache", "--version"], state2, tmp_path)
    assert not (state2 / "build-gates").exists()  # exec'd directly, not gated
    assert r.returncode in (0, 127)
