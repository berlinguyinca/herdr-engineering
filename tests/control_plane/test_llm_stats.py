# ruff: noqa: F811  (the public-listener fixtures are imported by name)
"""LLM page statistics (design slice 2): percentiles from histograms, the 5-sample rule,
source separation, windows, errors, and the operator-only routes."""
import datetime as dt
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane import llm_stats
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_public import stack  # noqa: F401

NOW = dt.datetime(2026, 10, 24, 12, tzinfo=dt.UTC).timestamp()
HOUR = 3600.0


def _repo():
    clock = [NOW]
    return ControlPlaneRepo(now=lambda: clock[0]), clock


def _row(**kw):
    base = {"schema": 2, "project": "p", "model": "m", "status": 200, "stream": True, "usage_known": True,
            "input_tokens": 10, "output_tokens": 100, "ttft_ms": 200, "gen_ms": 2000, "queue_ms": 0,
            "duration_ms": 2300, "route": "fabric", "session_id": "s-1"}
    base.update(kw)
    return base


def _seed(repo, clock, rows, ago=0.0):
    clock[0] = NOW - ago
    for r in rows:
        repo.record_token_usage_row(r)
    clock[0] = NOW


def test_percentiles_match_brute_force_within_a_bucket():
    values = [37, 41, 55, 90, 120, 130, 250, 400, 800, 1900, 5000, 12000]
    d = llm_stats.Dist(llm_stats._LAT_BOUNDS)
    for v in values:
        d.add(v)
    for q, rank in ((0.50, 6), (0.95, 12)):
        exact = sorted(values)[rank - 1]
        got = d.quantile(q)
        assert exact <= got <= exact * 1.13 + 1, (q, exact, got)


def test_fewer_than_five_samples_is_insufficient_data():
    repo, clock = _repo()
    _seed(repo, clock, [_row(ttft_ms=100 * i) for i in range(1, 5)])
    (g,) = repo.llm_stats("24h")["groups"]
    assert g["requests"] == 4
    assert g["ttft_ms"] == {"n": 4, "p50": None, "p95": None, "insufficient": True}
    _seed(repo, clock, [_row(ttft_ms=500)])
    (g,) = repo.llm_stats("24h")["groups"]
    assert g["ttft_ms"]["insufficient"] is False and g["ttft_ms"]["p50"] is not None


def test_speed_ttft_and_e2e_definitions():
    repo, clock = _repo()
    rows = [_row(output_tokens=100, gen_ms=2000) for _ in range(5)]                   # 50 tokens/s
    rows += [_row(stream=False, ttft_ms=None, gen_ms=None, output_tokens=60, duration_ms=3000)
             for _ in range(5)]                                                         # e2e 20 tokens/s
    rows += [_row(output_tokens=100, gen_ms=10)]                                        # too short for a speed
    rows += [_row(usage_known=False, output_tokens=None, input_tokens=None)]            # unknown, not 0
    _seed(repo, clock, rows)
    (g,) = repo.llm_stats("24h")["groups"]
    assert g["requests"] == 12
    assert g["speed_tps"]["n"] == 5 and 49 <= g["speed_tps"]["p50"] <= 57
    assert g["e2e_tps"]["n"] == 5 and 19 <= g["e2e_tps"]["p50"] <= 23
    assert g["ttft_ms"]["n"] == 7           # the five non-stream rows have no TTFT: unknown, never estimated
    assert g["tokens"]["output"] == 5 * 100 + 5 * 60 + 100
    assert g["tokens"]["lower_bound"] is True


def test_sources_are_never_summed():
    repo, clock = _repo()
    _seed(repo, clock, [_row(), _row(source="transcript", stream=None, usage_known=True)])
    groups = repo.llm_stats("24h")["groups"]
    assert sorted((g["source"], g["requests"] or g["events"]) for g in groups) == [("llm-proxy", 1), ("transcript", 1)]


