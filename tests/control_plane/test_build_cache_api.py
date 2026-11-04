"""/api/v1/build-cache (spec 09): flag-gated, authenticated, read-only plus an operator cleanup PREVIEW.

Real ControlPlaneServer on a socket, real state files in tmp_path; no mocks.
"""
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.build_cache_routes import (
    CAPABILITY,
    build_build_cache,
    build_cache_config,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

KEYS = {"k-op": "operator", "k-view": "viewer", "k-ingest": "ingest"}
SECTION = {"enabled": True, "local": {"capacity_gib": 1}, "eviction": {"hard_cap_gib": 1, "max_entries_per_run": 50},
           "cleanup": {"min_idle_hours": 1, "max_evict_per_pass_gib": 1}}
SECRET = "sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2"


def _write_builds(state, rows):
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "builds.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _started(i, **kw):
    return {"event": "build_started", "ts": f"2026-11-04T00:00:{i:02d}Z", "build_id": f"b{i}", "host": "h1",
            "slot": 0, "cmd_kind": "cargo build", "cache": {"applied": True}, **kw}


def _finished(i, code=0, **kw):
    return {"event": "build_finished", "ts": f"2026-11-04T00:01:{i:02d}Z", "build_id": f"b{i}", "host": "h1",
            "slot": 0, "cmd_kind": "cargo build", "exit_code": code, "elapsed_s": 1.5, "unavailable": [],
            "cache": {"applied": True}, "sccache_stats": None, **kw}


def _serve(cfg, keys=KEYS):
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_keys=keys, build_cache=cfg)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr, f"http://127.0.0.1:{svr.port}", repo


def call(base, method, path, key=None, body=None, ctype="application/json"):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": ctype}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


@pytest.fixture
def served(tmp_path):
    state = tmp_path / "state"
    svr, base, repo = _serve({"enabled": True, "state_dir": str(state), "fabric_build_cache": SECTION})
    try:
        yield base, state, repo
    finally:
        svr.shutdown()


# ---------------------------------------------------------------------------------------------- flag
def test_config_defaults_off_and_validates():
    assert build_cache_config(None)["enabled"] is False
    assert build_build_cache(None, record_event=None) is None
    assert build_build_cache({"enabled": False}, record_event=None) is None
    with pytest.raises(ValueError):
        build_cache_config({"enabled": "yes"})
    with pytest.raises(ValueError):
        build_cache_config({"enabled": True, "bogus": 1})


def test_flag_off_routes_404_and_no_capability(tmp_path):
    svr, base, _ = _serve(None)
    try:
        for path in ("status", "hosts", "builds", "policies"):
            assert call(base, "GET", f"/api/v1/build-cache/{path}", "k-op")[0] == 404
        assert call(base, "POST", "/api/v1/build-cache/cleanup/preview", "k-op", {})[0] == 404
        assert CAPABILITY not in call(base, "GET", "/api/v1/health", "k-op")[1]["capabilities"]
    finally:
        svr.shutdown()


def test_flag_on_advertises_capability(served):
    base, _, _ = served
    assert CAPABILITY == "build_cache_v1"
    assert CAPABILITY in call(base, "GET", "/api/v1/health", "k-view")[1]["capabilities"]


# ---------------------------------------------------------------------------------------------- auth
@pytest.mark.parametrize("path", ["status", "hosts", "builds", "builds/b1", "policies"])
def test_unauthenticated_is_401_and_viewer_can_read(served, path):
    base, state, _ = served
    _write_builds(state, [_started(1), _finished(1)])
    assert call(base, "GET", f"/api/v1/build-cache/{path}")[0] == 401
    assert call(base, "GET", f"/api/v1/build-cache/{path}", "wrong")[0] == 401
    assert call(base, "GET", f"/api/v1/build-cache/{path}", "k-view")[0] == 200


def test_preview_requires_operator(served):
    base, _, _ = served
    url = "/api/v1/build-cache/cleanup/preview"
    assert call(base, "POST", url, None, {})[0] == 401
    assert call(base, "POST", url, "k-view", {})[0] == 403
    assert call(base, "POST", url, "k-ingest", {})[0] == 403
    assert call(base, "POST", url, "k-op", {})[0] == 200


