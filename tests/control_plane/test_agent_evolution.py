"""Agent evolution: one agent's phases and its learning record. Every value needs evidence; a gap is null with a reason, never 0/false."""
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.agent_evolution import TTLCache, build

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=8)


def stamp(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def session(sid="s1", *, branch="feat/x", start=0, end=60, status="finished", mission=None, repository="github.com/org/app", commit_sha=None):
    return {"commit_sha": commit_sha, "session_id": sid, "repository_id": repository, "branch": branch, "created_at": stamp(start), "updated_at": stamp(end),
            "status": status, "mission_id": mission, "agent_role": "coder", "runtime": "pi", "model": "gpt-4o", "host_id": "beast"}


def pull(number=7, *, branch="feat/x", minutes=30, status="merged"):
    return {"number": number, "repository": "github.com/org/app", "branch": branch, "status": status, "created_at": stamp(minutes), "updated_at": stamp(100),
            "url": f"https://github.com/org/app/pull/{number}"}


def run(number, minutes, *, state="success", branch="feat/x"):
    return {"run_number": number, "provider_repo_id": 7, "branch": branch, "revision": f"{number:040x}", "created_at": stamp(minutes), "state": state, "event": "push",
            "url": f"https://ci/{number}"}


def view(*rows):
    return {"repos": [{"id": 7, "forge_url": "https://github.com/org/app"}], "pipelines": {"org/app": list(rows)}}


def call(seq, minutes, tool="bash", args="pytest -q", status="ok", agent="main", withheld=False, duration=100):
    data = {"tool": tool, "args_preview": None if withheld else args, "status": status, "duration_ms": duration}
    if withheld:
        data["withheld"] = True
    return {"seq": seq, "kind": "tool", "agent_id": agent, "ts": stamp(minutes), "data": data}


def tools(*events, has_more=False, dropped=0):
    return {"events": list(events), "has_more": has_more, "dropped": dropped}


def ev(minutes, status, kind="SessionUpdated", seq=0):
    return {"event_type": kind, "source_timestamp": stamp(minutes), "sequence": seq, "payload": {"status": status}}


def handoff(hid, source, target, *, states=("queued", "accepted"), state="accepted"):
    return {"handoff_id": hid, "state": state, "created_at": stamp(1), "goal": "implement the parser", "completed": ["spec"], "remaining": ["tests"],
            "decisions": ["use ast"], "source": {"harness": "pi", "session_id": source}, "target": {"harness": "claude", "session_id": target},
            "history": [{"state": s, "at": stamp(1 + i)} for i, s in enumerate(states)]}


def evolve(**kw):
    args = dict(sessions=[session()], view=view(run(1, 35)), pull_requests=[pull()], handoffs=[], reviews=[], plan_reviews=[], stream=[ev(0, "running", "SessionStarted")],
                tools=tools(), spend=None, prices=False, trail_for=None, now=NOW)
    sess = args["sessions"][0]
    args.update(kw)
    return build(args.pop("session", sess), **args)


def phase(out, key):
    return next(p for p in out["phases"] if p["key"] == key)


def test_phases_are_ordered_and_only_recorded_ones_carry_items():
    out = evolve()
    assert [p["key"] for p in out["phases"]] == ["assigned", "worked", "handed_off", "produced", "verified", "outcome", "cost"]
    assert phase(out, "assigned")["items"][0]["kind"] == "session_record"
    assert phase(out, "worked")["state"] == "recorded" and [i["kind"] for i in phase(out, "worked")["items"]] == ["status"]
    assert phase(out, "handed_off")["state"] == "none" and phase(out, "handed_off")["items"] == []


def test_handoff_in_and_out_attach_by_session_id_only():
    rows = [handoff("h1", "other", "s1"), handoff("h2", "s1", "s2", states=("queued", "rejected"), state="rejected"), handoff("h3", "x", "y")]
    out = evolve(handoffs=rows)
    assert [i["handoff_id"] for i in phase(out, "assigned")["items"][1:]] == ["h1"]
    assert phase(out, "assigned")["items"][1]["completed"] == ["spec"] and phase(out, "assigned")["items"][1]["time_to_accept_s"] == 60
    assert [i["handoff_id"] for i in phase(out, "handed_off")["items"]] == ["h2"]
    lr = out["learning_record"]
    assert lr["handoffs_in"]["value"] == 1 and lr["handoffs_out"]["total"]["value"] == 1 and lr["handoffs_out"]["rejected"]["value"] == 1
    assert lr["handoffs_out"]["failed"]["value"] == 0


def test_handoff_counts_are_null_when_unreadable_or_cut_off():
    out = evolve(handoffs=None)
    lr = out["learning_record"]
    assert lr["handoffs_in"]["value"] is None and lr["handoffs_out"]["total"]["value"] is None and "could not be read" in lr["handoffs_in"]["reason"]
    assert phase(out, "handed_off")["state"] == "unknown"
    cut = evolve(handoffs=[], handoffs_truncated=True)["learning_record"]
    assert cut["handoffs_out"]["rejected"]["value"] is None and "cut off" in cut["handoffs_in"]["reason"]


def test_mission_is_a_hint_and_feeds_no_number():
    other = session("s2", branch="other", mission="m1")
    out = evolve(sessions=[session(mission="m1"), other], session=session(mission="m1"),
                 handoffs=[handoff("h9", "s2", "s3")])
    assert [h["session_id"] for h in out["hints"]["same_mission_sessions"]] == ["s2"]
    assert out["learning_record"]["handoffs_in"]["value"] == 0 and out["learning_record"]["handoffs_out"]["total"]["value"] == 0


def test_tool_calls_total_failed_retried_with_a_documented_rule():
    rows = tools(call(1, 1, status="error"), call(2, 2), call(3, 3, args="ls"), call(4, 4, args="ls", agent="other"))
    out = evolve(tools=rows)["learning_record"]["tool_calls"]
    assert out["total"]["value"] == 4 and out["failed"]["value"] == 1
    assert out["retried"]["value"] == 1 and "earlier errored call" in out["retried"]["reason"]      # call 2 repeats errored call 1; same args on another agent does not count
    assert evolve(tools=tools(call(1, 1), call(2, 2)))["learning_record"]["tool_calls"]["retried"]["value"] == 0


def test_tool_aggregates_are_null_when_the_ring_lost_events_or_nothing_is_recorded():
    for rows, text in ((tools(call(1, 1), dropped=3), "evicted"), (tools(call(1, 1), has_more=True), "only the newest"), (tools(), "No tool call"), (None, "could not be read")):
        calls = evolve(tools=rows)["learning_record"]["tool_calls"]
        assert calls["total"]["value"] is None and calls["failed"]["value"] is None and calls["retried"]["value"] is None
        assert text in calls["total"]["reason"]


def test_a_withheld_argument_makes_retries_unknown_but_not_the_total():
    calls = evolve(tools=tools(call(1, 1, status="error"), call(2, 2, withheld=True)))["learning_record"]["tool_calls"]
    assert calls["total"]["value"] == 2 and calls["failed"]["value"] == 1
    assert calls["retried"]["value"] is None and "arguments" in calls["retried"]["reason"]


def test_runs_split_on_silence_and_undated_calls_are_counted_not_placed():
    undated = {**call(9, 0), "ts": None}
    out = evolve(tools=tools(call(1, 1), call(2, 3, status="error"), call(3, 60, tool="edit"), undated))
    runs = [i for i in phase(out, "worked")["items"] if i["kind"] == "run"]
    assert [r["calls"] for r in runs] == [2, 1] and runs[0]["failed"] == 1 and runs[1]["idle_before_s"] == 57 * 60
    assert phase(out, "worked")["undated"]["tool_calls"] == 1
    assert out["learning_record"]["tool_calls"]["total"]["value"] == 4


def test_status_transitions_and_restarts_are_derived_from_the_stream():
    stream = [ev(0, "running", "SessionStarted"), ev(5, "running", seq=1), ev(10, "failed", seq=2), ev(20, "running", seq=3), ev(30, "finished", seq=4)]
    out = evolve(stream=stream)
    changes = [i for i in phase(out, "worked")["items"] if i["kind"] == "status"]
    assert [(c["from"], c["to"], c["restart"]) for c in changes] == [(None, "running", False), ("running", "failed", False), ("failed", "running", True), ("running", "finished", False)]
    assert out["restarts"]["value"] == 1
    assert evolve(stream=None)["restarts"]["value"] is None and evolve(stream=[])["restarts"]["value"] is None


def test_undated_status_events_never_sort_first():
    stream = [{"event_type": "SessionUpdated", "source_timestamp": None, "sequence": 0, "payload": {"status": "failed"}}, ev(10, "running", "SessionStarted", 1)]
    out = evolve(stream=stream)
    assert [c["to"] for c in phase(out, "worked")["items"]] == ["running"] and phase(out, "worked")["undated"]["status_events"] == 1


def test_produced_links_ci_runs_and_prs_with_a_trail_link():
    out = evolve()
    items = phase(out, "produced")["items"]
    assert [i["kind"] for i in items] == ["commit", "pull_request"] or {i["kind"] for i in items} == {"commit", "pull_request"}
    pr = next(i for i in items if i["kind"] == "pull_request")
    assert pr["trail"] == "#/pr/org/app/7?host=github.com" and pr["rule"] == "session_commits"
    assert phase(out, "verified")["items"][0]["kind"] == "ci_run" and phase(out, "verified")["items"][0]["state"] == "success"


def test_produced_says_why_when_ambiguous_or_unknown():
    shared = evolve(session=session(branch="main"), sessions=[session(branch="main")])
    assert phase(shared, "produced")["state"] == "none" and "shared branch" in phase(shared, "produced")["reason"]
    unknown = evolve(view={"available": False}, pull_requests=[])
    assert phase(unknown, "produced")["state"] == "unknown" and "not a finding" in phase(unknown, "produced")["reason"]
    assert phase(unknown, "verified")["state"] == "unknown"
    assert unknown["learning_record"]["prs"]["value"] is None


def test_pull_request_signals_are_copied_from_the_trail_not_recomputed():
    seen = []

    def trail_for(host, owner, name, number):
        seen.append((host, owner, name, number))
        return {"pull_request": {"status": "merged"}, "sessions": [{"session_id": "s1"}], "signals": {"merged_without_rework": {"value": True, "reason": "from the trail"}}}
    out = evolve(trail_for=trail_for)
    assert seen == [("github.com", "org", "app", 7)]
    prs = out["learning_record"]["prs"]["value"]
    assert prs == [{"ref": "org/app#7", "status": "merged", "trail_lists_this_agent": True,
                    "signals": {"merged_without_rework": {"value": True, "reason": "from the trail"}}}]
    assert phase(out, "outcome")["items"][1]["kind"] == "pull_request_outcome"


def test_an_unreadable_trail_makes_the_pr_list_null():
    out = evolve(trail_for=lambda *a: None)
    assert out["learning_record"]["prs"]["value"] is None and phase(out, "outcome")["pr_signals_unreadable"] is True


def test_reviews_attach_by_session_id_and_reviews_unreadable_is_not_none():
    rows = [{"review_id": "r1", "session_id": "s1", "verdict": "approved", "status": "completed", "started_at": stamp(40)},
            {"review_id": "r2", "session_id": "zz", "verdict": "changes_requested", "started_at": stamp(41)}]
    out = evolve(reviews=rows)
    assert [i["review_id"] for i in phase(out, "verified")["items"] if i["kind"] == "review"] == ["r1"]
    dark = evolve(view={"available": False}, pull_requests=[], reviews=None, plan_reviews=None)
    assert phase(dark, "verified")["state"] == "unknown"


def test_outcome_and_duration_need_a_terminal_status_and_dates():
    done = evolve()["learning_record"]
    assert done["outcome"]["value"] == "finished" and done["duration_s"]["value"] == 3600 and done["ended_at"]["value"]
    running = evolve(session=session(status="running"))["learning_record"]
    assert running["outcome"]["value"] == "running" and running["duration_s"]["value"] is None and running["ended_at"]["value"] is None
    weird = evolve(session=session(status="mystery"))
    assert weird["learning_record"]["outcome"]["value"] is None and phase(weird, "outcome")["state"] == "unknown"
    assert evolve(session=session(status="failed"))["learning_record"]["outcome"]["value"] == "failed"


def test_cost_needs_prices_a_recent_start_usage_and_every_model_priced():
    ok = {"configured": True, "total": 1.25, "by_model": [{"model": "gpt-4o"}], "unpriced": []}
    assert evolve(prices=False, spend=lambda s: ok)["learning_record"]["cost_usd_estimate"]["value"] is None
    assert evolve(prices=True, spend=lambda s: ok)["learning_record"]["cost_usd_estimate"]["value"] == 1.25
    assert phase(evolve(prices=True, spend=lambda s: ok), "cost")["state"] == "recorded"
    for result, text in ((None, "could not be read"), ({"configured": True, "by_model": [], "unpriced": []}, "No usage"),
                         ({"configured": True, "total": 1, "by_model": [{"model": "a"}], "unpriced": [{"model": "mystery"}]}, "mystery")):
        out = evolve(prices=True, spend=lambda s, r=result: r)["learning_record"]["cost_usd_estimate"]
        assert out["value"] is None and text in out["reason"]
    old = session(start=-60 * 24 * 40, end=-60 * 24 * 40 + 5)
    assert "before" in evolve(session=old, sessions=[old], prices=True, spend=lambda s: ok)["learning_record"]["cost_usd_estimate"]["reason"]


def test_caps_apply_to_displayed_rows_not_to_aggregates():
    many = tools(*[call(i, i, args=f"cmd{i}") for i in range(1, 121)])
    stream = [ev(0, "running", "SessionStarted", 0)] + [ev(i, "running" if i % 2 else "idle", seq=i) for i in range(1, 80)]
    out = evolve(tools=many, stream=stream)
    worked = phase(out, "worked")
    assert out["learning_record"]["tool_calls"]["total"]["value"] == 120
    assert len([i for i in worked["items"] if i["kind"] == "status"]) == 50 and worked["omitted"] == 29      # 79 changes, 50 shown
    many_handoffs = [handoff(f"h{i}", "s1", f"t{i}") for i in range(40)]
    out = evolve(handoffs=many_handoffs)
    assert len(phase(out, "handed_off")["items"]) == 30 and phase(out, "handed_off")["omitted"] == 10
    assert out["learning_record"]["handoffs_out"]["total"]["value"] == 40


def test_free_text_is_redacted_and_bounded_and_malformed_rows_do_not_raise():
    secret = "ghp_" + "a" * 36
    row = handoff("h1", "other", "s1")
    row["goal"] = f"use {secret} " + "x" * 1000
    out = evolve(handoffs=[row, "junk", None, {"source": "bad", "target": 5, "history": "no"}], reviews=["junk", {"session_id": "s1", "verdict": 5}],
                 tools=tools("bad", {"kind": "tool", "data": "bad"}, call(1, 1)), stream=["junk", None, ev(0, "running", "SessionStarted")])
    goal = phase(out, "assigned")["items"][1]["goal"]
    assert secret not in goal and len(goal) <= 240
    assert out["learning_record"]["tool_calls"]["total"]["value"] == 2


def test_the_pull_request_phase_never_reports_a_pr_when_nothing_links():
    out = evolve(sessions=[session(), session("s2")], session=session(), pull_requests=[pull()], view=view(run(1, 35)))
    assert phase(out, "produced")["state"] in ("none", "unknown")
    assert out["learning_record"]["prs"]["value"] in (None, [])


def test_ttl_cache_expires_and_is_bounded():
    clock = [0.0]
    cache = TTLCache(ttl=5.0, size=2, clock=lambda: clock[0])
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None and cache.get("c") == 3
    clock[0] = 6.0
    assert cache.get("c") is None


def test_a_repository_without_a_forge_host_still_links_to_its_trail_without_a_host():
    seen = []
    mine = session(repository="org/app")
    pr = {**pull(), "repository": "org/app"}
    out = evolve(session=mine, sessions=[mine], pull_requests=[pr], view={"repos": [{"id": 7, "forge_url": "org/app"}], "pipelines": {}},
                 trail_for=lambda *a: seen.append(a) or {"pull_request": {"status": "open"}, "sessions": [], "signals": {}})
    item = next(i for i in phase(out, "produced")["items"] if i["kind"] == "pull_request")
    assert item["trail"] == "#/pr/org/app/7" and seen == [(None, "org", "app", 7)]
    assert phase(out, "outcome")["items"][1]["trail_lists_this_agent"] is False       # the trail did not list this agent: said, not hidden
    bad = session(repository="not-a-repo")
    assert [i for i in phase(evolve(session=bad, sessions=[bad]), "produced")["items"] if i["kind"] == "pull_request"] == []