def test_windows_filters_and_partial_disclosure():
    repo, clock = _repo()
    _seed(repo, clock, [_row(model="old")], ago=5 * 86400)
    _seed(repo, clock, [_row(model="new", project="q")], ago=600)
    day = repo.llm_stats("24h")
    assert [g["key"] for g in day["groups"]] == ["new"]
    assert day["partial"] is False and day["retained_since"].startswith("2026-10-19")
    week = repo.llm_stats("30d")
    assert {g["key"] for g in week["groups"]} == {"old", "new"}
    assert week["partial"] is True       # the oldest retained record is newer than the window start
    assert [g["key"] for g in repo.llm_stats("30d", project="q")["groups"]] == ["new"]
    assert [g["key"] for g in repo.llm_stats("30d", model="old")["groups"]] == ["old"]
    assert repo.llm_stats("1h")["groups"][0]["key"] == "new"
    with pytest.raises(ValueError):
        repo.llm_stats("2h")
    with pytest.raises(ValueError):
        repo.llm_stats("24h", group="nope")


def test_empty_store_is_partial_with_no_groups():
    repo, _ = _repo()
    out = repo.llm_stats("24h")
    assert out["groups"] == [] and out["partial"] is True and out["retained_since"] is None


def test_older_v1_records_still_aggregate_and_count_failures():
    repo, clock = _repo()
    repo.record_token_usage("p", "m", 3, 4, success=False, session_id="s-9")
    repo.record_token_usage("p", "m", 1, 2)
    (g,) = repo.llm_stats("24h")["groups"]
    assert (g["requests"], g["errors"], g["tokens"]["output"]) == (2, 1, 6)
    assert g["ttft_ms"]["n"] == 0
    (p,) = repo.llm_errors("24h")["problems"]
    assert p["class"] == "unclassified" and p["sessions"] == ["unmatched:s-9"]


def test_group_by_session_resolves_dashboard_ids_and_skips_rows_without_one():
    repo, clock = _repo()
    _seed(repo, clock, [_row(session_id="s-1"), _row(session_id=None), _row(session_id="zz-9")])
    keys = [g["key"] for g in repo.llm_stats("24h", group="session")["groups"]]
    assert sorted(keys) == ["unmatched:s-1", "unmatched:zz-9"]   # no such dashboard session; the row without an id is left out
    assert [g["key"] for g in repo.llm_stats("24h", group="route")["groups"]] == ["fabric"]
    (total,) = repo.llm_stats("24h", group="all")["groups"]     # one overall group: its percentiles are real, not merged
    assert (total["key"], total["requests"]) == ("all", 3)


def test_errors_group_by_class_and_model_with_sessions():
    repo, clock = _repo()
    _seed(repo, clock, [
        _row(status=429, error_class="upstream_admission", session_id="a-1", stream=None, ttft_ms=None),
        _row(status=429, error_class="upstream_admission", session_id="a-2", stream=None, ttft_ms=None),
        _row(status=502, error_class="upstream_unreachable", model="n", session_id="a-1"),
        _row(),                                            # a success is not a problem
    ])
    problems = repo.llm_errors("24h")["problems"]
    assert [(p["class"], p["model"], p["count"]) for p in problems] == [
        ("upstream_admission", "m", 2), ("upstream_unreachable", "n", 1)]
    assert problems[0]["session_count"] == 2 and problems[0]["statuses"] == {"429": 2}
    only = repo.llm_errors("24h", error_class="upstream_unreachable")["problems"]
    assert [p["class"] for p in only] == ["upstream_unreachable"]
    (g,) = [g for g in repo.llm_stats("24h")["groups"] if g["key"] == "m"]
    assert g["errors"] == 2 and g["error_rate"] == round(2 / 3, 4)


