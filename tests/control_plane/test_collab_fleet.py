# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fleets started on the owner's own hosts: what the service records, who may ask, what is refused.

The host runner does the starting; this service only holds requests, per-host opt-outs and results."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import MAX_OPEN_SPAWNS, MAX_SPAWN_AGENTS, SPAWN_STALE
from tests.control_plane.test_collab_tasks import W, code, svc  # noqa: F401


@pytest.fixture
def w(svc):
    svc.max_agents_per_user = 60
    return W(svc)


def ask(w, who="alice", **kw):
    return w.s.request_spawn(getattr(w, who), {"harness_type": "claude-code", "name_prefix": "fleet", "count": 4,
                                               "room_id": w.room, "actions": ["read", "execute"], **kw})


def test_a_request_is_recorded_then_claimed_once_then_finished(w):
    r = ask(w)
    assert r["status"] == "requested" and r["count"] == 4 and r["room_id"] == w.room
    assert [x["spawn_id"] for x in w.s.list_spawns(w.alice, "requested")["spawns"]] == [r["spawn_id"]]
    spec = w.s.claim_spawn(w.alice, r["spawn_id"])
    assert spec["status"] == "claimed" and sorted(spec["actions"]) == ["execute", "read"]
    assert code(w.s.claim_spawn, w.alice, r["spawn_id"]) == 409           # a restart cannot start it twice
    done = w.s.finish_spawn(w.alice, r["spawn_id"], {"placements": [
        {"name": "fleet-1", "host": "beast", "status": "started"},
        {"name": "fleet-2", "host": "bender", "status": "failed", "error": "herdr: no such machine"}]})
    assert done["status"] == "done" and [p["host"] for p in done["placements"]] == ["beast", "bender"]
    assert code(w.s.finish_spawn, w.alice, r["spawn_id"], {"placements": []}) == 409


def test_only_the_owner_of_the_request_can_claim_or_finish_it(w):
    r = ask(w)
    assert code(w.s.claim_spawn, w.bob, r["spawn_id"]) == 404
    assert w.s.list_spawns(w.bob)["spawns"] == []
    w.s.claim_spawn(w.alice, r["spawn_id"])
    assert code(w.s.finish_spawn, w.bob, r["spawn_id"], {"placements": []}) == 404


def test_agents_cannot_start_fleets_and_outsiders_cannot_target_a_room(w):
    a, _ = w.agent("alice")
    assert code(w.s.request_spawn, a, {"harness_type": "codex", "name_prefix": "x", "count": 1}) == 403
    assert code(w.s.list_fleet_hosts, a) == 403
    assert code(ask, w, "mallory") == 404


@pytest.mark.parametrize("bad", [
    {"harness_type": "rm -rf"}, {"harness_type": "gemini"}, {"name_prefix": "-x"}, {"name_prefix": "A b"},
    {"name_prefix": "a" * 25}, {"count": 0}, {"count": MAX_SPAWN_AGENTS + 1}, {"count": True}, {"count": "3"},
    {"actions": []}, {"actions": ["root"]}, {"expires_at": 1e12}, {"room_id": 5},
])
def test_bad_requests_are_refused_before_anything_is_recorded(w, bad):
    assert code(ask, w, **bad) == 400
    assert w.s.list_spawns(w.alice)["spawns"] == []


def test_a_fleet_start_respects_the_agent_limit_and_the_queue_cap(w):
    w.s.max_agents_per_user = 5
    assert code(ask, w, count=6) == 429
    w.s.max_agents_per_user = 60
    for i in range(MAX_OPEN_SPAWNS):
        ask(w, name_prefix=f"f{i}")
    assert code(ask, w, name_prefix="more") == 429


def test_a_stuck_start_is_shown_as_stalled(w):
    r = ask(w)
    w.s.claim_spawn(w.alice, r["spawn_id"])
    w.s.clock.t += SPAWN_STALE + 1
    assert w.s.list_spawns(w.alice)["spawns"][0]["status"] == "stalled"


def test_hosts_default_to_automatic_and_an_opt_out_sticks(w):
    w.s.report_fleet_hosts(w.alice, {"hosts": [{"host": "bender", "kind": "local"}, {"host": "beast"}]})
    assert {h["host"]: (h["auto_agents"], h["online"]) for h in w.s.list_fleet_hosts(w.alice)["hosts"]} == {
        "bender": (True, True), "beast": (True, True)}
    w.s.set_fleet_host(w.alice, "beast", {"auto_agents": False})
    hosts = {h["host"]: h for h in w.s.list_fleet_hosts(w.alice)["hosts"]}
    assert hosts["beast"]["auto_agents"] is False and hosts["bender"]["kind"] == "local"
    w.s.clock.t += 3600
    assert w.s.list_fleet_hosts(w.alice)["hosts"][0]["online"] is False     # not reported lately


@pytest.mark.parametrize("name", ["", "-x", "a b", "a;b", "A", "x" * 64, "a/b"])
def test_host_names_cannot_smuggle_options_or_shell(w, name):
    assert code(w.s.set_fleet_host, w.alice, name, {"auto_agents": False}) == 400
    assert code(w.s.report_fleet_hosts, w.alice, {"hosts": [{"host": name}]}) == 400
    assert code(w.s.set_fleet_host, w.alice, "beast", {"auto_agents": "no"}) == 400


def test_state_survives_a_restart_because_it_is_replayed_from_the_log(w):
    r = ask(w)
    w.s.claim_spawn(w.alice, r["spawn_id"])
    w.s.set_fleet_host(w.alice, "beast", {"auto_agents": False})
    again = type(w.s)(w.s.auth, sink=None, now=w.s._now)
    again.hydrate(list(w.s._log))
    assert again.list_spawns(w.alice)["spawns"][0]["status"] == "claimed"
    assert again._fleet_flags == {"beast": False}


def test_http_routes_are_operator_only(w):
    body = {"harness_type": "pi", "name_prefix": "p", "count": 2}
    for method, parts in (("POST", ["fleet", "spawns"]), ("GET", ["fleet", "spawns"]), ("GET", ["fleet", "hosts"]),
                          ("POST", ["fleet", "hosts", "beast"]), ("POST", ["fleet", "hosts"])):
        s, _ = w.s.dispatch(method, parts, {}, {"auto_agents": False, "hosts": [], **body}, "Bearer tok-alice-0123456789", None)
        assert s == 404, (method, parts)                                    # a signed-in user on the public site
    s, out = w.s.dispatch("POST", ["fleet", "spawns"], {}, body, "", None, operator="alice")
    assert s == 201 and out["status"] == "requested"
    s, out = w.s.dispatch("GET", ["fleet", "spawns"], {"status": "requested"}, {}, "", None, operator="alice")
    assert s == 200 and len(out["spawns"]) == 1
    s, _ = w.s.dispatch("POST", ["fleet", "spawns", out["spawns"][0]["spawn_id"], "claim"], {}, {}, "", None, operator="alice")
    assert s == 200
