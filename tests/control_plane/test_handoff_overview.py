import json
import time

from herdr_engineering.control_plane.handoffs import HandoffRegistry, OVERVIEW_ATTENTION, OVERVIEW_WORKSPACES
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = "2026-10-30T12:00:00+00:00"
OLD = "2026-10-30T09:00:00+00:00"


def registry(clock):
    repo = ControlPlaneRepo()
    return repo, HandoffRegistry(repo, now=lambda: clock[0])


def make(reg, hid, *, src=None, dst=None, goal="g", **extra):
    return reg.create({"handoff_id": hid, "source_harness": "pi", "target_harness": "pi", "goal": goal,
                       "source_session_id": src, "target_session_id": dst, **extra}, "t")[0]


def test_overview_groups_by_workspace_and_surfaces_only_what_needs_a_human():
    clock = [OLD]
    repo, reg = registry(clock)
    repo.upsert_session("s-app", repository_id="org/app")
    repo.upsert_session("s-lib", cwd="/work/lib")
    make(reg, "a1", src="s-app")
    make(reg, "a2", src="s-app")
    make(reg, "l1", src="s-lib")
    mission = repo.create_mission("Docs push")
    make(reg, "m1", mission_id=mission["mission_id"] if isinstance(mission, dict) else mission)
    make(reg, "n1")
    clock[0] = NOW
    reg.transition("a2", "claimed", {"id": "x"})
    reg.transition("a2", "failed", {"id": "x"})
    make(reg, "fresh", src="s-lib")
    ov = reg.overview()
    ws = {w["workspace"]: w for w in ov["workspaces"]}
    assert set(ws) == {"org/app", "lib", "Docs push", "Unassigned"}
    assert ws["org/app"]["total"] == 2 and ws["org/app"]["states"] == {"queued": 1, "failed": 1}
    assert ws["lib"]["total"] == 2 and ws["lib"]["attention"] == 1       # l1 is 3h queued: stalled; "fresh" is not
    assert ov["workspaces"][0]["attention"] >= ov["workspaces"][-1]["attention"]
    ids = {a["handoff_id"] for a in ov["attention"]}
    assert ids == {"a1", "a2", "l1", "m1", "n1"} and "fresh" not in ids
    assert ov["total"] == 6 and ov["attention_total"] == 5


def test_page_cursor_never_skips_or_repeats_rows_with_equal_timestamps():
    reg = registry([NOW])[1]
    for i in range(130):
        make(reg, f"h{i:03d}")
    seen, cursor = [], None
    while True:
        page = reg.page(limit=50, cursor=cursor)
        seen += [r["handoff_id"] for r in page["handoffs"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 130 and len(set(seen)) == 130


def test_page_filters_and_clamps_limit():
    reg = registry([NOW])[1]
    for i in range(5):
        make(reg, f"q{i}")
    reg.transition("q0", "cancelled", {"id": "x"})
    assert [r["handoff_id"] for r in reg.page(state="cancelled")["handoffs"]] == ["q0"]
    assert len(reg.page(limit=100000)["handoffs"]) == 5
    assert len(reg.page(limit=0)["handoffs"]) == 1
    assert reg.page(workspace="nowhere")["handoffs"] == []


def test_chain_joins_hops_on_session_ids_in_order_and_survives_cycles():
    clock = [NOW]
    repo, reg = registry(clock)
    for i, (a, b) in enumerate([("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s2")]):
        clock[0] = f"2026-10-30T11:0{i}:00+00:00"
        make(reg, f"hop{i}", src=a, dst=b)
    make(reg, "other", src="zz", dst="yy")
    out = reg.chain("hop1")
    ids = [r["handoff_id"] for r in out["chain"]]
    assert ids[out["position"]] == "hop1" and "hop0" in ids and "other" not in ids
    assert len(ids) == len(set(ids))      # s4 -> s2 closes a loop; every hop still appears once
    try:
        reg.chain("nope")
        assert False
    except KeyError:
        pass


def test_a_straight_chain_reads_in_order_from_any_hop():
    clock = [NOW]
    reg = registry(clock)[1]
    for i in range(4):
        clock[0] = f"2026-10-30T11:0{i}:00+00:00"
        make(reg, f"c{i}", src=f"t{i}", dst=f"t{i + 1}")
    for start in ("c0", "c2", "c3"):
        out = reg.chain(start)
        assert [r["handoff_id"] for r in out["chain"]] == ["c0", "c1", "c2", "c3"]
        assert out["chain"][out["position"]]["handoff_id"] == start


def test_default_view_is_bounded_at_five_thousand_handoffs():
    clock = [NOW]
    repo, reg = registry(clock)
    for w in range(300):
        repo.upsert_session(f"s{w}", repository_id=f"org/repo-{w}")
    for i in range(5000):
        hid = f"load{i:05d}"
        reg._rows[hid] = {"handoff_id": hid, "state": "failed" if i % 10 == 0 else "queued", "created_at": NOW,
                          "updated_at": f"2026-10-30T11:{i % 60:02d}:00+00:00", "goal": "x" * 5000,
                          "source": {"harness": "pi", "session_id": f"s{i % 300}"},
                          "target": {"harness": "pi"}, "completed": ["c"] * 50, "remaining": ["r"] * 50, "history": []}
    started = time.perf_counter()
    ov = reg.overview()
    pg = reg.page(limit=50)
    elapsed = time.perf_counter() - started
    assert ov["total"] == 5000 and len(ov["workspaces"]) == OVERVIEW_WORKSPACES and ov["workspaces_omitted"] == 200
    assert len(ov["attention"]) == OVERVIEW_ATTENTION and 0 < ov["attention_total"] < 5000
    size = len(json.dumps(ov)) + len(json.dumps(pg))
    assert size < 120_000, size           # the default screen fetches kilobytes, not the 25 MB of raw rows
    assert elapsed < 2.0, elapsed


def test_tree_for_one_session_is_bounded_to_that_sessions_handoffs():
    reg = registry([NOW])[1]
    make(reg, "mine-out", src="s1", dst="s2")
    make(reg, "mine-in", src="s0", dst="s1")
    make(reg, "other", src="x", dst="y")
    full, mine = reg.tree(), reg.tree(session_id="s1")
    assert len(full["edges"]) == 3
    assert {e["handoff_id"] for e in mine["edges"]} == {"mine-out", "mine-in"}
    assert reg.tree(session_id="nobody")["edges"] == []