def test_timeseries_buckets_and_validation():
    repo, clock = _repo()
    _seed(repo, clock, [_row() for _ in range(3)], ago=30)
    _seed(repo, clock, [_row(status=500, error_class="upstream_server_error")], ago=3 * HOUR)
    ts = repo.llm_timeseries("24h", "1h", "requests")
    assert len(ts["points"]) == 24 and sum(p["v"] for p in ts["points"]) == 4
    assert ts["points"][-1]["v"] == 3
    assert sum(p["v"] for p in repo.llm_timeseries("24h", "1h", "errors")["points"]) == 1
    quiet = repo.llm_timeseries("24h", "1h", "ttft_p95")["points"][0]
    assert quiet["v"] is None and quiet["n"] == 0          # an empty bucket is unknown, not 0
    with pytest.raises(ValueError):
        repo.llm_timeseries("30d", "5m", "requests")        # 8640 points
    with pytest.raises(ValueError):
        repo.llm_timeseries("24h", "1h", "bogus")


def test_no_record_field_can_carry_message_text():
    repo, clock = _repo()
    _seed(repo, clock, [_row(messages=[{"content": "SECRET-BODY"}], prompt="SECRET-BODY")])
    assert "SECRET-BODY" not in json.dumps(repo.llm_stats("24h"))
    assert "SECRET-BODY" not in json.dumps(repo.export_state())