def test_preview_content_type_and_cross_origin(served):
    base, _, _ = served
    url = "/api/v1/build-cache/cleanup/preview"
    assert call(base, "POST", url, "k-op", {}, ctype="text/plain")[0] == 415
    req = urllib.request.Request(base + url, data=b"{}", method="POST", headers={
        "X-API-Key": "k-op", "Content-Type": "application/json", "Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 403


def test_apply_is_not_available_over_http(served):
    base, _, _ = served
    status, body = call(base, "POST", "/api/v1/build-cache/cleanup/execute", "k-op", {})
    assert status == 501 and "CLI" in body["error"]
    assert call(base, "POST", "/api/v1/build-cache/cleanup/execute", "k-view", {})[0] == 403
    assert call(base, "POST", "/api/v1/build-cache/status", "k-op", {})[0] == 404
    assert call(base, "GET", "/api/v1/build-cache/nope", "k-op")[0] == 404


# ---------------------------------------------------------------------------------------------- reads
def test_status_hosts_policies_have_no_paths_or_secrets(served):
    base, state, _ = served
    st = call(base, "GET", "/api/v1/build-cache/status", "k-view")[1]
    assert st["enabled"] is True and st["apply"] == "cli_only"
    assert st["ingest"]["remote_hosts"] is False
    hosts = call(base, "GET", "/api/v1/build-cache/hosts", "k-view")[1]
    assert hosts["total"] == 1 and hosts["source"] == "local_host_only"
    pol = call(base, "GET", "/api/v1/build-cache/policies", "k-view")[1]
    assert pol["policy"]["enabled"] is True and "credentials_file" not in json.dumps(pol)
    dump = json.dumps([st, hosts, pol])
    assert str(state) not in dump


def test_status_when_section_disabled_is_honest(tmp_path):
    svr, base, _ = _serve({"enabled": True, "state_dir": str(tmp_path / "s"), "fabric_build_cache": {"enabled": False}})
    try:
        assert call(base, "GET", "/api/v1/build-cache/hosts", "k-view")[1]["hosts"] == []
        assert call(base, "GET", "/api/v1/build-cache/status", "k-view")[1]["policy_enabled"] is False
    finally:
        svr.shutdown()


def test_builds_pagination_filters_and_allowlist(served):
    base, state, _ = served
    rows = []
    for i in range(1, 8):
        rows.append(_started(i, trace={"session": "sess-a" if i % 2 else "sess-b", "mission": "m1"}))
        rows.append(_finished(i, code=1 if i == 3 else 0, argv=["cargo", f"--token={SECRET}"], env={"X": SECRET},
                              trace={"session": "sess-a" if i % 2 else "sess-b", "mission": "m1"}))
    _write_builds(state, rows)
    page = call(base, "GET", "/api/v1/build-cache/builds?limit=3", "k-view")[1]
    assert [b["build_id"] for b in page["builds"]] == ["b7", "b6", "b5"]      # newest first
    assert page["next_cursor"] and page["limit"] == 3
    nxt = call(base, "GET", f"/api/v1/build-cache/builds?limit=3&cursor={page['next_cursor']}", "k-view")[1]
    assert [b["build_id"] for b in nxt["builds"]] == ["b4", "b3", "b2"]
    last = call(base, "GET", f"/api/v1/build-cache/builds?limit=3&cursor={nxt['next_cursor']}", "k-view")[1]
    assert [b["build_id"] for b in last["builds"]] == ["b1"] and last["next_cursor"] is None
    sess = call(base, "GET", "/api/v1/build-cache/builds?session=sess-b&limit=50", "k-view")[1]
    assert {b["build_id"] for b in sess["builds"]} == {"b2", "b4", "b6"}
    failed = call(base, "GET", "/api/v1/build-cache/builds?status=failed", "k-view")[1]
    assert [b["build_id"] for b in failed["builds"]] == ["b3"]
    assert call(base, "GET", "/api/v1/build-cache/builds?mission=nope", "k-view")[1]["builds"] == []
    raw = json.dumps(page)
    assert SECRET not in raw and "argv" not in raw and '"env"' not in raw
    one = call(base, "GET", "/api/v1/build-cache/builds/b3", "k-view")[1]
    assert one["build_id"] == "b3" and one["started"]["event"] == "build_started" and one["status"] == "failed"
    assert call(base, "GET", "/api/v1/build-cache/builds/missing", "k-view")[0] == 404


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=x", "cursor=zzz", "cursor=-1"])
def test_builds_bad_pagination_is_400(served, query):
    base, _, _ = served
    assert call(base, "GET", f"/api/v1/build-cache/builds?{query}", "k-view")[0] == 400


def test_builds_garbage_and_missing_log(served):
    base, state, _ = served
    assert call(base, "GET", "/api/v1/build-cache/builds", "k-view")[1]["builds"] == []
    state.mkdir(parents=True, exist_ok=True)
    (state / "builds.jsonl").write_text("not json\n[1]\n" + json.dumps(_finished(1)) + "\n")
    assert [b["build_id"] for b in call(base, "GET", "/api/v1/build-cache/builds", "k-view")[1]["builds"]] == ["b1"]


# ---------------------------------------------------------------------------------------------- preview
def _fill_cache(state, n=5, size=2048):
    cache = state / "sccache"
    cache.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        f = cache / f"obj{i}"
        f.write_bytes(b"x" * size)
        os.utime(f, (1000 + i, 1000 + i))


def test_preview_is_dry_run_no_paths_no_mutation(tmp_path):
    state = tmp_path / "state"
    section = {**SECTION, "eviction": {"hard_cap_gib": 0.000001, "max_entries_per_run": 50},
               "local": {"capacity_gib": 0.000001}}
    svr, base, _ = _serve({"enabled": True, "state_dir": str(state), "fabric_build_cache": section})
    try:
        _fill_cache(state)
        before = sorted(p.name for p in (state / "sccache").iterdir())
        status, out = call(base, "POST", "/api/v1/build-cache/cleanup/preview", "k-op", {"request_key": "rk-1"})
        assert status == 200 and out["mode"] == "dry_run" and out["apply"] == "cli_only"
        assert out["plan"]["planned_count"] >= 1
        assert all(set(c) == {"id", "bytes", "reason"} for c in out["plan"]["candidates"])
        assert str(state) not in json.dumps(out) and "obj0" not in json.dumps(out)
        assert sorted(p.name for p in (state / "sccache").iterdir()) == before       # nothing removed
        again = call(base, "POST", "/api/v1/build-cache/cleanup/preview", "k-op", {"request_key": "rk-1"})[1]
        assert again["plan_id"] == out["plan_id"] and again["request_key"] == "rk-1"   # idempotent
    finally:
        svr.shutdown()


def test_preview_rejects_unknown_fields_and_bad_key(served):
    base, _, _ = served
    url = "/api/v1/build-cache/cleanup/preview"
    assert call(base, "POST", url, "k-op", {"apply": True})[0] == 400
    assert call(base, "POST", url, "k-op", {"request_key": "x" * 200})[0] == 400
    assert call(base, "POST", url, "k-op", [1])[0] == 400


def test_preview_with_disabled_section_reports_disabled(tmp_path):
    svr, base, _ = _serve({"enabled": True, "state_dir": str(tmp_path / "s"), "fabric_build_cache": {"enabled": False}})
    try:
        out = call(base, "POST", "/api/v1/build-cache/cleanup/preview", "k-op", {})[1]
        assert out["plan"] is None and out["reason"] == "build_cache_disabled"
    finally:
        svr.shutdown()


# ---------------------------------------------------------------------------------------------- SSE events
def test_new_build_lines_become_redacted_events(served):
    base, state, repo = served
    _write_builds(state, [_started(1), _finished(1)])
    call(base, "GET", "/api/v1/build-cache/status", "k-view")               # baseline: history is not replayed
    assert not [e for e in repo.recent_events(limit=100) if e["entity_type"] == "build_cache"]
    _write_builds(state, [_started(2, trace={"session": "s1", "mission": "m1"}),
                          _finished(2, trace={"session": "s1", "mission": "m1"}, argv=[SECRET], note=SECRET)])
    call(base, "GET", "/api/v1/build-cache/builds", "k-view")
    evs = [e for e in repo.recent_events(limit=100) if e["entity_type"] == "build_cache"]
    assert sorted(e["event_type"] for e in evs) == ["build_cache.build_finished", "build_cache.build_started"]
    assert all(e["entity_id"] == "h1" for e in evs)
    assert SECRET not in json.dumps(evs) and "argv" not in json.dumps(evs)
    fin = next(e for e in evs if e["event_type"].endswith("finished"))
    assert fin["payload"]["build_id"] == "b2" and fin["payload"]["exit_code"] == 0
    assert fin["payload"]["trace"] == {"session": "s1", "mission": "m1"}
    call(base, "GET", "/api/v1/build-cache/builds", "k-view")               # same lines never published twice
    assert len([e for e in repo.recent_events(limit=100) if e["entity_type"] == "build_cache"]) == 2


def test_events_reach_the_sse_stream(served):
    base, state, repo = served
    call(base, "GET", "/api/v1/build-cache/status", "k-view")
    _write_builds(state, [_finished(9)])
    call(base, "GET", "/api/v1/build-cache/status", "k-view")
    req = urllib.request.Request(base + "/api/v1/events/stream", headers={"X-API-Key": "k-view"})
    seen = ""
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        assert r.headers.get("ETag") is None
        for _ in range(60):
            line = r.readline().decode()
            seen += line
            if "build_cache.build_finished" in seen:
                break
    assert "build_cache.build_finished" in seen
    assert "\nid:" not in "\n" + seen         # honest: the stream has no ids, so no Last-Event-ID resume


def test_log_rotation_does_not_break_event_sync(served):
    base, state, repo = served
    _write_builds(state, [_finished(1)] * 3)
    call(base, "GET", "/api/v1/build-cache/status", "k-view")
    (state / "builds.jsonl").write_text(json.dumps(_finished(5)) + "\n")      # shorter file: rotated
    call(base, "GET", "/api/v1/build-cache/status", "k-view")
    evs = [e for e in repo.recent_events(limit=100) if e["entity_type"] == "build_cache"]
    assert [e["payload"]["build_id"] for e in evs] == ["b5"]
