"""Agent inspector (spec 0200): ingest, bounds, redaction, access, restore, scale."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.inspector import (
    DEFAULT_LIMITS,
    InspectorStore,
    truncate_text,
    validate_prefs,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

SID = "w1:p1"


def tool(call, phase, **kw):
    return {"kind": "tool", "call_id": call, "phase": phase, **kw}


def feed(store, *events, sid=SID):
    return store.ingest(sid, {"events": list(events)})


@pytest.fixture
def store():
    return InspectorStore()


# ------------------------------------------------------------------ ingest
def test_all_kinds_roundtrip(store):
    r = feed(
        store,
        {"kind": "capabilities", "reasoning": "summary", "streaming_tools": True},
        {"kind": "message", "role": "user", "text": "fix the bug"},
        {"kind": "reasoning", "text": "plan: read file first"},
        tool("c1", "start", tool="bash", args_preview="pytest -q"),
        tool("c1", "end", output="ok", exit_code=0),
        {"kind": "file", "path": "a.py", "op": "edit", "diff": "--- a\n+++ b\n+x\n-y\n+z"},
        {"kind": "test", "run_id": "t1", "phase": "end", "passed": 3, "failed": 1,
         "failures": [{"name": "test_x", "message": "boom"}]},
        {"kind": "usage", "input_tokens": 10, "output_tokens": 5, "context_tokens": 50,
         "context_window": 200},
        {"kind": "association", "mission_id": "m1", "branch": "b", "pr": {"number": 7, "url": "u"}},
        {"kind": "model", "model": "x", "previous_model": "y", "reason": "handoff"},
        {"kind": "lifecycle", "phase": "started"},
    )
    assert r["accepted"] == 11 and not r["rejected"]
    q = store.query(SID, "operator")
    s = q["summary"]
    assert s["counts"]["tool"] == 1 and s["counts"]["file"] == 1
    assert s["files"][0]["additions"] == 2 and s["files"][0]["deletions"] == 1
    assert s["tests"][0]["failed"] == 1 and s["tests"][0]["failures"] == ["test_x"]
    assert s["usage"]["input_tokens"] == 10 and s["usage"]["context_pct"] == 25.0
    assert s["association"]["pr"]["number"] == 7
    assert q["capabilities"]["reasoning"] == "summary"


def test_nul_bytes_never_reach_storage(store):
    """Postgres jsonb refuses \\u0000, so a binary tool output once failed the whole batch with a 500."""
    r = feed(store, {"kind": "message", "role": "assistant", "text": "a\x00b"},
             tool("c1", "end", output="x\x00y\x00"),
             {"kind": "file", "path": "a.py", "op": "edit", "diff": "+q\x00r"})
    assert r["accepted"] == 3 and "\\u0000" not in json.dumps(store.query(SID, "operator"))


def test_rejects_bad_events_without_dropping_good_ones(store):
    r = feed(store, {"kind": "nope"}, "str", {"kind": "tool", "phase": "start"},
             {"kind": "message", "text": "ok"})
    assert r["accepted"] == 1 and len(r["rejected"]) == 3


def test_batch_limit(store):
    with pytest.raises(ValueError):
        store.ingest(SID, {"events": [{"kind": "message", "text": "x"}] * 101})


def test_idempotent_event_ids(store):
    ev = {"kind": "message", "text": "a", "event_id": "e1"}
    assert feed(store, ev)["accepted"] == 1
    assert feed(store, ev)["duplicates"] == 1
    assert store.query(SID)["summary"]["counts"]["message"] == 1


# --------------------------------------------------------------- reasoning
def test_reasoning_never_fabricated_without_capability(store):
    r = feed(store, {"kind": "reasoning", "text": "secret thoughts"})
    assert r["accepted"] == 0 and "not exposed" in r["rejected"][0]["error"]
    feed(store, {"kind": "capabilities", "reasoning": "none"})
    assert feed(store, {"kind": "reasoning", "text": "x"})["accepted"] == 0
    q = store.query(SID, "operator", view="reasoning")
    assert q["events"] == [] and q["capabilities"]["reasoning"] == "none"


def test_reasoning_unknown_capability_before_declaration(store):
    feed(store, {"kind": "message", "text": "hi"})
    assert store.query(SID)["capabilities"] == {"declared": False, "reasoning": "unknown"}


def test_reasoning_withheld_from_viewers_and_not_searchable(store):
    feed(store, {"kind": "capabilities", "reasoning": "full"},
         {"kind": "reasoning", "text": "needle-in-thoughts"})
    v = store.query(SID, "viewer", view="reasoning")
    assert v["events"][0]["data"]["withheld"] is True
    assert v["events"][0]["data"]["text"] is None
    assert store.query(SID, "viewer", q="needle-in-thoughts")["events"] == []
    o = store.query(SID, "operator", q="needle-in-thoughts")
    assert o["events"][0]["data"]["text"] == "needle-in-thoughts"
    assert v["summary"]["reasoning_withheld"] is True


def test_reasoning_storage_can_be_disabled():
    s = InspectorStore(store_reasoning=False)
    feed(s, {"kind": "capabilities", "reasoning": "full"})
    assert feed(s, {"kind": "reasoning", "text": "x"})["accepted"] == 0


# ------------------------------------------------------------ redaction/ACL
def test_secrets_redacted_before_storage(store):
    feed(store, tool("c", "start", tool="bash", args_preview={"cmd": "curl", "api_key": "abcd1234efgh"}),
         tool("c", "end", output="token=supersecretvalue1 and sk-abcdef123456"),
         {"kind": "message", "text": "password: hunter2hunter2"})
    blob = json.dumps(store.query(SID, "operator", limit=50))
    for leaked in ("supersecretvalue1", "sk-abcdef123456", "hunter2hunter2", "abcd1234efgh"):
        assert leaked not in blob
    ev = store.query(SID, "operator", view="tools")["events"][0]
    assert ev["redacted"] is True


def test_sensitive_tool_output_and_diff_withheld_from_viewer(store):
    feed(store, tool("c", "start", tool="bash", args_preview="cat .env"),
         tool("c", "end", output="DB=prod"),
         {"kind": "file", "path": "config/.env", "op": "read", "diff": "+DB=prod"},
         {"kind": "file", "path": "src/ok.py", "op": "edit", "diff": "+ok"})
    v = store.query(SID, "viewer")
    by_path = {e["data"].get("path") or e["data"].get("tool"): e for e in v["events"]}
    assert by_path["bash"]["data"]["output"] is None and by_path["bash"]["data"]["withheld"]
    assert by_path["[withheld]"]["data"]["diff"] is None   # path itself is withheld too
    assert by_path["src/ok.py"]["data"]["diff"] == "+ok"
    assert store.query(SID, "viewer", q="prod")["events"] == []
    assert store.query(SID, "operator", q="prod")["events"]


def test_emitter_sensitive_flag_is_honoured(store):
    feed(store, {"kind": "message", "text": "private", "sensitive": True})
    assert store.query(SID, "viewer")["events"][0]["data"]["withheld"] is True


# ------------------------------------------------------------------ tools
def test_tool_streaming_status_updates_in_place(store):
    feed(store, tool("c1", "start", tool="bash", args_preview="make"))
    q = store.query(SID)
    assert [t["call_id"] for t in q["summary"]["running_tools"]] == ["c1"]
    rev = q["rev"]
    feed(store, tool("c1", "update", output="line 1"))
    feed(store, tool("c1", "update", output="line 2"))
    d = store.query(SID, since_rev=rev)
    assert len(d["events"]) == 1 and d["events"][0]["data"]["output"] == "line 2"
    feed(store, tool("c1", "end", output="done", exit_code=1, is_error=True))
    e = store.query(SID, view="tools")["events"][0]
    assert e["data"]["status"] == "error" and e["data"]["exit_code"] == 1
    assert store.query(SID)["summary"]["running_tools"] == []
    assert store.query(SID)["summary"]["counts"]["tool"] == 1


def test_missing_start_marks_incomplete_not_invented(store):
    feed(store, tool("zz", "end", tool="read", output="x"))
    e = store.query(SID, view="tools")["events"][0]
    assert e["data"]["incomplete"] is True and e["data"]["status"] == "ok"


def test_interrupted_and_restart_do_not_claim_success(store):
    feed(store, tool("a", "start", tool="bash"), tool("b", "start", tool="edit"))
    feed(store, {"kind": "lifecycle", "phase": "restarted"})
    st = {e["data"]["call_id"]: e["data"]["status"]
          for e in store.query(SID, view="tools")["events"]}
    assert st == {"a": "interrupted", "b": "interrupted"}
    assert store.query(SID)["summary"]["running_tools"] == []


def test_subagent_attribution(store):
    feed(store, tool("c1", "start", tool="grep", agent_id="reviewer", parent_agent_id="main"),
         tool("c2", "start", tool="bash"))
    q = store.query(SID, agent_id="reviewer")
    assert len(q["events"]) == 1 and q["events"][0]["parent_agent_id"] == "main"
    ids = {a["agent_id"] for a in q["summary"]["agents"]}
    assert ids == {"reviewer", "main"}


def test_test_run_failure_reporting(store):
    feed(store, {"kind": "test", "run_id": "r", "phase": "start", "command": "pytest"})
    assert store.query(SID, view="tests")["events"][0]["data"]["status"] == "running"
    feed(store, {"kind": "test", "run_id": "r", "phase": "end", "passed": 9, "failed": 2,
                 "failures": [{"name": "t_a", "message": "assert 1 == 2", "file": "a.py", "line": 3}]})
    e = store.query(SID, view="tests")["events"][0]["data"]
    assert e["status"] == "failed" and e["failures"][0]["line"] == 3
    assert store.query(SID, q="assert 1 == 2")["events"]


# ----------------------------------------------------------------- bounds
def test_body_truncation_head_tail(store):
    big = "A" * 100_000 + "TAILMARK"
    feed(store, tool("c", "start", tool="bash"), tool("c", "end", output=big))
    d = store.query(SID, view="tools")["events"][0]
    assert d["truncated"] is True
    out = store.get_event(SID, d["seq"], "operator")["data"]["output"]
    assert len(out.encode()) <= DEFAULT_LIMITS["max_body_bytes"] + 100
    assert out.startswith("AAAA") and out.endswith("TAILMARK") and "truncated" in out


def test_truncate_text_is_utf8_safe():
    s, t = truncate_text("é" * 5000, 1000)
    assert t and len(s.encode()) <= 1000 + 60
    s.encode("utf-8")


def test_list_view_caps_inline_bodies_but_full_fetch_returns_more(store):
    feed(store, {"kind": "file", "path": "a", "op": "edit", "diff": "+" + "x" * 20_000})
    ev = store.query(SID)["events"][0]
    assert len(ev["data"]["diff"]) == 2000 and ev["data"]["diff_more"] is True
    assert len(store.get_event(SID, ev["seq"], "operator")["data"]["diff"]) > 19_000


def test_large_diff_has_own_higher_cap(store):
    feed(store, {"kind": "file", "path": "a", "op": "edit", "diff": "+" + "x" * 200_000})
    ev = store.get_event(SID, 0, "operator")
    assert len(ev["data"]["diff"]) > 150_000


def test_event_count_ring_evicts_oldest_and_reports_drop():
    s = InspectorStore()
    s.set_prefs({"max_events_per_session": 50}, SID)
    for i in range(120):
        feed(s, {"kind": "message", "text": f"m{i}"})
    q = s.query(SID, limit=1000)
    assert len(q["events"]) == 50 and q["dropped"] == 70
    assert q["oldest_available_seq"] == 70
    assert q["events"][0]["data"]["text"] == "m119"


def test_byte_ring_bounds_memory():
    s = InspectorStore()
    s.set_prefs({"max_bytes_per_session": 50_000}, SID)
    for i in range(200):
        feed(s, {"kind": "message", "text": "x" * 5_000})
    q = s.query(SID, limit=1000)
    assert q["summary"]["ring"]["bytes"] <= 50_000 + 6_000
    assert q["dropped"] > 0


def test_global_session_cap_evicts_ended_first():
    s = InspectorStore(limits={"max_sessions": 5})
    for i in range(5):
        feed(s, {"kind": "message", "text": "x"}, sid=f"s{i}")
    feed(s, {"kind": "lifecycle", "phase": "ended"}, sid="s2")
    feed(s, {"kind": "message", "text": "y"}, sid="s5")
    assert not s.has_session("s2") and s.has_session("s0") and s.has_session("s5")
    assert s.health()["sessions"] == 5


def test_age_retention(store):
    t = [1_000_000.0]
    s = InspectorStore(clock=lambda: t[0])
    s.set_prefs({"retention_hours": 1}, SID)
    feed(s, {"kind": "message", "text": "old"})
    t[0] += 7200
    s.sweep()
    assert s.query(SID)["events"] == []


def test_prefs_validation_and_clamping():
    assert validate_prefs({"layout": "split", "tool_output_lines": 9999})["tool_output_lines"] == 500
    assert validate_prefs({"max_events_per_session": 10**9})["max_events_per_session"] == 50_000
    for bad in ({"layout": "weird"}, {"show_reasoning": "yes"}, {"tool_output_lines": "x"}, []):
        with pytest.raises(ValueError):
            validate_prefs(bad)
    assert "bogus" not in validate_prefs({"bogus": 1})


def test_prefs_merge_global_then_session(store):
    store.set_prefs({"layout": "stacked", "show_reasoning": True})
    store.set_prefs({"layout": "split"}, SID)
    assert store.get_prefs(SID)["layout"] == "split"
    assert store.get_prefs(SID)["show_reasoning"] is True
    assert store.get_prefs("other")["layout"] == "stacked"


# ---------------------------------------------- stream / slow clients / scale
def test_subscriber_gap_on_overflow(store):
    feed(store, {"kind": "message", "text": "seed"})
    sub = store.subscribe(SID)
    for i in range(400):
        feed(store, {"kind": "message", "text": str(i)})
    first = sub.get(0.1)
    assert first["type"] == "gap" and first["dropped"] >= 256
    store.unsubscribe(SID, sub)
    assert store.health()["subscribers"] == 0


def test_many_concurrent_agents_threaded():
    s = InspectorStore()
    errs = []

    def agent(n):
        try:
            sid = f"w{n}:p1"
            for i in range(60):
                s.ingest(sid, {"events": [
                    tool(f"c{i}", "start", tool="bash", agent_id=f"sub{n % 3}"),
                    tool(f"c{i}", "update", output="x" * 200),
                    tool(f"c{i}", "end", output="done"),
                    {"kind": "usage", "input_tokens": 1, "output_tokens": 1},
                ]})
        except Exception as exc:  # pragma: no cover
            errs.append(exc)

    threads = [threading.Thread(target=agent, args=(n,)) for n in range(64)]
    t0 = time.perf_counter()
    [t.start() for t in threads]
    [t.join() for t in threads]
    took = time.perf_counter() - t0
    assert not errs and s.health()["sessions"] == 64
    assert took < 15
    q = s.query("w7:p1", limit=1000)
    assert q["summary"]["usage"]["input_tokens"] == 60
    assert q["summary"]["running_tools"] == []


def test_ingest_throughput_budget(store):
    batch = {"events": [tool("c", "update", output="y" * 100) for _ in range(100)]}
    t0 = time.perf_counter()
    for _ in range(100):
        store.ingest(SID, batch)
    per_event_us = (time.perf_counter() - t0) / 10_000 * 1e6
    assert per_event_us < 300, per_event_us


# ----------------------------------------------- durability / restart / repo
class _Sink:
    def __init__(self):
        self.events = []

    def record(self, ev):
        self.events.append(ev)

    record_required = record


def test_messages_and_summary_kinds_are_persisted_and_hydrate_after_restart():
    sink = _Sink()
    repo = ControlPlaneRepo(sink=sink)
    repo.upsert_session(SID, host_id="h")
    repo.inspector.ingest(SID, {"events": [
        {"kind": "capabilities", "reasoning": "none"},
        {"kind": "message", "text": "conversation survives restart"},
        tool("c", "start", tool="bash"), tool("c", "end", output="o" * 5000),
        {"kind": "association", "mission_id": "m9", "branch": "feat", "handoff_from": "w0:p0"},
        {"kind": "model", "model": "b", "previous_model": "a", "reason": "switch"},
        {"kind": "test", "run_id": "r", "phase": "end", "passed": 1, "failed": 1,
         "failures": [{"name": "t", "message": "m"}]},
    ]})
    repo.inspector.set_prefs({"layout": "stacked"})
    persisted = [e for e in sink.events if e["entity_type"] == "inspector"]
    kinds = {e["payload"].get("kind") for e in persisted}
    assert kinds == {"capabilities", "message", "tool", "association", "model", "test", None}   # None = preferences
    assert "conversation survives restart" in json.dumps(persisted)

    restarted = ControlPlaneRepo()
    state = repo.export_state()
    for ev in persisted:       # what the durable log would hand back on boot
        key = f"inspector|{ev['entity_id']}"
        state["events"].setdefault(key, []).append(ev)
        state["event_order"].append(("inspector", ev["entity_id"], len(state["events"][key]) - 1))
    restarted.import_state(state)
    assert not any(k[0] == "inspector" for k in restarted._events)
    assert all(t[0] != "inspector" for t in restarted._event_order)
    restarted.recent_events()    # must not KeyError
    q = restarted.inspector.query(SID, "operator")
    assert q["summary"]["association"]["handoff_from"] == "w0:p0"
    assert q["summary"]["models"][0]["previous_model"] == "a"
    assert q["summary"]["tests"][0]["failed"] == 1
    assert restarted.inspector.get_prefs()["layout"] == "stacked"
    assert q["capabilities"]["reasoning"] == "none"


def test_inspector_traffic_stays_off_global_event_feed():
    repo = ControlPlaneRepo()
    before = len(repo._event_order)
    repo.inspector.ingest(SID, {"events": [tool(f"c{i}", "start", tool="x") for i in range(50)]})
    assert len(repo._event_order) == before


# -------------------------------------------------------------------- HTTP
@pytest.fixture
def server():
    repo = ControlPlaneRepo()
    repo.upsert_session(SID, host_id="h", agent_role="pi")
    srv = ControlPlaneServer(repo, api_keys={"vk": "viewer", "ok": "operator", "ik": "ingest"})
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    yield srv
    srv.shutdown()


def call(srv, method, path, key, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def post_events(srv, *events, key="ik"):
    return call(srv, "POST", "/api/v1/ingest", key, {
        "entity_type": "inspector", "entity_id": SID, "event_type": "inspector.batch",
        "payload": {"events": list(events)}})


def test_http_ingest_and_read_roles(server):
    st, body = post_events(server, {"kind": "capabilities", "reasoning": "full"},
                           {"kind": "reasoning", "text": "think"},
                           {"kind": "usage", "input_tokens": 12, "output_tokens": 3},
                           {"kind": "test", "run_id": "r", "phase": "end", "passed": 2, "failed": 0},
                           tool("c", "start", tool="bash"), tool("c", "end", output="hi"))
    assert st == 200 and body["entity"]["accepted"] == 6, body
    # ingest role may not read mutate endpoints but may read (viewer-level)
    st, v = call(server, "GET", f"/api/v1/sessions/{SID.replace(':', '%3A')}/inspector?view=reasoning", "vk")
    assert st == 200 and v["events"][0]["data"]["withheld"] is True
    st, o = call(server, "GET", f"/api/v1/sessions/{SID}/inspector?view=reasoning", "ok")
    assert o["events"][0]["data"]["text"] == "think"
    # key-hint redaction must NOT mangle usage/test counters
    st, u = call(server, "GET", f"/api/v1/sessions/{SID}/inspector?view=usage", "vk")
    assert u["summary"]["usage"]["input_tokens"] == 12
    st, t = call(server, "GET", f"/api/v1/sessions/{SID}/inspector?view=tests", "vk")
    assert t["events"][0]["data"]["passed"] == 2
    st, _ = call(server, "GET", f"/api/v1/sessions/{SID}/inspector", "bad")
    assert st == 401


def test_http_prefs_require_operator_and_validate(server):
    assert call(server, "POST", "/api/v1/inspector/preferences", "vk", {"layout": "split"})[0] == 403
    assert call(server, "POST", "/api/v1/inspector/preferences", "ik", {"layout": "split"})[0] == 403
    st, b = call(server, "POST", "/api/v1/inspector/preferences", "ok", {"layout": "split"})
    assert st == 200 and b["prefs"]["layout"] == "split"
    assert call(server, "POST", "/api/v1/inspector/preferences", "ok", {"layout": "nope"})[0] == 400
    st, b = call(server, "POST", f"/api/v1/sessions/{SID}/inspector/preferences", "ok",
                 {"layout": "stacked", "density": "compact"})
    assert st == 200 and b["prefs"]["layout"] == "stacked"
    st, b = call(server, "GET", f"/api/v1/sessions/{SID}/inspector/preferences", "vk")
    assert b["session"] == {"layout": "stacked", "density": "compact"}
    assert call(server, "POST", "/api/v1/sessions/nope/inspector/preferences", "ok", {})[0] == 404


def test_http_unknown_session_and_no_telemetry(server):
    assert call(server, "GET", "/api/v1/sessions/ghost/inspector", "vk")[0] == 404
    st, b = call(server, "GET", f"/api/v1/sessions/{SID}/inspector", "vk")
    assert st == 200 and b["telemetry"] == "none" and b["events"] == []
    assert b["capabilities"]["reasoning"] == "unknown"


def test_http_event_body_and_health(server):
    post_events(server, {"kind": "file", "path": "a.py", "op": "edit", "diff": "+" + "z" * 5000})
    st, e = call(server, "GET", f"/api/v1/sessions/{SID}/inspector/events/0", "vk")
    assert st == 200 and len(e["data"]["diff"]) > 4000
    assert call(server, "GET", f"/api/v1/sessions/{SID}/inspector/events/99", "vk")[0] == 404
    st, h = call(server, "GET", "/api/v1/inspector/health", "vk")
    assert h["events"] == 1
    assert call(server, "GET", "/api/v1/health", "vk")[1]["inspector"]["events"] == 1


def test_http_sse_stream_pushes_updates_and_reports_gaps(server):
    post_events(server, tool("c", "start", tool="bash"))
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/api/v1/sessions/{SID}/inspector/stream",
        headers={"X-API-Key": "vk"})
    resp = urllib.request.urlopen(req, timeout=5)

    def frame():
        line = resp.readline()
        while not line.startswith(b"data:"):
            line = resp.readline()
        return json.loads(line[5:])

    assert frame()["type"] == "connected"
    post_events(server, tool("c", "update", output="tail-1"))
    f = frame()
    assert f["type"] == "event" and f["event"]["data"]["output"] == "tail-1"
    assert f["event"]["data"]["status"] == "running"
    resp.close()
    deadline = time.time() + 5
    while server.repo.inspector.health()["subscribers"] and time.time() < deadline:
        post_events(server, tool("c", "update", output="again"))
        time.sleep(0.2)
    assert server.repo.inspector.health()["subscribers"] == 0


def test_rejected_event_id_can_be_retried_after_capability_arrives(store):
    ev = {"kind": "reasoning", "text": "x", "event_id": "r1"}
    assert feed(store, ev)["accepted"] == 0
    feed(store, {"kind": "capabilities", "reasoning": "summary"})
    assert feed(store, ev)["accepted"] == 1


def test_from_config_configuration_and_fail_closed_roles():
    s = InspectorStore.from_config({
        "reasoning_role": "viewer", "sensitive_role": "bogus",
        "max_events": 123, "max_bytes": 999999999999, "store_reasoning": True})
    assert s.limits["max_events_per_session"] == 123
    assert s.limits["max_bytes_per_session"] == 64_000_000     # clamped to the hard ceiling
    feed(s, {"kind": "capabilities", "reasoning": "full"}, {"kind": "reasoning", "text": "r"},
         {"kind": "message", "text": "s", "sensitive": True})
    v = s.query(SID, "viewer")["events"]
    by_kind = {e["kind"]: e for e in v}
    assert by_kind["reasoning"]["data"]["text"] == "r"              # viewer allowed by config
    assert by_kind["message"]["data"]["withheld"] is True           # unknown role => operator-only
    assert not InspectorStore.from_config({"store_reasoning": False}).store_reasoning


def test_http_64_agents_concurrent_ingest_and_read(server):
    errs, lat = [], []

    def agent(n):
        sid = f"w{n}:p1"
        try:
            for i in range(10):
                st, _ = call(server, "POST", "/api/v1/ingest", "ik", {
                    "entity_type": "inspector", "entity_id": sid, "event_type": "inspector.batch",
                    "payload": {"events": [tool(f"c{i}", "start", tool="bash"),
                                           tool(f"c{i}", "end", output="o" * 2000),
                                           {"kind": "usage", "input_tokens": 5}]}})
                assert st == 200
            t0 = time.perf_counter()
            st, body = call(server, "GET", f"/api/v1/sessions/{sid}/inspector?view=tools", "vk")
            lat.append(time.perf_counter() - t0)
            assert st == 404 or body["summary"]["usage"]["input_tokens"] == 50
        except Exception as exc:  # pragma: no cover
            errs.append(repr(exc))

    # rate limiter is per-IP; lift it for the load test only
    server._limiter.max_requests = 10**6
    threads = [threading.Thread(target=agent, args=(n,)) for n in range(64)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errs, errs[:3]
    assert server.repo.inspector.health()["sessions"] == 64
    assert max(lat) < 2.0


# ------------------------------------------- security review regressions (#63)
SECRET_TEST = {"kind": "test", "run_id": "r", "phase": "end", "failed": 1, "sensitive": True,
               "command": "pytest", "failures": [{"name": "test_alice_ssn_123", "message": "SECRETISH", "file": "x"}]}


def test_summary_and_render_do_not_leak_sensitive_test_or_file_metadata(store):
    feed(store, SECRET_TEST,
         {"kind": "file", "path": "home/.ssh/id_rsa", "op": "read"},
         tool("c", "start", tool="bash", args_preview="cat .env"))
    q = store.query(SID, "viewer", limit=50)
    blob = json.dumps(q)
    for leaked in ("test_alice_ssn_123", "SECRETISH", "id_rsa", ".env"):
        assert leaked not in blob, leaked
    assert q["summary"]["files"][0]["path"] == "[withheld]"
    assert q["summary"]["tests"][0]["failures"] == []
    assert "test_alice_ssn_123" in json.dumps(store.query(SID, "operator", limit=50))


def test_sensitive_test_bodies_never_persisted_or_resurrected_by_hydrate():
    sink = _Sink()
    repo = ControlPlaneRepo(sink=sink)
    repo.inspector.ingest(SID, {"events": [SECRET_TEST]})
    persisted = [e for e in sink.events if e["entity_type"] == "inspector"]
    blob = json.dumps(persisted)
    assert "SECRETISH" not in blob and "test_alice_ssn_123" not in blob and persisted[0]["payload"]["sensitive"]
    fresh = InspectorStore()
    fresh.hydrate(persisted)
    assert "SECRETISH" not in json.dumps(fresh.query(SID, "operator", limit=50))
    assert fresh.query(SID, "viewer")["events"][0]["data"].get("withheld")


def test_test_command_that_reads_secrets_marks_run_sensitive(store):
    feed(store, {"kind": "test", "run_id": "r", "phase": "end", "command": "cat .env && pytest", "failed": 1,
                 "failures": [{"name": "n", "message": "m"}]})
    assert store.query(SID, "viewer")["events"][0]["data"]["withheld"] is True


def test_private_key_redaction_is_linear_time():
    from herdr_engineering.control_plane.redact import redact_text
    evil = "-----BEGIN RSA PRIVATE KEY-----" * 40_000      # >1 MB, no END marker
    t0 = time.perf_counter()
    out = redact_text(evil)
    assert time.perf_counter() - t0 < 1.0 and out == "[REDACTED]"
    assert redact_text("a -----BEGIN PRIVATE KEY-----\nxx\n-----END PRIVATE KEY----- b") == "a [REDACTED] b"
    s = InspectorStore()
    t0 = time.perf_counter()
    feed(s, {"kind": "message", "text": evil})
    assert time.perf_counter() - t0 < 1.0


def test_non_finite_and_huge_numbers_are_dropped_not_propagated(store):
    feed(store, tool("c", "start", tool="x"), tool("c", "end", exit_code=float("nan"), duration_ms=float("inf")),
         {"kind": "usage", "context_tokens": 10 ** 400, "context_window": 1, "input_tokens": 10 ** 400})
    q = store.query(SID, "viewer")
    json.dumps(q, allow_nan=False)           # strict JSON must succeed
    assert "exit_code" not in q["events"][-1]["data"] or q["events"][-1]["data"].get("exit_code") is None


def test_identifiers_are_redacted_bounded_and_validated(store):
    feed(store, {"kind": "test", "run_id": "AKIAABCDEFGHIJKLMNOP", "phase": "end", "failed": 1,
                 "failures": [{"name": "n", "message": "m", "file": "/h/.env AKIAABCDEFGHIJKLMNOP"}]},
         {"kind": "message", "text": "hi", "agent_id": "a" * 500, "ts": "not-a-time"})
    blob = json.dumps(store.query(SID, "operator", limit=50))
    assert "AKIAABCDEFGHIJKLMNOP" not in blob
    ev = store.query(SID, "operator", view="conversation")["events"][0]
    assert len(ev["agent_id"]) <= 128 and ev["ts"].startswith("20")
    for bad in ("", "a" * 129, "x y", "../../etc", None, 5):
        with pytest.raises(ValueError):
            store.ingest(bad, {"kind": "message", "text": "x"})


def test_malformed_events_are_rejected_per_event_not_500(store):
    r = feed(store, {"kind": "test", "run_id": "r", "failures": {"a": 1}},
             {"kind": "tool", "call_id": "c", "phase": 5}, {"kind": "message", "text": "ok"})
    assert r["accepted"] == 1 and len(r["rejected"]) == 2


def test_subscriber_caps(store):
    from herdr_engineering.control_plane.inspector import MAX_SUBS_PER_SESSION
    for _ in range(MAX_SUBS_PER_SESSION):
        store.subscribe(SID)
    assert not store.can_subscribe(SID)
    with pytest.raises(ValueError):
        store.subscribe(SID)


def test_http_stream_unknown_session_404_and_malformed_inputs(server):
    assert call(server, "GET", "/api/v1/sessions/ghost/inspector/stream", "vk")[0] == 404
    assert not server.repo.inspector.has_session("ghost")
    assert call(server, "GET", f"/api/v1/sessions/{SID}/inspector/events/abc", "vk")[0] == 400
    st, _ = call(server, "POST", "/api/v1/ingest", "ik", {"entity_type": "inspector", "entity_id": SID,
                                                          "event_type": 5, "payload": {}})
    assert st == 400
    st, b = call(server, "POST", "/api/v1/ingest", "ik", {"entity_type": "inspector", "entity_id": SID,
                                                          "event_type": "inspector.batch",
                                                          "payload": {"events": [{"kind": "test", "run_id": "r", "failures": {}}]}})
    assert st == 200 and b["entity"]["rejected"] == [] or st == 200


def test_http_rejects_oversized_body(server):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/ingest", method="POST",
                                 data=b"{" + b" " * (5 * 1024 * 1024) + b"}",
                                 headers={"X-API-Key": "ik", "Content-Type": "application/json"})
    # The server refuses before reading the body: either a 400 or a reset while we upload.
    with pytest.raises((urllib.error.HTTPError, urllib.error.URLError, ConnectionError)) as e:
        urllib.request.urlopen(req, timeout=10)
    if isinstance(e.value, urllib.error.HTTPError):
        assert e.value.code == 400


# ------------------------------------------- correctness review regressions (#63)
def test_reused_call_id_keeps_live_mapping_after_old_entry_evicted():
    s = InspectorStore()
    s.set_prefs({"max_events_per_session": 50}, SID)
    feed(s, tool("c1", "start", tool="a"), tool("c1", "end"))
    for i in range(48):
        feed(s, {"kind": "message", "text": str(i)})
    feed(s, tool("c1", "start", tool="a"))         # new live entry reusing the id
    feed(s, {"kind": "message", "text": "evicts the old ended c1"})
    feed(s, tool("c1", "end", output="x"))
    q = s.query(SID, view="tools")
    assert [e["data"]["status"] for e in q["events"]] == ["ok"]
    assert q["summary"]["running_tools"] == []


def test_global_eviction_never_evicts_the_session_being_written():
    s = InspectorStore(limits={"max_sessions": 2})
    for sid in ("A", "B"):
        s.subscribe(sid)
    feed(s, {"kind": "message", "text": "x"}, sid="C")
    assert s.has_session("C")


def test_client_timestamps_cannot_pin_or_shorten_retention():
    t = [1_000_000.0]
    s = InspectorStore(clock=lambda: t[0])
    s.set_prefs({"retention_hours": 1}, SID)
    feed(s, {"kind": "message", "text": "future", "ts": "2999-01-01T00:00:00Z"})
    t[0] += 1800
    feed(s, {"kind": "message", "text": "fresh", "ts": "2001-01-01T00:00:00Z"})   # past ts must not evict early
    t[0] += 1801
    s.sweep()
    texts = [e["data"]["text"] for e in s.query(SID)["events"]]
    assert texts == ["fresh"]                    # 'future' aged out on server time; 'fresh' kept
    for e in s.query(SID)["events"]:
        assert e["ts"] < "2100"                  # clamped, not echoed
    feed(s, {"kind": "message", "text": "naive", "ts": "0001-01-01T00:00:00"})  # no exception


def test_infinite_prefs_rejected_cleanly():
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError):
            validate_prefs({"tool_output_lines": bad})


def test_has_more_is_exact_and_epoch_is_reported(store):
    for i in range(5):
        feed(store, {"kind": "message", "text": str(i)})
    assert store.query(SID, limit=5)["has_more"] is False
    assert store.query(SID, limit=4)["has_more"] is True
    assert store.query(SID)["epoch"] == store.epoch
    assert InspectorStore().epoch != store.epoch


# ------------------------------------------------ tokenless operation (no API keys)
def test_open_tailnet_mode_needs_no_token_and_is_operator_for_everyone():
    """Deployed control planes run --allow-open on the tailnet: no key anywhere."""
    repo = ControlPlaneRepo()
    repo.upsert_session(SID, host_id="h")
    srv = ControlPlaneServer(repo, allow_open=True)          # no api_keys at all
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    try:
        def raw(method, path, body=None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}{path}", method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"})          # note: no X-API-Key
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())

        st, b = raw("POST", "/api/v1/ingest", {
            "entity_type": "inspector", "entity_id": SID, "event_type": "inspector.batch",
            "payload": {"events": [{"kind": "capabilities", "reasoning": "summary"},
                                   {"kind": "reasoning", "text": "visible to the whole tailnet"}]}})
        assert st == 200 and b["entity"]["accepted"] == 2
        st, q = raw("GET", f"/api/v1/sessions/{SID}/inspector?view=reasoning")
        # Open mode treats every caller as operator, so reasoning is readable by anyone who can
        # reach the port. Operators who do not want that set control_plane.inspector_limits.store_reasoning: false.
        assert q["events"][0]["data"]["text"] == "visible to the whole tailnet"
    finally:
        srv.shutdown()


def test_keyed_server_still_rejects_unauthenticated_inspector_ingest(server):
    st, _ = call(server, "POST", "/api/v1/ingest", "", {
        "entity_type": "inspector", "entity_id": SID, "event_type": "inspector.batch",
        "payload": {"events": [{"kind": "message", "text": "x"}]}})
    assert st == 401
    assert server.repo.inspector.health()["events"] == 0


# ------------------------------------- authorization must actually stop the request
def test_refused_requests_do_not_execute(server):
    """Regression: 401/403 used to be sent and the handler then ran the request anyway."""
    store = server.repo.inspector
    assert call(server, "POST", "/api/v1/inspector/preferences", "vk", {"layout": "split"})[0] == 403
    assert call(server, "POST", "/api/v1/inspector/preferences", "ik", {"layout": "split"})[0] == 403
    assert call(server, "POST", "/api/v1/inspector/preferences", "nope", {"layout": "split"})[0] == 401
    assert store.raw_prefs() == {}
    before = len(server.repo.stream("session", SID))
    assert call(server, "POST", f"/api/v1/sessions/{SID}/messages", "vk", {"message": "x"})[0] == 403
    assert call(server, "POST", f"/api/v1/sessions/{SID}/actions", "vk", {"action": "stop"})[0] == 403
    assert len(server.repo.stream("session", SID)) == before
    assert call(server, "POST", "/api/v1/ingest", "nope", {
        "entity_type": "host", "entity_id": "evil", "event_type": "host.registered", "payload": {}})[0] == 401
    assert "evil" not in server.repo._hosts
    # an ingest key may ingest, but only ingest
    assert call(server, "POST", "/api/v1/ingest", "ik", {
        "entity_type": "host", "entity_id": "ok-host", "event_type": "host.registered", "payload": {}})[0] == 200
    assert "ok-host" in server.repo._hosts
    assert call(server, "POST", "/api/v1/token-usage", "ik", {"project": "p", "model": "m"})[0] == 403
    # an operator key still works
    assert call(server, "POST", "/api/v1/inspector/preferences", "ok", {"layout": "split"})[0] == 200


def test_every_mutating_route_is_refused_without_side_effects(server):
    repo = server.repo
    routes = [
        ("POST", "/api/v1/token-usage", {"project": "p", "model": "m", "input_tokens": 1}),
        ("POST", "/api/v1/llm/keys", {"project": "p"}),
        ("POST", "/api/v1/llm/models", {"name": "n", "base_url": "http://x"}),
        ("POST", f"/api/v1/sessions/{SID}/messages", {"message": "x"}),
        ("POST", f"/api/v1/sessions/{SID}/actions", {"action": "stop"}),
        ("POST", f"/api/v1/sessions/{SID}/inspector/preferences", {"layout": "split"}),
        ("POST", "/api/v1/hosts/h9/telemetry", {"cpu": 1}),
    ]
    snapshot = lambda: (len(repo._event_order), json.dumps(repo.inspector.raw_prefs(SID)),  # noqa: E731
                        json.dumps(sorted(repo._hosts)))
    before = snapshot()
    for key in ("vk", "ik", "nope", ""):
        for method, path, body in routes:
            st, _ = call(server, method, path, key, body)
            assert st in (401, 403), (key, path, st)
    assert snapshot() == before
    assert call(server, "GET", f"/api/v1/sessions/{SID}/inspector", "nope")[0] == 401
    st, body = call(server, "GET", "/api/v1/health", "")   # liveness only without a key
    assert st == 200 and set(body) == {"status", "version", "commit"}


def test_refused_post_closes_connection_so_the_body_cannot_be_smuggled(server):
    import socket
    inner = json.dumps({"entity_type": "host", "entity_id": "smug", "event_type": "host.registered",
                        "payload": {}})
    inner_req = (f"POST /api/v1/ingest HTTP/1.1\r\nHost: x\r\nX-API-Key: ik\r\n"
                 f"Content-Type: application/json\r\nContent-Length: {len(inner)}\r\n\r\n{inner}")
    outer = (f"POST /api/v1/token-usage HTTP/1.1\r\nHost: x\r\nX-API-Key: vk\r\n"
             f"Content-Type: application/json\r\nContent-Length: {len(inner_req)}\r\n\r\n{inner_req}")
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as s:
        s.sendall(outer.encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk                       # server must close, so this terminates
    assert data.startswith(b"HTTP/1.1 403") and b"Connection: close" in data
    assert data.count(b"HTTP/1.") == 1          # no second (smuggled) response
    assert "smug" not in server.repo._hosts


@pytest.mark.parametrize("sid", ["herdr:bender:default:w1Z%3ApF", "herdr:bender:default:w1Z:pF", "w1:p1"])
def test_canonical_pane_ids_are_accepted_without_decoding(store, sid):
    assert feed(store, {"kind": "message", "text": "hello"}, sid=sid)["accepted"] == 1
    assert store.has_session(sid)


@pytest.mark.parametrize("sid", [
    "herdr:bender:default:w1%3ap1", "herdr:bender:default:w1%253Ap1",
    "herdr:bender:default:%2Fetc", "herdr:bender:default:%00",
    "herdr:bender:default:%41", "w1%3Ap1", "x%", "w1:p1\n",
])
def test_noncanonical_pane_escapes_are_rejected(store, sid):
    with pytest.raises(ValueError):
        feed(store, {"kind": "message", "text": "hello"}, sid=sid)


def test_durable_messages_restore_redaction_attribution_order_and_replay_identity():
    now = [time.time()]
    rows = []
    store = InspectorStore(persist=rows.append, clock=lambda: now[0])
    first = {"kind": "message", "role": "user", "text": "token=ghp_" + "a" * 36,
             "event_id": "turn-1", "ts": "2099-01-01T00:00:00Z", "sensitive": True}
    second = {"kind": "message", "role": "assistant", "text": "working on it",
              "event_id": "turn-2", "agent_id": "helper", "parent_agent_id": "main",
              "ts": "2001-01-01T00:00:00Z"}
    feed(store, first)
    now[0] += 1
    feed(store, second)
    fresh = InspectorStore(clock=lambda: now[0])
    assert fresh.hydrate(list(reversed(rows))) == 2
    events = fresh.query(SID, "operator", view="conversation")["events"]
    assert [e["data"]["role"] for e in events] == ["assistant", "user"]
    assert events[0]["parent_agent_id"] == "main"
    assert events[0]["agent_id"] == "helper"
    assert events[1]["redacted"] is True
    assert "ghp_" + "a" * 36 not in json.dumps(rows)
    assert fresh.query(SID, "viewer")["events"][1]["data"]["withheld"] is True
    assert feed(fresh, first, second)["duplicates"] == 2
    assert len(fresh.query(SID)["events"]) == 2


def test_durable_messages_obey_body_ring_and_original_receive_retention_limits():
    now = [time.time()]
    rows = []
    store = InspectorStore(persist=rows.append, clock=lambda: now[0], limits={"max_body_bytes": 200})
    feed(store, {"kind": "message", "text": "expired", "event_id": "old"})
    now[0] += 3601
    for i in range(4):
        feed(store, {"kind": "message", "text": str(i) * 1000, "event_id": str(i)})
        now[0] += 1
    fresh = InspectorStore(clock=lambda: now[0], limits={"max_events_per_session": 2, "retention_hours": 1})
    fresh.hydrate(rows)
    events = fresh.query(SID)["events"]
    assert len(events) == 2
    assert all(e["truncated"] and len(e["data"]["text"].encode()) <= 200 for e in events)
    now[0] += 3601
    fresh.sweep()
    assert fresh.query(SID)["events"] == []
    another = InspectorStore(clock=lambda: now[0], limits={"retention_hours": 1})
    another.hydrate(rows)
    assert another.query(SID)["events"] == []


def test_message_durability_excludes_reasoning_and_system_bodies():
    rows = []
    store = InspectorStore(persist=rows.append)
    feed(store, {"kind": "capabilities", "reasoning": "full"},
         {"kind": "reasoning", "text": "private reasoning"},
         {"kind": "message", "role": "system", "text": "private instructions"},
         tool("t", "start", args_preview="observable command"),
         tool("t", "end", output="observable output"),
         {"kind": "message", "role": "assistant", "text": "public response"})
    blob = json.dumps(rows)
    assert "public response" in blob
    assert "private" not in blob



def test_explicit_github_issue_association_survives_restart():
    rows = []
    store = InspectorStore(persist=rows.append)
    issue = {"repo": "org/repo", "number": 42, "url": "https://github.com/org/repo/issues/42"}
    feed(store, {"kind": "association", "issue": issue, "parent_session_id": "parent"})
    fresh = InspectorStore()
    fresh.hydrate(rows)
    association = fresh.query(SID)["summary"]["association"]
    assert association["issue"] == issue
    assert association["parent_session_id"] == "parent"


def test_replay_dedup_window_covers_retained_conversation_ring():
    rows = []
    store = InspectorStore(persist=rows.append)
    for i in range(1100):
        feed(store, {"kind": "message", "text": str(i), "event_id": f"message-{i}"})
    fresh = InspectorStore()
    fresh.hydrate(rows)
    assert feed(fresh, {"kind": "message", "text": "0", "event_id": "message-0"})["duplicates"] == 1


@pytest.mark.parametrize("role", ["developer", "tool", "reasoning"])
def test_nonconversation_roles_cannot_be_persisted_as_assistant_messages(role):
    rows = []
    store = InspectorStore(persist=rows.append)
    result = feed(store, {"kind": "message", "role": role, "text": "private instructions"})
    assert result["accepted"] == 0
    assert not rows



def test_http_canonical_pane_id_roundtrip(server):
    sid = "herdr:bender:default:w1Z%3ApF"
    status, body = call(server, "POST", "/api/v1/ingest", "ik", {
        "entity_type": "inspector", "entity_id": sid, "event_type": "inspector.batch",
        "payload": {"events": [{"kind": "message", "role": "user", "text": "hello"}]}})
    assert status == 200 and body["entity"]["accepted"] == 1
    path_id = urllib.parse.quote(sid, safe="")
    status, body = call(server, "GET", f"/api/v1/sessions/{path_id}/inspector", "vk")
    assert status == 200 and body["events"][0]["data"]["text"] == "hello"


def test_message_persistence_failure_does_not_publish_or_ack_and_retry_is_idempotent():
    class FlakySink:
        def __init__(self):
            self.events = []
            self.fail = True

        def record(self, event):
            raise AssertionError("conversation must use acknowledged persistence")

        def record_required(self, event):
            if self.fail and event["payload"]["event_id"] == "second":
                raise OSError("database unavailable")
            self.events.append(event)

    sink = FlakySink()
    store = ControlPlaneRepo(sink=sink).inspector
    sub = store.subscribe(SID)
    first = {"kind": "message", "event_id": "first", "text": "persisted"}
    second = {"kind": "message", "event_id": "second", "text": "retry me"}
    with pytest.raises(RuntimeError, match="persistence"):
        feed(store, first, second)
    assert [e["data"]["text"] for e in store.query(SID)["events"]] == ["persisted"]
    assert sub.get(0)["type"] == "event"
    assert sub.get(0) is None
    assert store.stats["accepted"] == 1
    sink.fail = False
    result = feed(store, first, second)
    assert result["accepted"] == 1 and result["duplicates"] == 1
    assert len(sink.events) == 2
    assert [e["seq"] for e in store.query(SID)["events"]] == [1, 0]
    fresh = InspectorStore(persist=lambda event: pytest.fail("hydrate must not write"))
    fresh.hydrate(sink.events)
    assert feed(fresh, first, second)["duplicates"] == 2


def test_configured_best_effort_only_sink_cannot_ack_conversation():
    class UnacknowledgedSink:
        def record(self, event):
            pass

    store = ControlPlaneRepo(sink=UnacknowledgedSink()).inspector
    with pytest.raises(RuntimeError, match="persistence"):
        feed(store, {"kind": "message", "text": "must survive", "event_id": "retry"})
    assert store.query(SID)["events"] == []



def test_http_failed_conversation_write_is_not_acknowledged(server):
    class BrokenSink:
        def record_required(self, event):
            raise OSError("database unavailable")

    server.repo._sink = BrokenSink()
    status, _ = call(server, "POST", "/api/v1/ingest", "ik", {
        "entity_type": "inspector", "entity_id": SID, "event_type": "inspector.batch",
        "payload": {"events": [{"kind": "message", "event_id": "retry", "text": "durable"}]}})
    assert status >= 500
    assert server.repo.inspector.query(SID)["events"] == []
    assert server.repo.inspector.stats["accepted"] == 0


def test_durable_activity_replays_tool_phases_files_usage_and_retry_ids():
    rows = []
    now = [time.time()]
    store = InspectorStore(persist=rows.append, clock=lambda: now[0], limits={"max_body_bytes": 200})
    secret = "ghp_" + "a" * 36
    events = [
        tool("c", "start", event_id="start", tool="bash", args_preview="cat .env",
             agent_id="helper", parent_agent_id="main"),
        {"kind": "message", "event_id": "message", "text": "working"},
        tool("c", "update", event_id="update", output="token=" + secret),
        tool("c", "end", event_id="end", output="token=" + secret + "\n" + "x" * 500,
             exit_code=1, is_error=True),
        {"kind": "file", "event_id": "file", "path": ".env", "op": "edit",
         "diff": "+token=" + secret},
        {"kind": "usage", "event_id": "usage", "input_tokens": 10, "output_tokens": 20},
    ]
    feed(store, *events)
    assert len(rows) == len(events)
    assert secret not in json.dumps(rows)
    fresh = InspectorStore(clock=lambda: now[0])
    # Equal receive timestamps still preserve mutation order, including interleaved messages.
    assert fresh.hydrate(list(reversed(rows))) == len(events)
    tool_entry = fresh.query(SID, "operator", view="tools")["events"][0]
    assert tool_entry["data"]["status"] == "error"
    assert tool_entry["data"]["exit_code"] == 1
    assert tool_entry["agent_id"] == "helper" and tool_entry["parent_agent_id"] == "main"
    assert tool_entry["redacted"] and tool_entry["truncated"]
    assert len(tool_entry["data"]["output"].encode()) <= 200
    assert fresh.query(SID, "viewer", view="tools")["events"][0]["data"]["withheld"]
    file_entry = fresh.query(SID, "operator", view="files")["events"][0]
    assert file_entry["redacted"] and secret not in file_entry["data"]["diff"]
    assert fresh.query(SID, "viewer", view="files")["events"][0]["data"]["withheld"]
    assert fresh.query(SID)["summary"]["usage"]["input_tokens"] == 10
    assert feed(fresh, *events)["duplicates"] == len(events)


@pytest.mark.parametrize("failed_kind", ["start", "update", "end", "file", "usage"])
def test_activity_persistence_failure_is_retryable_without_partial_mutation(failed_kind):
    class FlakySink:
        fail = True
        events = None

        def __init__(self):
            self.events = []

        def record(self, event):
            pytest.fail("activity must use acknowledged persistence")

        def record_required(self, event):
            if self.fail and event["payload"].get("event_id") == "failed":
                raise OSError("database unavailable")
            self.events.append(event)

    sink = FlakySink()
    store = ControlPlaneRepo(sink=sink).inspector
    if failed_kind in ("update", "end"):
        feed(store, tool("c", "start", event_id="start", output="before"))
    event = (tool("c", failed_kind, output="after") if failed_kind in ("start", "update", "end")
             else {"kind": "file", "path": "main.py", "diff": "+new"} if failed_kind == "file"
             else {"kind": "usage", "input_tokens": 7})
    event["event_id"] = "failed"
    feed(store)
    before = store.query(SID)["events"]
    # Snapshot nested data before attempting the write.
    before = json.loads(json.dumps(before))
    with pytest.raises(RuntimeError, match="persistence"):
        feed(store, event)
    assert store.query(SID)["events"] == before
    sink.fail = False
    assert feed(store, event)["accepted"] == 1
    assert feed(store, event)["duplicates"] == 1
    fresh = InspectorStore()
    fresh.hydrate(sink.events)
    assert feed(fresh, event)["duplicates"] == 1
    if failed_kind == "usage":
        assert fresh.query(SID)["summary"]["usage"]["input_tokens"] == 7


def test_replayed_tool_mutations_keep_original_age_and_do_not_flag_other_turns():
    rows = []
    now = [time.time()]
    store = InspectorStore(persist=rows.append, clock=lambda: now[0])
    feed(store, tool("c", "start", event_id="start", tool="bash"))
    created = now[0]
    now[0] += 30
    feed(store, {"kind": "message", "event_id": "message", "text": "visible reply"})
    now[0] += 30
    feed(store, tool("c", "end", event_id="end", output="token=" + "ghp_" + "a" * 36))
    fresh = InspectorStore(clock=lambda: now[0], limits={"retention_hours": 1})
    fresh.hydrate(rows)
    activity = fresh.query(SID, "operator", view="tools")["events"][0]
    assert activity["redacted"]
    assert activity["data"]["duration_ms"] == 60_000
    assert fresh._sessions[SID].events[0]["recv"] == created
    message = fresh.query(SID, view="conversation")["events"][0]
    assert not message["redacted"]
    now[0] = created + 3601
    fresh.sweep()
    assert fresh.query(SID, view="tools")["events"] == []
    assert len(fresh.query(SID, view="conversation")["events"]) == 1


def test_replay_restores_reused_tool_call_ids_as_distinct_entries():
    rows = []
    store = InspectorStore(persist=rows.append)
    for index in range(2):
        feed(store, tool("c", "start", event_id=f"start-{index}"),
             tool("c", "end", event_id=f"end-{index}", output=str(index)))
    fresh = InspectorStore()
    fresh.hydrate(rows)
    assert [e["data"]["output"] for e in fresh.query(SID, view="tools")["events"]] == ["1", "0"]