def _get(svr, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{svr.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_routes_on_the_operator_listener():
    repo, clock = _repo()
    _seed(repo, clock, [_row(), _row(status=503, error_class="upstream_admission")])
    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0, allow_open=True)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        st, body = _get(svr, "/api/v1/llm/stats?window=24h&group=model")
        assert st == 200 and body["groups"][0]["requests"] == 2
        st, body = _get(svr, "/api/v1/llm/timeseries?window=24h&step=1h&metric=errors")
        assert st == 200 and sum(p["v"] for p in body["points"]) == 1
        st, body = _get(svr, "/api/v1/llm/errors?class=upstream_admission")
        assert st == 200 and body["problems"][0]["count"] == 1
        for bad in ("/api/v1/llm/stats?window=2h", "/api/v1/llm/stats?group=x",
                    "/api/v1/llm/timeseries?step=9m", "/api/v1/llm/timeseries?window=30d&step=5m"):
            assert _get(svr, bad)[0] == 400, bad
        assert _get(svr, "/api/v1/llm/stats/extra")[0] == 404
        assert _get(svr, "/api/v1/analytics/tokens")[0] == 200   # the old route is untouched
    finally:
        svr.shutdown()


def test_spend_route_is_off_without_a_price_table_and_priced_with_one():
    repo, clock = _repo()
    _seed(repo, clock, [_row()])
    off = ControlPlaneServer(repo, host="127.0.0.1", port=0, allow_open=True)
    on = ControlPlaneServer(repo, host="127.0.0.1", port=0, allow_open=True,
                            usage_prices=[{"model": "*", "input": 1.0, "output": 1.0, "cached": None, "cached_in_input": False}])
    for svr in (off, on):
        threading.Thread(target=svr.serve, daemon=True).start()
        svr.wait_until_ready()
    try:
        assert _get(off, "/api/v1/llm/spend?window=24h") == (200, {"configured": False})
        st, body = _get(on, "/api/v1/llm/spend?window=24h")
        assert st == 200 and body["configured"] and body["estimate"] and body["source"] == "llm-proxy"
        assert body["unpriced"] == [] and body["days"] and body["total"] >= 0
        assert _get(on, "/api/v1/llm/spend?window=2h")[0] == 400
    finally:
        off.shutdown()
        on.shutdown()


@pytest.mark.parametrize("path", ["/api/v1/llm/stats", "/api/v1/llm/stats?window=24h", "/api/v1/llm/timeseries",
                                  "/api/v1/llm/errors", "/api/v1/llm/spend", "/api/v1/llm/sessions/s-1", "/api/v1/llm/samples",
                                  "/llm-view.js", "/llm"])
def test_llm_routes_do_not_exist_on_the_public_listener(stack, path):
    status, _ = stack.pub.call("GET", path)
    assert status == 404


def _transcript(repo, sid, *usages):
    repo.inspector.ingest(sid, {"events": [
        {"kind": "usage", "ts": "2026-10-24T11:00:00Z", "model": "pi-model", **u} for u in usages]})


def test_transcript_tokens_are_their_own_source_with_unknown_requests_and_timings():
    repo, clock = _repo()
    _transcript(repo, "herdr:a:b:w1%3Ap1", {"input_tokens": 10, "output_tokens": 500},
                {"input_tokens": 5, "output_tokens": 250})
    _seed(repo, clock, [_row(session_id="s-1")])
    by_source = {g["source"]: g for g in repo.llm_stats("24h", group="all")["groups"]}
    t = by_source["transcript"]
    assert t["tokens"]["output"] == 750 and t["events"] == 2 and t["partial_source"] is True
    assert (t["requests"], t["errors"], t["error_rate"]) == (None, None, None)
    assert t["ttft_ms"]["n"] == 0 and t["speed_tps"]["n"] == 0        # not reported, not 0
    assert by_source["llm-proxy"]["requests"] == 1 and by_source["llm-proxy"]["tokens"]["output"] == 100
    assert by_source["llm-proxy"]["partial_source"] is False


def test_a_session_with_proxy_records_marks_its_transcript_row_as_a_cross_check():
    repo, clock = _repo()
    sid = repo.create_session(agent_role="pi")
    other = repo.create_session(agent_role="pi")
    _transcript(repo, sid, {"output_tokens": 10})
    _transcript(repo, other, {"output_tokens": 20})
    _seed(repo, clock, [_row(session_id=sid)])
    rows = {(g["key"], g["source"]): g for g in repo.llm_stats("24h", group="session")["groups"]}
    assert rows[(sid, "transcript")]["cross_check"] is True
    assert rows[(other, "transcript")]["cross_check"] is False
    assert rows[(sid, "llm-proxy")]["cross_check"] is False


def test_timeseries_and_errors_use_the_proxy_source_only_and_source_can_be_chosen():
    repo, clock = _repo()
    _transcript(repo, "t-1", {"output_tokens": 999})
    _seed(repo, clock, [_row(), _row(status=500, error_class="upstream_server_error")])
    assert sum(p["v"] for p in repo.llm_timeseries("24h", "1h", "requests")["points"]) == 2
    assert sum(p["v"] for p in repo.llm_timeseries("24h", "1h", "tokens_out")["points"]) == 200
    assert sum(p["v"] for p in repo.llm_timeseries("24h", "1h", "tokens_out", source="transcript")["points"]) == 999
    assert [p["class"] for p in repo.llm_errors("24h")["problems"]] == ["upstream_server_error"]
    assert [g["source"] for g in repo.llm_stats("24h", group="all", source="transcript")["groups"]] == ["transcript"]
    with pytest.raises(ValueError):
        repo.llm_stats("24h", source="bogus")


def test_transcript_usage_outside_the_window_is_left_out():
    repo, _ = _repo()
    repo.inspector.ingest("t-1", {"events": [{"kind": "usage", "ts": "2026-10-10T00:00:00Z", "output_tokens": 5}]})
    assert repo.llm_stats("24h", group="all")["groups"] == []
    assert repo.llm_stats("30d", group="all")["groups"][0]["tokens"]["output"] == 5


def test_group_by_mission_queue_series_and_the_previous_window():
    repo, clock = _repo()
    _seed(repo, clock, [_row(mission_id="m-1", queue_ms=40) for _ in range(5)] + [_row(), _row(mission_id="m-2")], ago=30)
    _seed(repo, clock, [_row(mission_id="m-1") for _ in range(2)], ago=30 * HOUR)       # before the 24h window, inside the one before
    groups = {g["key"]: g["requests"] for g in repo.llm_stats("24h", "mission", source="llm-proxy")["groups"]}
    assert groups == {"m-1": 5, "m-2": 1}                       # a row with no mission is left out of this grouping
    assert repo.llm_stats("24h", "all")["groups"][0]["requests"] == 7
    assert repo.llm_stats("24h", "all", previous="1")["groups"][0]["requests"] == 2
    queue = [p["v"] for p in repo.llm_timeseries("24h", "1h", "queue_p95")["points"] if p["v"] is not None]
    assert queue and all(v >= 40 for v in queue)
