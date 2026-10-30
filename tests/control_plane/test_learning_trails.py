"""Durable pull-request trails for the learning loop (``learning_trails``): assembly once per end, null propagation, bounds, export."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.learning_trails import (
    ENTITY, EVENT, LATE, TrailRecords, learning_config, parse_repository)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000


def sig(value=None, reason="no evidence"):
    return {"value": value, "reason": reason}


def fake_trail(extra=None):
    body = {
        "pull_request": {"number": 7, "repository": "org/app", "status": "merged", "url": "https://github.com/org/app/pull/7",
                         "branch": "feat/x", "opened_at": "2026-10-01T09:00:00+00:00"},
        "state": "linked", "reason": "ok", "sources": {"ci": "unavailable", "handoffs": "ok"}, "omitted": {"sessions": 0},
        "sessions": [{"session_id": "s1", "rule": "branch_window", "inferred": False}],
        "inferred_sessions": [{"session_id": "hint"}], "ambiguous": [], "handoffs": [{"handoff_id": "h1", "state": "accepted"}],
        "reviews": [], "ci_runs": [], "timeline": [{"at": "x"}],
        "signals": {"merged_without_rework": sig(None, "CI data is unavailable."), "duration_s": sig(120, "reported end")},
    }
    body.update(extra or {})
    return body


def fake_evolution(session_id):
    return {"learning_record": {"session_id": session_id, "tool_calls": {"total": sig(None, "tool events were lost")},
                                "prs": [{"ref": "org/app#7", "signals": {"merged_without_rework": sig(True, "x")}}],
                                "outcome": sig("finished", "terminal status")}}


def world(*, trail=None, evolution=fake_evolution, **cfg):
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    calls = []

    def trail_for(host, owner, name, number):
        calls.append((host, owner, name, number))
        return trail() if callable(trail) else (fake_trail() if trail is None else trail)

    records = TrailRecords(repo, learning_config(cfg or None), trail_for=trail_for, evolution_for=evolution, now=lambda: clock[0])
    return repo, records, clock, calls


def end_pr(repo, status="merged", **kw):
    return repo.upsert_pull_request("pr-1", repository="github.com/org/app", number=7, status=status, branch="feat/x", **kw)


def stored(repo, event_type=EVENT, pr_id="pr-1"):
    return [e for e in repo.stream(ENTITY, pr_id) if e["event_type"] == event_type]


# ---------------------------------------------------------------- assembly
def test_an_ended_pr_gets_one_record_with_signals_agents_and_sources():
    repo, records, _, calls = world()
    end_pr(repo)
    assert records.sweep(force=True) == 1
    (event,) = stored(repo)
    p = event["payload"]
    assert p["schema_version"] == 1 and p["kind"] == "pull_request_trail" and p["pr_id"] == "pr-1"
    assert p["ended_source"] == "observed" and p["ended_at"] == repo.get_pull_request("pr-1")["merged_at"]
    assert p["signals"]["merged_without_rework"] == sig(None, "CI data is unavailable.")
    assert p["sources"]["ci"] == "unavailable" and p["sources"]["agent_evolution"]["state"] == "ok"
    assert [a["session_id"] for a in p["agents"]] == ["s1"] and p["agents"][0]["rule"] == "branch_window"
    assert "prs" not in p["agents"][0]["learning_record"] and p["agents"][0]["learning_record"]["tool_calls"]["total"]["value"] is None
    assert p["inferred_sessions_count"] == 1 and "timeline" not in p and "inferred_sessions" not in p
    assert p["pull_request"]["host"] == "github.com" and p["pull_request"]["repository"] == "org/app"
    assert calls == [("github.com", "org", "app", 7)]
    assert event["entity_type"] == ENTITY


def test_assembly_happens_once_per_end_and_a_replay_or_second_sweep_writes_nothing():
    repo, records, _, calls = world()
    end_pr(repo)
    assert records.sweep(force=True) == 1
    assert records.sweep(force=True) == 0
    end_pr(repo)                                  # the same end observed again
    assert records.sweep(force=True) == 0 and len(stored(repo)) == 1 and len(calls) == 1
    fresh = ControlPlaneRepo(now=lambda: NOW)
    fresh.import_state(repo.export_state())
    again = TrailRecords(fresh, learning_config(None), trail_for=lambda *a: fake_trail(), evolution_for=fake_evolution, now=lambda: NOW)
    assert again.sweep(force=True) == 0 and len(stored(fresh)) == 1, "the stored record survives a restart and is not written twice"


def test_a_reopened_pr_that_ends_again_gets_a_second_record_not_a_rewrite():
    repo, records, _, _ = world()
    end_pr(repo, merged_at="2026-10-01T10:00:00Z")
    records.sweep(force=True)
    end_pr(repo, status="open")
    end_pr(repo, status="merged", merged_at="2026-10-02T10:00:00Z")
    assert records.sweep(force=True) == 1
    assert [e["payload"]["ended_at"] for e in stored(repo)] == ["2026-10-01T10:00:00Z", "2026-10-02T10:00:00Z"]
    assert [e["payload"]["end_seq"] for e in stored(repo)] == [1, 2] and [e["payload"]["learning_seq"] for e in stored(repo)] == [0, 1]


def test_open_prs_and_prs_without_an_end_time_are_not_assembled():
    repo, records, _, calls = world()
    repo.upsert_pull_request("open", repository="github.com/org/app", number=1, status="open")
    legacy = repo.upsert_pull_request("old", repository="github.com/org/app", number=2, status="merged")
    repo._pull_requests["old"]["merged_at"] = None            # a record from before end times were stored
    assert legacy["merged_at"]
    assert records.sweep(force=True) == 0 and calls == [] and repo.entity_ids(ENTITY) == []


def test_null_propagation_nothing_is_replaced_by_a_zero_and_unreadable_agents_are_named():
    def evolution(session_id):
        return None
    repo, records, _, _ = world(evolution=evolution)
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert p["signals"]["merged_without_rework"]["value"] is None
    assert p["agents"][0]["learning_record"] is None and "could not be read" in p["agents"][0]["reason"]
    assert p["sources"]["agent_evolution"]["state"] == "partial"
    unwired = world(evolution=None)
    end_pr(unwired[0])
    unwired[1].sweep(force=True)
    q = stored(unwired[0])[0]["payload"]
    assert q["agents"] is None and q["sources"]["agent_evolution"]["state"] == "unavailable"


def test_agents_are_capped_but_the_omission_is_recorded():
    many = fake_trail({"sessions": [{"session_id": f"s{i}", "rule": "commit"} for i in range(5)]})
    repo, records, _, _ = world(trail=many, max_agents=2)
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert len(p["agents"]) == 2 and p["omitted"]["agents"] == 3
    assert p["sources"]["agent_evolution"]["state"] == "partial" and "max_agents" in p["sources"]["agent_evolution"]["reason"]


def test_an_unreadable_trail_writes_nothing_and_is_retried_later_not_every_sweep():
    repo, records, clock, calls = world(trail=lambda: None)
    end_pr(repo)
    assert records.sweep(force=True) == 0 and repo.entity_ids(ENTITY) == []
    assert records.sweep(force=True) == 0 and len(calls) == 1, "backed off"
    clock[0] += 3601
    assert records.sweep(force=True) == 0 and len(calls) == 2


def test_an_oversize_record_drops_whole_lists_and_says_which():
    big = fake_trail({"ci_runs": [{"state": "x" * 400, "n": i} for i in range(1000)], "handoffs": [{"goal": "y" * 400}] * 600})
    repo, records, _, _ = world(trail=big)
    end_pr(repo)
    records.sweep(force=True)
    p = stored(repo)[0]["payload"]
    assert len(json.dumps(p)) <= 131072 + 4096 and p["ci_runs"] is None and "ci_runs" in p["trimmed"]


def test_redaction_everything_is_cleaned_again_and_no_raw_tool_arguments_appear():
    leaky = fake_trail({"handoffs": [{"handoff_id": "h", "goal": "use password=hunter2hunter2 now"}], "reason": "token=" + "a" * 40})
    repo, records, _, _ = world(trail=leaky)
    end_pr(repo)
    records.sweep(force=True)
    text = json.dumps(stored(repo)[0]["payload"])
    assert "hunter2hunter2" not in text and "a" * 40 not in text and "args_preview" not in text


def test_disabled_writes_nothing():
    repo, records, _, calls = world(enabled=False)
    end_pr(repo)
    assert records.sweep(force=True) == 0 and calls == []


def test_the_sweep_is_throttled_and_batched():
    repo, records, clock, _ = world(sweep_batch=2)
    for n in range(5):
        repo.upsert_pull_request(f"pr-{n}", repository="github.com/org/app", number=10 + n, status="merged", merged_at="2026-10-01T10:00:00Z")
    assert records.sweep() == 2
    assert records.sweep() == 0, "throttled"
    clock[0] += 301
    assert records.sweep() == 2


# ---------------------------------------------------------------- late evidence
def test_a_reported_end_time_after_an_observed_one_adds_one_late_evidence_event_and_leaves_the_record_alone():
    repo, records, _, _ = world()
    end_pr(repo)
    records.sweep(force=True)
    record = json.dumps(stored(repo)[0]["payload"], sort_keys=True)
    end_pr(repo, merged_at="2026-10-01T10:00:00Z")      # the stored observed time is never overwritten by a later look ...
    assert repo.get_pull_request("pr-1")["ended_source"] == "reported"      # ... a reported one replaces it
    records.sweep(force=True)
    records.sweep(force=True)
    (late,) = stored(repo, LATE)
    assert late["payload"]["evidence"]["merged_at"] == "2026-10-01T10:00:00Z" and late["payload"]["evidence"]["ended_source"] == "reported"
    assert json.dumps(stored(repo)[0]["payload"], sort_keys=True) == record


def test_late_evidence_keeps_only_whitelisted_keys_and_needs_a_record():
    repo, records, _, _ = world()
    end_pr(repo)
    records.sweep(force=True)
    out = records.late_evidence("pr-1", 1, {"note": "token=" + "b" * 40, "evil": "x"})
    assert set(out["evidence"]) == {"note"} and "b" * 40 not in out["evidence"]["note"]
    with pytest.raises(KeyError):
        records.late_evidence("pr-1", 9, {"note": "n"})
    with pytest.raises(ValueError):
        records.late_evidence("pr-1", 1, {"evil": "x"})


# ---------------------------------------------------------------- export
def seeded(n=5):
    repo, records, clock, _ = world(page_default=2, page_max=3)
    for i in range(n):
        repo.upsert_pull_request(f"pr-{i}", repository="github.com/org/app", number=10 + i, status="merged", merged_at="2026-10-01T10:00:00Z")
    records.sweep(force=True)
    return repo, records, clock


def test_export_pages_oldest_first_with_a_cursor_that_survives_appends():
    repo, records, clock = seeded(5)
    first = records.export()
    assert first["count"] == 2 and first["has_more"] and [r["learning_seq"] for r in first["records"]] == [0, 1]
    repo.upsert_pull_request("pr-late", repository="github.com/org/app", number=99, status="merged", merged_at="2026-10-02T10:00:00Z")
    clock[0] += 301
    records.sweep()
    second = records.export(cursor=first["next_cursor"], limit=3)
    assert [r["learning_seq"] for r in second["records"]] == [2, 3, 4] and second["has_more"]
    third = records.export(cursor=second["next_cursor"], limit=3)
    assert [r["pr_id"] for r in third["records"]] == ["pr-late"] and not third["has_more"]
    assert records.export(cursor=third["next_cursor"])["records"] == []


def test_export_since_filters_on_assembly_time_and_rejects_bad_parameters():
    repo, records, _ = seeded(2)
    assert records.export(since="2999-01-01T00:00:00Z")["records"] == []
    assert records.export(since="2000-01-01T00:00:00Z", limit=3)["count"] == 2
    for kw in ({"since": "yesterday"}, {"limit": 0}, {"limit": 4}, {"limit": True}, {"cursor": "abc"}, {"cursor": "s-1"}, {"cursor": 7}):
        with pytest.raises(ValueError):
            records.export(**kw)


def test_the_counter_continues_after_a_restart():
    repo, records, _ = seeded(3)
    fresh = ControlPlaneRepo(now=lambda: NOW)
    fresh.import_state(repo.export_state())
    again = TrailRecords(fresh, learning_config(None), trail_for=lambda *a: fake_trail(), evolution_for=fake_evolution, now=lambda: NOW)
    fresh.upsert_pull_request("pr-new", repository="github.com/org/app", number=50, status="merged", merged_at="2026-10-03T10:00:00Z")
    again.sweep(force=True)
    assert [r["learning_seq"] for r in again.export(limit=3, cursor="s1")["records"]] == [2, 3]


# ---------------------------------------------------------------- config and repository parsing
@pytest.mark.parametrize("raw,message", [
    ("x", "must be a mapping"), ({"nope": 1}, "unknown keys: nope"), ({"enabled": "yes"}, "enabled must be true or false"),
    ({"max_agents": 0}, "max_agents must be a whole number from 1 to 50"), ({"sweep_batch": True}, "sweep_batch"),
    ({"page_default": 300, "page_max": 100}, "page_default must not exceed page_max")])
def test_config_is_validated(raw, message):
    with pytest.raises(ValueError, match=message):
        learning_config(raw)


def test_config_defaults_and_repository_parsing():
    assert learning_config(None) == {"enabled": True, "max_agents": 12, "sweep_batch": 20, "page_default": 50, "page_max": 200}
    assert parse_repository("github.com/org/app") == ("github.com", "org", "app")
    assert parse_repository("org/app") == (None, "org", "app")
    assert parse_repository("https://ghe.example/org/app.git") == ("ghe.example", "org", "app")
    assert parse_repository("nonsense") is None and parse_repository(None) is None and parse_repository("a/b/c/d") is None


# ---------------------------------------------------------------- HTTP
class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()


@pytest.fixture()
def api(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True, learning={"page_default": 2, "page_max": 3})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    repo.upsert_session("sx", host_id="h", agent_role="pi", repository_id="github.com/org/app", branch="feat/x", status="finished")
    for n in range(4):
        repo.upsert_pull_request(f"pr-{n}", repository="github.com/org/app", number=7 + n, status="merged", branch="feat/x",
                                 merged_at="2026-10-01T10:00:00Z")
    yield Client(svr.port), repo, svr
    svr.shutdown()


def test_the_endpoint_assembles_lazily_and_pages_with_a_cursor(api):
    c, repo, _ = api
    status, headers, body = c.get("/api/v1/learning/trails")
    out = json.loads(body)
    assert status == 200 and out["count"] == 2 and out["has_more"] and out["schema_version"] == 1
    assert out["records"][0]["kind"] == "pull_request_trail" and out["records"][0]["pull_request"]["repository"] == "org/app"
    status, _, body = c.get(f"/api/v1/learning/trails?limit=3&cursor={out['next_cursor']}")
    nxt = json.loads(body)
    assert status == 200 and [r["learning_seq"] for r in nxt["records"]] == [2, 3] and not nxt["has_more"]


def test_the_jsonl_export_is_one_object_per_line_with_the_same_bounds_and_a_cursor_header(api):
    c, _, _ = api
    status, headers, body = c.get("/api/v1/learning/trails.jsonl?limit=3")
    lines = body.decode().splitlines()
    assert status == 200 and headers["Content-Type"] == "application/x-ndjson" and headers["X-Has-More"] == "true"
    rows = [json.loads(line) for line in lines]
    assert len(rows) == 3 and headers["X-Next-Cursor"] == rows[-1]["cursor"] and body.endswith(b"\n")
    status, headers, body = c.get(f"/api/v1/learning/trails.jsonl?cursor={headers['X-Next-Cursor']}")
    assert [json.loads(x)["learning_seq"] for x in body.decode().splitlines()] == [3] and headers["X-Has-More"] == "false"
    status, headers, body = c.get(f"/api/v1/learning/trails.jsonl?cursor={headers['X-Next-Cursor']}")
    assert status == 200 and body == b"" and headers["X-Next-Cursor"].startswith("s")


@pytest.mark.parametrize("query", ["limit=0", "limit=4", "limit=abc", "limit=", "limit=-1", "since=yesterday", "since=", "cursor=zzz", "cursor=s"])
def test_bad_parameters_are_400_on_both_forms(api, query):
    c, _, _ = api
    assert c.get(f"/api/v1/learning/trails?{query}")[0] == 400
    assert c.get(f"/api/v1/learning/trails.jsonl?{query}")[0] == 400


def test_disabled_is_404_and_unknown_subpaths_are_404(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True, learning={"enabled": False})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        c = Client(svr.port)
        assert c.get("/api/v1/learning/trails")[0] == 404 and c.get("/api/v1/learning/trails.jsonl")[0] == 404
        assert c.get("/api/v1/learning/other")[0] == 404
    finally:
        svr.shutdown()


def test_the_maintenance_sweep_writes_records_without_any_read(api):
    _, repo, svr = api
    svr.learning_trails._swept = 0.0
    svr.sweep_once()
    assert len(repo.entity_ids(ENTITY)) == 4 and svr.learning_trails.export(limit=3)["count"] == 3


def test_the_server_rejects_a_bad_learning_config(acknowledged_sink):
    with pytest.raises(ValueError, match="control_plane.learning"):
        ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), scheduler_admission=True, learning={"page_max": 0})
