# ruff: noqa: F811  (pytest fixtures are imported by name)
"""T16 security audit, Herdr side: one regression test per finding (each fails without its fix).

H1/M1/M5 ingest allow-list + collab startup quarantine, H2 safety headroom, M2 since_join on every read,
M3/M15 uniform hot fail-closed switches, M4 ingest sources, M6 per-principal gap state, M7 trusted writers
survive the contribution cap, M9 quorum head redaction, M12 rate limiter client identity, L1.
"""
from __future__ import annotations

import copy
import http.client
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime

import pytest
import yaml

from herdr_engineering import cli
from herdr_engineering.control_plane import collab_insights as ins
from herdr_engineering.control_plane import envelope as envelope_mod
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import DurableSource, load_into
from herdr_engineering.control_plane.collab import (
    BULK_EVENTS,
    SAFETY_EVENTS,
    CollabError,
    CollabService,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.ingest import (
    DISPATCHED_ENTITY_TYPES,
    INGEST_ENTITY_TYPES,
    IngestService,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigStore
from tests.control_plane.test_attention_progress import (  # noqa: F401
    PR1,
    edge,
    fx,
    node,
    prog,
    task_row,
    tick,
)
from tests.control_plane.test_collab import Clock, _digest
from tests.control_plane.test_collab_links import link, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401
from tests.control_plane.test_graph import OPERATOR
from tests.control_plane.test_graph_membership import fab, put, read  # noqa: F401
from tests.control_plane.test_review_quorum import PI, PR, World, cfg
from tests.test_central_config_servers import COLLAB, make_file

KEY = {"kind": "ingest", "id": "key:aaaaaaaaaaaa", "authenticated_by": "api_key"}
KEY2 = {"kind": "ingest", "id": "key:bbbbbbbbbbbb", "authenticated_by": "api_key"}


def v2(entity_type, entity_id, event_type, payload, n=0, source="pi", event_id=None):
    return {"schema_version": 2, "event_id": event_id or f"{source}-{n}", "source": source, "source_sequence": n,
            "entity_type": entity_type, "entity_id": entity_id, "event_type": event_type, "payload": payload}


# ======================================================================== H1 / M1 / M5: ingest allow-list
class Sink:
    """Behaves like the durable sink: acknowledged rows survive a restart."""

    def __init__(self):
        self.rows, self.seen = [], set()

    def record_required(self, ev):
        key = (((ev.get("actor") or {}).get("id")) or "", ev["event_id"])
        if key in self.seen:
            raise RuntimeError("duplicate")
        self.seen.add(key)
        self.rows.append(copy.deepcopy(ev))

    record = record_required


class Src(DurableSource):
    def __init__(self, rows):
        self._rows = rows

    def missions(self): return []
    def sessions(self): return []
    def services(self): return []
    def hosts(self): return []
    def events(self): return self._rows


@pytest.mark.parametrize("entity_type", ["collab", "collab_room", "ci_observation", "ci_report", "ci_telemetry",
                                         "plannotator_review", "ci_pipeline", "agent_identity", "agent_summary",
                                         "custom_model", "llm_key", "artifact", "made-up",
                                         "pr_gate", "pr_gate_verdict", "review_job"])
@pytest.mark.parametrize("schema", [1, 2])
def test_ingest_refuses_every_entity_type_that_is_not_on_the_allow_list(entity_type, schema):
    svc = IngestService(ControlPlaneRepo())
    body = {"entity_type": entity_type, "entity_id": "log", "event_type": "x.y", "payload": {}}
    if schema == 2:
        body = {**body, **v2(entity_type, "log", "x.y", {})}
    with pytest.raises(ValueError):
        svc.ingest(body, KEY)
    assert svc.repo.stream(entity_type, "log") == []


def test_the_allow_list_is_exactly_what_the_dispatcher_handles_plus_plain_activity_types():
    # Delivery entities (issue #32) join the dispatched set; they are validated
    # and stored, not silently recorded as plain activity.
    assert DISPATCHED_ENTITY_TYPES == {"lineage", "host", "session", "mission", "service", "inspector",
                                       "mission_activity", "test_run", "build", "review",
                                       "review_finding", "pull_request", "token_usage", "resource_usage"}
    assert INGEST_ENTITY_TYPES - DISPATCHED_ENTITY_TYPES == {"task", "note", "workspace", "ledger",
                                                                        "platform", "lifecycle",
                                                                        "repository", "request", "span"}


@pytest.mark.parametrize("entity_type", ["repository", "request", "span"])
def test_an_ingest_key_may_still_post_the_system_graph_adapter_streams(entity_type):
    """main's opt-in system-graph adapter dispatches these through generic ingest (they were accepted before T16)."""
    svc = IngestService(ControlPlaneRepo(), system_graph=True)
    out = svc.ingest({"entity_type": entity_type, "entity_id": "r-1", "event_type": "x.observed",
                      "payload": {"request_id": "r-1"}}, KEY)
    assert out["entity_type"] == entity_type
    assert [e["event_type"] for e in svc.repo.stream(entity_type, "r-1")] == ["x.observed"]


@pytest.mark.parametrize("entity_type", ["pr_gate", "pr_gate_verdict", "review_job", "Review_Job_resume"])
def test_reserved_review_gate_prefixes_stay_refused_even_if_the_allow_list_ever_names_them(monkeypatch, entity_type):
    import herdr_engineering.control_plane.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "INGEST_ENTITY_TYPES", ingest_mod.INGEST_ENTITY_TYPES | {entity_type})
    svc = IngestService(ControlPlaneRepo())
    with pytest.raises(ValueError, match="reserved review-gate"):
        svc.ingest({"entity_type": entity_type, "entity_id": "pr-1", "event_type": "verdict", "payload": {}}, KEY)
    assert svc.repo.stream(entity_type, "pr-1") == []


def test_forged_ci_result_cannot_replace_the_server_written_observation():
    """M1: ci_* streams are written by the CI adapters only."""
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    repo.record_event("ci_observation", "pipe-1", "CIPipelineObserved", {"status": "failure"})
    for n, et in enumerate(("ci_observation", "ci_report", "ci_telemetry")):
        with pytest.raises(ValueError):
            svc.ingest(v2(et, "pipe-1", "CIPipelineObserved", {"status": "success", "conclusion": "success"}, n), KEY)
    assert [e["payload"]["status"] for e in repo.stream("ci_observation", "pipe-1")] == ["failure"]


def test_forged_session_receipt_cannot_suppress_message_delivery():
    """M5: SessionCommandQueued/Settled are the server's idempotency receipts; a client may not name them."""
    repo = ControlPlaneRepo(sink=Sink())
    svc = IngestService(repo)
    for n, et in enumerate(("SessionCommandQueued", "SessionCommandSettled")):
        with pytest.raises(ValueError):
            svc.ingest(v2("session", "s1", et, {"operation_id": "op1", "status": "delivered"}, n), KEY)
        with pytest.raises(ValueError):                                       # and as a v1 body
            svc.ingest({"entity_type": "session", "entity_id": "s1", "event_type": et, "payload": {}}, KEY)
    repo.upsert_session("s1", host_id="h", mission_id=None, agent_role="r", model="m", status="active")
    calls = []
    repo.send_message("s1", "hello", operation_id="op1",
                      dispatcher=lambda *a: calls.append(a) or {"status": "delivered"})
    assert len(calls) == 1                                                  # nothing pre-empted the delivery
    svc.ingest(v2("session", "s1", "session.started", {"status": "active"}, 9), KEY)       # honest events still work


def test_a_forged_collab_row_never_reaches_the_log_and_the_restart_succeeds():
    """H1: the exact audit repro. An ingest key writes 'collab' rows; the next restart must not break."""
    sink = Sink()
    repo = ControlPlaneRepo(sink=sink)
    collab = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    cast = W(collab)
    forged = {"entity_type": "collab", "entity_id": "log", "event_type": "member_added",
              "payload": {"room_id": cast.room, "subject_user_id": "mallory", "role": "owner"}}
    svc = IngestService(repo)
    with pytest.raises(ValueError):
        svc.ingest(forged, KEY)
    with pytest.raises(ValueError):
        svc.ingest({**forged, **v2("collab", "log", "member_added", forged["payload"], 0)}, KEY)
    assert {r["entity_type"] for r in sink.rows} == {"collab"} and all("source" not in r for r in sink.rows)
    repo2 = ControlPlaneRepo()
    load_into(repo2, Src(copy.deepcopy(sink.rows)))
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    assert again.hydrate(repo2.take_collab_replay()) == len(collab._log)
    assert repo2.collab_quarantine == []


def test_rows_already_stored_by_the_old_deny_list_are_quarantined_not_replayed(caplog):
    """H1: a database poisoned BEFORE the fix (forged v2 'collab' rows with sequence 0 / foreign stream ids)
    must still start; the foreign rows are kept for the operator and never applied."""
    sink = Sink()
    collab = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    W(collab)
    good = len(sink.rows)
    sink.rows.append({"entity_type": "collab", "entity_id": "log", "sequence": 0, "event_type": "member_added",
                      "event_id": "e1", "source": "pi", "source_sequence": 0, "actor": KEY,
                      "payload": {"room_id": "r", "subject_user_id": "mallory", "role": "owner"}})
    sink.rows.append({"entity_type": "collab", "entity_id": "other-stream", "sequence": 1,
                      "event_type": "member_added", "event_id": "e2", "payload": {}})
    repo = ControlPlaneRepo()
    with caplog.at_level(logging.ERROR):
        load_into(repo, Src(copy.deepcopy(sink.rows)))
    assert len(repo.collab_quarantine) == 2 and any("quarantined" in r.message for r in caplog.records)
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    assert fresh.hydrate(repo.take_collab_replay()) == good


def test_a_corrupt_collab_log_disables_collaboration_only_with_an_operator_visible_error(caplog):
    """H1: a genuinely non-contiguous collab log must not brick the control plane; collaboration alone is
    switched off (every request 503s, no write) and the error is logged."""
    sink = Sink()
    collab = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    W(collab)
    rows = [r for r in sink.rows if r["sequence"] != 2]                  # a hole in the middle
    repo = ControlPlaneRepo()
    load_into(repo, Src(rows))
    broken = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    with caplog.at_level(logging.ERROR):
        server = ControlPlaneServer(repo, collab=broken, allow_open=True)
    server.shutdown()
    assert broken.degraded and "integrity" in broken.degraded
    assert any("DISABLED" in r.message for r in caplog.records)
    with pytest.raises(CollabError) as exc:
        broken.authenticate("Bearer " + USERS["alice"], None)
    assert exc.value.status == 503
    with pytest.raises(CollabError) as exc:
        broken._commit("project_created", {})
    assert exc.value.status == 503


# ======================================================================== H2: safety events keep headroom
def _small(cap):
    clock = Clock()
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=clock,
                        max_log_events=cap, max_agents_per_user=20)
    return svc, clock, W(svc)


def _fill(svc, clock, cast, who):
    n = 0
    try:
        while True:
            svc.post_message(who, cast.room, {"text": "x", "idempotency_key": f"k{n}"})
            n += 1
            if n % 50 == 0:
                clock.t += 61
    except CollabError as exc:
        assert exc.status == 503
    return n


def test_a_contributor_filling_the_log_cannot_block_removing_them():
    svc, clock, cast = _small(300)
    posts = _fill(svc, clock, cast, cast.bob)
    assert posts > 0
    assert len(svc._log) < 300                              # chat stopped EARLY, headroom was kept
    assert svc.stats["log_headroom_refused"] >= 1
    svc.remove_member(cast.alice, cast.room, "bob")        # the safety event is still admitted
    assert "bob" not in svc._rooms[cast.room]["members"]


def test_non_safety_writes_stop_before_safety_writes_and_the_tiers_are_ordered():
    svc, _, _ = _small(1000)
    bulk, ordinary, safety = (svc._log_limit(t) for t in ("message", "room_created", "member_removed"))
    assert bulk < ordinary < safety == 1000
    assert "message" in BULK_EVENTS and not (BULK_EVENTS & SAFETY_EVENTS)
    assert SAFETY_EVENTS == {"member_removed", "agent_revoked", "user_decided", "join_token_revoked", "xlink_revoked",
                             "workspace_unlinked", "mission_unlinked", "agent_grant_revoked",
                             "helpers_paused", "helper_cancelled", "egress_removed",
                             "federation_share_revoked", "federation_peer_disabled",      # ADR-0008 P2
                             "team_detached",                                              # team rooms slice 1
                             "team_role_cleared"}                                          # team roles slice 2
    svc._log.extend([{}] * (ordinary - len(svc._log)))        # exactly at the ordinary limit
    with pytest.raises(CollabError):
        svc._commit("room_created", {})
    assert svc._log_limit("member_removed") > len(svc._log)


def test_an_agent_revoke_still_works_on_a_log_that_refuses_ordinary_writes():
    svc, clock, cast = _small(300)
    _, info = cast.agent("alice")
    _fill(svc, clock, cast, cast.bob)
    svc._log.extend([{}] * (svc._log_limit("room_created") - len(svc._log)))
    with pytest.raises(CollabError):
        svc.add_member(cast.alice, cast.room, {"user_id": "mallory", "role": "viewer"})
    svc.revoke_agent(cast.alice, info["agent_id"])           # the safety event is admitted
    assert svc._agents[info["agent_id"]]["status"] != "active"


def _at_the_ordinary_limit(s):
    """Shrink the cap so the log sits exactly at the limit of ordinary writes (the safety reserve is all that is left)."""
    n = len(s._log)
    for cap in range(n + 1, 4 * n + 50):
        s.max_log_events = cap
        if s._log_limit("room_created") == n:
            return cap
    raise AssertionError("no cap found")


def _flow_member(w):
    return lambda: w.s.remove_member(w.alice, w.room, "bob")


def _flow_agent(w):
    _, info = w.agent("alice")
    return lambda: w.s.revoke_agent(w.alice, info["agent_id"])


def _flow_external(w):
    inv = w.s.create_external_invite(w.alice, w.room, {"label": "Dana"})
    out = w.s.redeem_join_token({"token": inv["code"]})
    return lambda: w.s.revoke_external(w.alice, w.room, out["agent_id"])


def _flow_join_token(w):
    inv = w.s.create_external_invite(w.alice, w.room, {"label": "Dana"})
    return lambda: w.s.revoke_external_invite(w.alice, w.room, inv["invite_id"])


def _flow_workspace(w):
    lk = link(w)
    return lambda: w.s.unlink_workspace(w.alice, w.room, lk["link_id"])


def _flow_mission(w):
    w.s.link_mission(w.alice, w.room, {"mission_id": "m-1", "host": "bender"})
    return lambda: w.s.unlink_mission(w.alice, w.room, "m-1")


def _flow_role(w):
    from herdr_engineering.control_plane.collab_team_roles import normalize_team_roles
    from herdr_engineering.control_plane.collab_team_rooms import normalize_team_rooms
    w.s.team_rooms = normalize_team_rooms({"enabled": True})
    w.s.team_roles = normalize_team_roles({"enabled": True})
    info = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})
    team = "team-" + "b" * 20
    w.s.attach_team(w.alice, w.room, team, {"roles": {"implementer": [info["agent_id"]]}})
    w.s.assign_team_role(w.alice, w.room, team, {"role": "tester", "holder": "user:" + w.alice.id})
    return lambda: w.s.clear_team_role(w.alice, w.room, team, {"role": "tester", "holder": "user:" + w.alice.id})


def _flow_team(w):
    from herdr_engineering.control_plane.collab_team_rooms import normalize_team_rooms
    w.s.team_rooms = normalize_team_rooms({"enabled": True})
    info = w.s.connect_agent(w.alice, {"harness_type": "claude-code"})        # not yet granted in the room
    team = "team-" + "a" * 20
    w.s.attach_team(w.alice, w.room, team, {"roles": {"implementer": [info["agent_id"]]}})
    return lambda: w.s.detach_team(w.alice, w.room, team)


@pytest.mark.parametrize("flow", [_flow_member, _flow_agent, _flow_external, _flow_join_token, _flow_workspace,
                                  _flow_mission, _flow_team, _flow_role])
def test_every_revoke_flow_completes_on_a_log_that_refuses_ordinary_writes(w, flow):
    revoke = flow(w)
    _at_the_ordinary_limit(w.s)
    with pytest.raises(CollabError) as full:
        w.s.add_member(w.alice, w.room, {"user_id": "mallory", "role": "viewer"})
    assert full.value.status == 503
    revoke()                                                          # must not 503 halfway


def test_the_reserve_only_holds_events_an_attacker_class_cannot_fire_at_will():
    """Self-triggerable or auto-fired events (an agent rotating its credential, a member cancelling a consultation or
    request, a grant lapsing) would let the same attacker drain the reserve, so they are not safety events."""
    for name in ("credential_rotated", "host_unregistered", "consult_closed", "agent_request_closed", "steer_rejected",
                 "invite_closed", "agent_grant_expired", "xlink_unpublished", "external_status_changed"):
        assert name not in SAFETY_EVENTS, name


# ======================================================================== M2: since_join on every read
def _join_cut(fab, who, stamps_index):
    ctx = fab.repo.graph._log_ctx[("room", fab.room, None)]
    stamps = sorted(datetime.fromisoformat(str(e["ingest_timestamp"])).timestamp() for e, _ in ctx)
    fab.s._rooms[fab.room]["members"][who]["joined_at"] = datetime.fromtimestamp(
        stamps[stamps_index] - 1e-6, UTC).isoformat()


def test_since_join_strips_pre_join_ids_times_and_removal_reasons_from_node_edge_and_listing_reads(fab):
    f = fab.feed
    put(f, "task:old", "room", label="old", room_id=fab.room)
    put(f, "task:peer", "room", label="peer", room_id=fab.room)
    f.edge("spawn", "task:old", "task:peer", scope="room", nodes=False, room_id=fab.room)
    f.send("edge.remove", {"edge_type": "spawn", "from_id": "task:old", "to_id": "task:peer",
                           "reason": "PRESECRET_REMOVAL_REASON"}, "room", room_id=fab.room)
    put(f, "task:new", "room", label="after", room_id=fab.room)
    fab.s.replay_history = "since_join"
    _join_cut(fab, "bob", 4)                                           # joined just before the last event
    q = {"room_id": fab.room}
    node = fab.s.graph_read(fab.bob, ["nodes", "task:old"], q)["node"]
    assert node["first_event_id"] is None and node["last_event_id"] is None
    assert node["fields"]["label"]["event_id"] is None and node["fields"]["label"]["admitted_at"] is None
    hood = fab.s.graph_read(fab.bob, ["nodes", "task:old", "neighborhood"], {**q, "include_removed": "true"})
    with pytest.raises(CollabError) as gone:        # removed before bob joined: the edge does not exist for him
        fab.s.graph_read(fab.bob, ["edge"], {"edge_type": "spawn", "from_id": "task:old", "to_id": "task:peer",
                                             "room_id": fab.room})
    assert gone.value.status == 404
    one = {}
    listing = fab.s.graph_read(fab.bob, ["nodes"], {**q, "node_type": "task"})
    dumped = json.dumps([hood, one, listing, node])
    assert "PRESECRET_REMOVAL_REASON" not in dumped
    assert all(n["first_event_id"] is None for n in listing["nodes"] if n["id"] != "task:new")
    g = fab.repo.graph
    pre = [eid for eid, t in g._first_ts.items() if t[0] < g._first_ts["pi-4"][0] - 1e-9]
    assert len(pre) >= 4 and not any(eid in dumped for eid in pre)                   # no pre-join event id anywhere
    assert not [e for e in hood["edges"] if e["edge_type"] == "spawn"]     # no endpoints, no writer of a pre-join edge
    # the post-join node keeps its provenance, and the full-history setting changes nothing else
    fresh = next(n for n in listing["nodes"] if n["id"] == "task:new")
    assert fresh["first_event_id"] is not None
    fab.s.replay_history = "full"
    assert fab.s.graph_read(fab.bob, ["nodes", "task:old"], q)["node"]["first_event_id"] is not None


def test_since_join_hides_pre_join_admission_times_from_the_insights_snapshot(fx):
    """M2b: progress.tasks[].evidence_at came from the server admission time of an event the reader's
    membership predates."""
    f = fx
    f.s.replay_history = "since_join"
    node(f.feed, "task:t1", state="running", room_id=f.room)
    node(f.feed, PR1, state="merged", room_id=f.room, principal=OPERATOR)
    edge(f.feed, "produced", "task:t1", PR1, room_id=f.room, principal=OPERATOR)
    before = f.s.clock.t
    tick(f, 5000)
    f.s.add_member(f.alice, f.room, {"user_id": "mallory", "role": "viewer"})
    tick(f, 10)
    row = task_row(prog(f, f.mallory), "task:t1")
    assert row["stage"] == "complete"                                  # the current state is visible...
    assert row["evidence_at"] is None or ins.epoch(row["evidence_at"]) > before + 1000   # ...its pre-join time is not
    f.s.replay_history = "full"
    assert ins.epoch(task_row(prog(f, f.mallory), "task:t1")["evidence_at"]) == pytest.approx(before, abs=2)


# ======================================================================== M3 / M15: uniform switches
def _bump(path):
    t = time.time() + 5
    os.utime(path, (t, t))


def _switch_values(svc):
    """What every feature switch reads RIGHT NOW (the same accessors the request paths use)."""
    return {
        "steering": bool(svc.steer_cfg()["enabled"] and not svc.steer_cfg()["kill_switch"]),
        "contextual": bool(svc.ctx_cfg()["enabled"] and not svc.ctx_cfg()["kill_switch"]),
        "insights": bool(svc.insights_cfg()["enabled"] and not svc.insights_cfg()["kill_switch"]),
        "team_rooms": svc.team_rooms_active(),
        "team_roles": svc.team_roles_active(),
        "delegation_enabled": svc.delegation_enabled,
        "replay_history": svc.replay_history,
    }


FEATURES = {"steering": {"enabled": True}, "contextual": {"enabled": True}, "insights": {"enabled": True},
            "team_rooms": {"enabled": True}, "team_roles": {"enabled": True}, "delegation_enabled": True,
            "replay_history": "full"}
OFF = {"steering": False, "contextual": False, "insights": False, "team_rooms": False, "team_roles": False,
       "delegation_enabled": False,
       "replay_history": "since_join"}


def test_every_collab_switch_is_registered_as_hot_or_explicitly_startup_only():
    """A new control_plane.collab key must decide: hot (and then covered below) or startup-only."""
    assert set(cli.HOT_COLLAB_SWITCHES) == set(FEATURES) == set(OFF)
    assert set(cli.HOT_COLLAB_SWITCHES) | cli.STARTUP_ONLY_COLLAB_KEYS == set(cli.COLLAB_CONFIG_KEYS)
    assert cli.STARTUP_ONLY_COLLAB_KEYS.isdisjoint(cli.HOT_COLLAB_SWITCHES)


@pytest.mark.parametrize("breakage", ["delete", "garbage", "world_readable", "invalid_value"])
def test_every_feature_switch_fails_closed_identically_when_the_fleet_file_breaks(tmp_path, breakage):
    path = make_file(tmp_path, collab=dict(COLLAB, **FEATURES))
    store = FleetConfigStore(path)
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    cli._wire_collab_switches(svc, store)
    assert _switch_values(svc) == {"steering": True, "contextual": True, "insights": True, "team_rooms": True,
                                   "team_roles": True, "delegation_enabled": True, "replay_history": "full"}
    if breakage == "delete":
        path.unlink()
    elif breakage == "garbage":
        path.write_text("sections: [unclosed\n  - : :")
        path.chmod(0o600)
        _bump(path)
    elif breakage == "world_readable":
        path.chmod(0o644)
        _bump(path)
    else:
        data = yaml.safe_load(path.read_text())
        coll = data["sections"]["control_plane"]["values"]["collab"]
        for key in ("steering", "contextual", "insights", "team_rooms", "team_roles"):
            coll[key] = {"kill_switch": "yes"}
        coll["delegation_enabled"] = "yes"
        coll["replay_history"] = "everything"
        path.write_text(yaml.safe_dump(data))
        path.chmod(0o600)
        _bump(path)
    assert _switch_values(svc) == OFF, breakage


def test_a_missing_switch_key_means_off_and_the_strict_replay_default(tmp_path):
    path = make_file(tmp_path, collab=dict(COLLAB))
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    cli._wire_collab_switches(svc, FleetConfigStore(path))
    assert _switch_values(svc) == OFF


def test_delegation_and_replay_history_follow_a_file_edit_without_a_restart(tmp_path):
    path = make_file(tmp_path, collab=dict(COLLAB, delegation_enabled=True, replay_history="full"))
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    cli._wire_collab_switches(svc, FleetConfigStore(path))
    assert svc.delegation_enabled is True and svc.replay_history == "full"
    data = yaml.safe_load(path.read_text())
    coll = data["sections"]["control_plane"]["values"]["collab"]
    coll["delegation_enabled"], coll["replay_history"] = False, "since_join"
    path.write_text(yaml.safe_dump(data))
    path.chmod(0o600)
    _bump(path)
    assert svc.delegation_enabled is False and svc.replay_history == "since_join"


def test_the_insights_source_uses_the_fail_closed_reader(tmp_path):
    path = make_file(tmp_path, collab=dict(COLLAB, insights={"enabled": True}))
    store = FleetConfigStore(path)
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    cli._wire_collab_switches(svc, store)
    assert svc.insights_cfg()["enabled"] is True
    path.chmod(0o644)
    _bump(path)
    assert svc.insights_cfg()["enabled"] is False and svc.insights_cfg()["kill_switch"] is True


# ======================================================================== M4 / M12 over real HTTP
def _server(api_keys):
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, api_keys=api_keys)
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    return srv


def _http(srv, method, path, key=None, xff=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    if xff:
        headers["X-Forwarded-For"] = xff
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data or b"{}")


def test_ingest_sources_lists_only_the_callers_own_emitters_to_non_operators():
    keys = {"op-key-0123456789abcdef": "operator", "ing-a-0123456789abcdef": "ingest",
            "ing-b-0123456789abcdef": "ingest", "view-0123456789abcdefgh": "viewer"}
    srv = _server(keys)
    try:
        for key, source in (("ing-a-0123456789abcdef", "a-emitter"), ("ing-b-0123456789abcdef", "secret-emitter")):
            status, _ = _http(srv, "POST", "/api/v1/ingest", key, body=v2("host", "h1", "host.registered",
                                                                           {"host_name": "h1"}, 0, source=source))
            assert status == 200
        path = "/api/v1/ingest/sources"
        names = lambda key: sorted(s["source"] for s in _http(srv, "GET", path, key)[1]["sources"])  # noqa: E731
        assert names("op-key-0123456789abcdef") == ["a-emitter", "secret-emitter"]
        assert names("ing-a-0123456789abcdef") == ["a-emitter"]
        assert names("ing-b-0123456789abcdef") == ["secret-emitter"]
        assert names("view-0123456789abcdefgh") == []
    finally:
        srv.shutdown()


def test_the_generic_rate_limiter_keys_clients_behind_a_proxy_by_forwarded_address():
    srv = _server({"op-key-0123456789abcdef": "operator"})
    try:
        srv._limiter.max_requests = 5
        codes = [_http(srv, "GET", "/api/v1/hosts", "op-key-0123456789abcdef", xff=f"203.0.113.{i}")[0]
                 for i in range(12)]
        assert codes == [200] * 12                              # twelve clients, twelve buckets
        same = [_http(srv, "GET", "/api/v1/hosts", "op-key-0123456789abcdef", xff="198.51.100.9")[0]
                for _ in range(8)]
        assert same[:5] == [200] * 5 and same[5:] == [429] * 3     # one client is still limited
    finally:
        srv.shutdown()


# ======================================================================== M6: per-principal gap state
def test_one_ingest_key_cannot_evict_another_principals_gap_state():
    repo = ControlPlaneRepo()
    svc = IngestService(repo)
    svc.ingest(v2("host", "h", "host.registered", {}, 1, source="legit"), KEY2)
    svc.ingest(v2("host", "h", "host.registered", {}, 5, source="legit"), KEY2)          # a gap 2-4
    legit = lambda: repo.ledger.source_state("legit", KEY2["id"])  # noqa: E731
    assert legit()["gaps"] and legit()["last_sequence"] == 5
    for i in range(envelope_mod.MAX_SOURCES + 100):
        svc.ingest(v2("host", "h", "host.registered", {}, 1, source=f"a{i:05d}"[:32], event_id=f"x{i}"), KEY)
    assert legit()["gaps"] == [{"from": 2, "to": 4, "seen_at": legit()["gaps"][0]["seen_at"]}]
    assert legit()["last_sequence"] == 5
    mine = [s for s in repo.ledger.source_states(KEY["id"])]
    assert len(mine) <= envelope_mod.MAX_SOURCES_PER_ACTOR


def test_when_the_global_cap_is_hit_the_heaviest_principal_pays():
    led = envelope_mod.SourceLedger()

    def commit(actor, source, seq=1):
        led.commit({"event_id": f"{actor}-{source}", "source": source, "source_sequence": seq,
                    "actor": {"id": actor}, "content_hash": "h", "received_at": "2026-01-01T00:00:00+00:00"},
                   persist=False)
    for i in range(envelope_mod.MAX_SOURCES - 1):
        commit(f"heavy{i % 20}", f"s{i}")
    commit("light", "mine")
    commit("newcomer", "n")
    assert led.source_state("mine", "light")["last_sequence"] == 1


# ======================================================================== M7: trusted writers survive the cap
def test_spam_writers_cannot_push_a_trusted_evidence_writers_value_out_of_the_read(fab):
    f = fab.feed
    trusted = {"kind": "ingest", "id": "key:eeeeeeeeeeee", "authenticated_by": "api_key"}
    pr = "pr:github.com/o/r#1"
    f.send("node.upsert", {"node_type": "pr", "node_id": pr, "state": "open"}, "room", room_id=fab.room)
    f.send("node.state", {"node_id": pr, "state": "merged"}, "room", room_id=fab.room, principal=trusted)
    for n in range(8):
        spam = {"kind": "ingest", "id": f"key:{n:012x}", "authenticated_by": "api_key"}
        f.send("node.state", {"node_id": pr, "state": "open"}, "room", room_id=fab.room, principal=spam)
    reader = {"kind": "member", "id": "user:bob", "authenticated_by": "collab", "access": fab.s.graph_access(fab.bob)}
    graph = fab.repo.graph
    plain = graph.get_node(reader, pr)["node"]
    from herdr_engineering.control_plane import collab_insights as ci
    assert ci.trusted_value(plain, "state", frozenset({trusted["id"]})) is None          # the capped read loses it
    kept = graph.get_node({**reader, "keep_writers": frozenset({trusted["id"]})}, pr)["node"]
    assert ci.trusted_value(kept, "state", frozenset({trusted["id"]})) == "merged"
    assert len(kept["contributions"]["state"]) <= 5 + 1                                   # the cap still bounds spam


# ======================================================================== M9: quorum head redaction
def test_the_quorum_route_does_not_leak_a_head_the_viewer_cannot_read():
    world = World()
    other = {"kind": "ingest", "id": "key:other", "authenticated_by": "api_key"}
    world.feed.node(PR, label="PR 7", principal=other, scope="org")                       # a visible node shell
    world.feed.node(PR, principal=PI, scope="private", attrs={"head_sha": "d" * 40, "author": "somebody"})
    mine = world.q(cfg(), principal=PI)
    assert mine["head"] == "d" * 40 and "head_hidden" not in mine                         # the writer sees its own
    seen = world.q(cfg(), principal=other)
    assert seen["head"] is None and seen["head_hidden"] is True
    assert "d" * 40 not in json.dumps(seen)
    assert seen["status"] == mine["status"]                                                # the decision is the same


def test_a_second_visible_trusted_writer_does_not_unlock_the_hidden_winners_head():
    world = World()
    other = {"kind": "ingest", "id": "key:other", "authenticated_by": "api_key"}
    pi2 = {"kind": "agent", "id": "key:pi2", "authenticated_by": "api_key"}
    world.feed.node(PR, label="PR 7", principal=other, scope="org")
    world.feed.node(PR, principal=pi2, scope="org", attrs={"head_sha": "c" * 40})            # visible, older
    world.feed.node(PR, principal=PI, scope="private", attrs={"head_sha": "d" * 40, "author": "somebody"})   # the winner
    config = cfg(writers=("key:pi", "key:pi2"))
    assert world.q(config, principal=PI)["head"] == "d" * 40
    seen = world.q(config, principal=other)
    assert seen["head"] is None and seen["head_hidden"] is True and "d" * 40 not in json.dumps(seen)


def test_trusted_evidence_survives_spam_through_the_real_progress_wiring(fx):
    """M7 end to end: the operator marks the pull request merged, then 8 other writers write 'open'."""
    f = fx
    node(f.feed, "task:t1", state="running", room_id=f.room)
    node(f.feed, PR1, state="merged", room_id=f.room, principal=OPERATOR)
    edge(f.feed, "produced", "task:t1", PR1, room_id=f.room, principal=OPERATOR)
    assert task_row(prog(f), "task:t1")["stage"] == "complete"
    for n in range(8):
        spam = {"kind": "operator", "id": f"key:spam{n}", "authenticated_by": "api_key"}
        f.ing.key_scopes[spam["id"]] = {"rooms": ("*",), "missions": ("*",)}
        node(f.feed, PR1, state="open", room_id=f.room, principal=spam)
    assert task_row(prog(f), "task:t1")["stage"] == "complete"


def _restart_with(extra_rows):
    sink = Sink()
    collab = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    W(collab)
    good = len(sink.rows)
    rows = copy.deepcopy(sink.rows) + extra_rows
    repo = ControlPlaneRepo()
    load_into(repo, Src(rows))
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    server = ControlPlaneServer(repo, collab=again, allow_open=True)
    server.shutdown()
    return repo, again, good


def test_pre_fix_v1_forged_rows_without_a_collab_payload_are_quarantined_and_collaboration_stays_up():
    forged = {"entity_type": "collab", "entity_id": "log", "sequence": 1, "event_type": "member_added",
              "event_id": "forged", "payload": {"room_id": "r", "subject_user_id": "mallory", "role": "owner"}}
    repo, again, good = _restart_with([forged])
    assert repo.collab_quarantine_reasons == ["not_collab_schema"] and not again.degraded
    assert len(again._log) == good                                                       # the real log, nothing else


STAMP = "2026-01-01T00:00:00+00:00"


def test_pre_fix_forged_rows_that_copy_the_payload_shape_are_quarantined_by_sequence():
    """A forger who copies org_id/correlation_id/created_at still collides with a real sequence: the first row wins."""
    mimic = {"entity_type": "collab", "entity_id": "log", "sequence": 1, "event_type": "member_added",
             "event_id": "forged2", "source_timestamp": STAMP, "ingest_timestamp": STAMP,
             "payload": {"org_id": "default", "correlation_id": "c", "created_at": STAMP,
                                                "room_id": "r", "subject_user_id": "mallory", "role": "owner"}}
    repo, again, good = _restart_with([mimic])
    assert repo.collab_quarantine_reasons == ["duplicate_sequence"] and not again.degraded
    assert len(again._log) == good


@pytest.mark.parametrize("payload", [{}, {"room_id": 5}, None, "text", [1]])
def test_malformed_collab_payloads_never_crash_startup(payload):
    """The audit's a8 probe: whatever shape a stored row has, the control plane starts."""
    n = 99
    row = {"entity_type": "collab", "entity_id": "log", "sequence": n, "event_type": "member_added",
           "event_id": "zz", "payload": payload}
    repo, again, good = _restart_with([row])
    assert len(again._log) == good and repo.collab_quarantine


def test_a_replay_error_of_any_type_degrades_collaboration_instead_of_crashing():
    """KeyError from a well-formed-looking row; also the callable form collab-only uses for sink.load."""
    sink = Sink()
    CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    bad = {"entity_type": "collab", "entity_id": "log", "sequence": 1, "event_type": "member_added", "event_id": "e",
           "payload": {"org_id": "default", "correlation_id": "c", "created_at": "t"}}      # no room_id: KeyError
    for rows in ([bad], lambda: [bad], lambda: (_ for _ in ()).throw(ValueError("torn json"))):
        c = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
        assert c.hydrate_or_degrade(rows) is False and c.degraded
        with pytest.raises(CollabError) as exc:
            c.authenticate("Bearer " + USERS["alice"], None)
        assert exc.value.status == 503 and "KeyError" not in exc.value.message and "integrity" not in exc.value.message


def test_degraded_collaboration_is_visible_on_health_without_internal_detail():
    sink = Sink()
    c = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    c.hydrate_or_degrade([{"entity_type": "collab", "entity_id": "log", "sequence": 5, "event_type": "x", "payload": {}}])
    srv = ControlPlaneServer(ControlPlaneRepo(sink=sink), collab=c, allow_open=True)
    try:
        health = srv._health()
    finally:
        srv.shutdown()
    assert health["collab_degraded"] is True and "contiguous" not in json.dumps(health)


def test_the_cli_uses_the_guarded_replay_in_both_modes():
    import inspect
    src = inspect.getsource(cli._cmd_cp_web)
    assert "hydrate_or_degrade(sink.load)" in src                      # collab-only
    assert "collab.hydrate(" not in src
    from herdr_engineering.control_plane import api
    assert "hydrate_or_degrade(repo.take_collab_replay())" in inspect.getsource(api.ControlPlaneServer.__init__)


# ---- the operator repair command
def _file_log(tmp_path, extra=()):
    from herdr_engineering.control_plane.collab import FileEventLog
    path = tmp_path / "collab.jsonl"
    log = FileEventLog(str(path))
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=log)
    W(svc)
    log.close()           # the server that wrote it has stopped (a live one is covered by its own tests)
    with open(path, "ab") as fh:
        for row in extra:
            fh.write((json.dumps(row) + "\n").encode())
    return path


FOREIGN = [{"entity_type": "collab", "entity_id": "log", "sequence": 1, "event_type": "member_added", "event_id": "f1",
            "payload": {"room_id": "r"}},
           {"entity_type": "collab", "entity_id": "elsewhere", "sequence": 2, "event_type": "x", "event_id": "f2",
            "payload": {}}]


def test_collab_repair_is_a_dry_run_by_default_and_changes_nothing(tmp_path):
    from herdr_engineering.control_plane.collab_repair import repair_file
    path = _file_log(tmp_path, FOREIGN)
    before = path.read_bytes()
    out = repair_file(str(path))
    assert out["mode"] == "dry-run" and [q["reason"] for q in out["quarantine"]] == ["not_collab_schema", "foreign_stream"]
    assert path.read_bytes() == before and not (tmp_path / "collab.jsonl.quarantine.jsonl").exists()


def test_collab_repair_apply_needs_confirmation_then_moves_rows_and_never_deletes(tmp_path):
    from herdr_engineering.control_plane.collab_repair import repair_file
    path = _file_log(tmp_path, FOREIGN)
    before = path.read_bytes()
    with pytest.raises(PermissionError):
        repair_file(str(path), apply=True)
    assert path.read_bytes() == before
    out = repair_file(str(path), apply=True, confirmed=True)
    moved = [json.loads(line) for line in open(out["quarantine_file"])]
    assert [m["row"]["event_id"] for m in moved] == ["f1", "f2"] and {m["quarantine_reason"] for m in moved}
    assert open(out["backup"], "rb").read() == before                   # the original is kept whole
    kept = [json.loads(line) for line in path.read_bytes().split(b"\n") if line.strip()]
    assert all(r["event_id"] not in ("f1", "f2") for r in kept)
    c = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    assert c.hydrate_or_degrade(kept) and not c.degraded                # and the repaired log replays
    assert os.stat(out["quarantine_file"]).st_mode & 0o077 == 0


def test_collab_repair_gap_tail_is_only_set_aside_on_explicit_request(tmp_path):
    from herdr_engineering.control_plane.collab_repair import classify
    rows = [{"entity_type": "collab", "entity_id": "log", "sequence": n, "event_type": "x", "event_id": f"e{n}",
             "source_timestamp": STAMP, "ingest_timestamp": STAMP,
             "payload": {"org_id": "d", "correlation_id": "c", "created_at": STAMP}} for n in (1, 2, 4, 5)]
    keep, bad = classify(rows)
    assert len(keep) == 4 and not bad                                    # a gap is the service's call (it degrades)
    keep, bad = classify(rows, cut_tail=True)
    assert [r["sequence"] for r in keep] == [1, 2] and [why for _, why in bad] == ["after_gap"] * 2


def test_collab_repair_postgres_plan_and_apply_use_one_transaction_and_a_quarantine_table():
    import asyncio

    from herdr_engineering.control_plane.collab_repair import repair_postgres

    class Conn:
        def __init__(self):
            self.sql, self.in_tx = [], False

        async def fetch(self, q, *a):
            self.sql.append(q)
            ok = {"org_id": "d", "correlation_id": "c", "created_at": STAMP}
            return [{"tid": "(0,1)", "event_id": "g1", "entity_id": "log", "sequence": 1, "event_type": "x",
                     "payload": json.dumps(ok), "source": None, "source_sequence": None, "actor": None,
                     "source_timestamp": STAMP, "ingest_timestamp": STAMP},
                    {"tid": "(0,2)", "event_id": "f1", "entity_id": "log", "sequence": 1, "event_type": "x",
                     "payload": "{}", "source": "pi", "source_sequence": 0, "actor": "{}"}]

        async def execute(self, q, *a):
            assert self.in_tx
            self.sql.append(q)

        def transaction(self):
            conn = self

            class Tx:
                async def __aenter__(self):
                    conn.in_tx = True

                async def __aexit__(self, *exc):
                    conn.in_tx = False
            return Tx()
    locked = Conn()
    asyncio.run(repair_postgres(locked, apply=True, confirmed=True))
    assert locked.sql[0].startswith("LOCK TABLE fabric_events") and "SHARE ROW EXCLUSIVE" in locked.sql[0]   # first, in the tx
    dry = Conn()
    out = asyncio.run(repair_postgres(dry))
    assert out["mode"] == "dry-run" and out["quarantine"][0]["reason"] == "ingest_envelope" and len(dry.sql) == 1
    with pytest.raises(PermissionError):
        asyncio.run(repair_postgres(Conn(), apply=True))
    live = Conn()
    asyncio.run(repair_postgres(live, apply=True, confirmed=True))
    text = " ".join(live.sql)
    assert "fabric_events_quarantine" in text and "DELETE FROM fabric_events" in text and "RETURNING *" in text
    assert "DROP" not in text and "TRUNCATE" not in text


# ======================================================================== H2 round 2
def test_one_enrolled_external_agent_cannot_drain_the_reserve_by_toggling_presence(w, svc):
    from tests.control_plane.test_collab_external import enroll
    ag, _, _ = enroll(w)
    s = w.s
    s.max_log_events = 600
    s._log.extend([{}] * (s._log_limit("room_created") - len(s._log)))
    start = len(s._log)
    states = ["idle", "working"]
    for i in range(300):
        if i % 10 == 9:
            s.clock.t += 601
        if i % 10 == 0:
            s.clock.t += 6
        try:
            s.external_heartbeat(ag, {"status": states[i % 2], "activity": "x"})
        except CollabError as exc:
            assert exc.status != 503, "a heartbeat must never fail because the log is full"
            s.clock.t += 61
    assert len(s._log) == start                         # presence changes above 90 % are kept in memory only
    s.remove_member(w.alice, w.room, "bob")


def test_presence_flips_are_logged_while_the_log_has_room(w, svc):
    from tests.control_plane.test_collab_external import enroll
    ag, _, _ = enroll(w)
    before = len(w.s._log)
    w.s.external_heartbeat(ag, {"status": "working", "activity": "x"})
    assert len(w.s._log) == before + 1 and w.s._log[-1]["event_type"] == "external_status_changed"


def test_create_then_revoke_cycles_cannot_drain_the_reserve(w, svc):
    """The audit's a3 probe: a user links missions (ordinary) up to the limit, then unlinks them (safety)."""
    s = w.s
    s.max_log_events = 400
    base = s._outstanding()
    m = w.bob
    pid = s.create_project(m, {"name": "bobs"})["project_id"]
    rooms = [s.create_room(m, pid, {"name": f"r{i}"})["room_id"] for i in range(10)]
    linked, i = [], 0
    try:
        while True:
            r = rooms[i % 10]
            s.link_mission(m, r, {"mission_id": f"m{i}", "host": "bender"})
            linked.append((r, f"m{i}"))
            i += 1
            if i % 40 == 0:
                s.clock.t += 3700
    except CollabError as exc:
        assert exc.status == 503
    assert linked
    for k, (r, mid) in enumerate(linked):
        s.unlink_mission(m, r, mid)
        if k % 40 == 0:
            s.clock.t += 3700
    assert len(s._log) <= s.max_log_events - s.max_log_events // 20          # the reserve was not touched
    assert s._outstanding() == base
    s.remove_member(w.alice, w.room, "bob")                                   # the owner's revoke still succeeds


def test_a_full_set_of_outstanding_creations_still_leaves_room_to_revoke_each_of_them(w, svc):
    s = w.s
    s.max_log_events = 300
    base = s._outstanding()
    n = 0
    try:
        while True:
            s.link_mission(w.alice, w.room, {"mission_id": f"mm{n}", "host": "bender"})
            n += 1
            if n % 40 == 0:
                s.clock.t += 3700
    except CollabError:
        pass
    assert n > 0 and len(s._log) + s._outstanding() <= s.max_log_events
    for k in range(n):
        s.unlink_mission(w.alice, w.room, f"mm{k}")
        if k % 40 == 0:
            s.clock.t += 3700
    assert s._outstanding() == base


def test_revocation_accounting_survives_a_restart():
    sink = Sink()
    c = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=sink)
    cast = W(c)
    mid = c._recount()["member"]
    c.add_member(cast.alice, cast.room, {"user_id": "mallory", "role": "viewer"})
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}))
    again.hydrate(copy.deepcopy(sink.rows))
    assert again._recount()["member"] == c._recount()["member"] == mid + 1


# ======================================================================== M4 round 2
def test_ingest_sources_scope_all_is_operator_only_and_scope_own_narrows_operators():
    keys = {"op-key-0123456789abcdef": "operator", "ing-a-0123456789abcdef": "ingest",
            "view-0123456789abcdefgh": "viewer"}
    srv = _server(keys)
    try:
        _http(srv, "POST", "/api/v1/ingest", "ing-a-0123456789abcdef",
              body=v2("host", "h1", "host.registered", {"host_name": "h1"}, 0, source="a-emitter"))
        path = "/api/v1/ingest/sources"
        status, body = _http(srv, "GET", path + "?scope=all", "op-key-0123456789abcdef")
        assert status == 200 and [s["source"] for s in body["sources"]] == ["a-emitter"]
        assert _http(srv, "GET", path + "?scope=all", "view-0123456789abcdefgh")[0] == 403
        assert _http(srv, "GET", path + "?scope=all", "ing-a-0123456789abcdef")[0] == 403
        assert _http(srv, "GET", path + "?scope=own", "op-key-0123456789abcdef")[1]["sources"] == []
        assert _http(srv, "GET", path + "?scope=bogus", "op-key-0123456789abcdef")[0] == 400
    finally:
        srv.shutdown()


def test_the_ledger_stays_inside_its_global_cap_with_one_domain_per_key():
    led = envelope_mod.SourceLedger()
    for i in range(envelope_mod.MAX_SOURCES + 200):
        led.commit({"event_id": f"e{i}", "source": "s", "source_sequence": 1, "actor": {"id": f"key:{i}"},
                    "content_hash": "h", "received_at": "2026-01-01T00:00:00+00:00"}, persist=False)
    assert len(led.source_states()) <= envelope_mod.MAX_SOURCES


# ======================================================================== M2 round 2
def _join_at(fab, room, who, when):
    fab.s._rooms[room]["members"][who]["joined_at"] = datetime.fromtimestamp(when, UTC).isoformat()


def _two_room_world(fab):
    """bob is in fab.room (joined EARLY) and fab.room2 (joined LATE); task:secret2 and its removed edge belong to room2."""
    f = fab.feed
    put(f, "task:secret2", "room", label="r2-old", room_id=fab.room2)
    put(f, "task:peer2", "room", label="peer2", room_id=fab.room2)
    f.edge("spawn", "task:secret2", "task:peer2", scope="room", nodes=False, room_id=fab.room2)
    f.send("edge.remove", {"edge_type": "spawn", "from_id": "task:secret2", "to_id": "task:peer2",
                           "reason": "R2PRESECRET"}, "room", room_id=fab.room2)
    put(f, "task:post2", "room", label="r2-new", room_id=fab.room2)
    fab.s.add_member(fab.alice, fab.room2, {"user_id": "bob", "role": "contributor"})
    ts = sorted(t for t, _ in fab.repo.graph._first_ts.values())
    fab.s.replay_history = "since_join"
    _join_at(fab, fab.room, "bob", ts[0] - 1)
    _join_at(fab, fab.room2, "bob", ts[-1] - 1e-6)
    return ts


@pytest.mark.parametrize("named", ["room2", "room", None])
def test_the_caller_named_room_cannot_move_the_since_join_horizon(fab, named):
    """The audit's room probe: bob joined room2 late but names the room he joined early."""
    _two_room_world(fab)
    q = {} if named is None else {"room_id": fab.room2 if named == "room2" else fab.room}
    node = fab.s.graph_read(fab.bob, ["nodes", "task:secret2"], q)["node"]
    hood = fab.s.graph_read(fab.bob, ["nodes", "task:secret2", "neighborhood"], {**q, "include_removed": "true"})
    assert node["first_event_id"] is None and node["fields"]["label"]["admitted_at"] is None
    assert "R2PRESECRET" not in json.dumps([node, hood])
    assert not [e for e in hood["edges"] if e["edge_type"] == "spawn"]


def test_an_event_without_a_room_uses_the_latest_join_of_the_readers_rooms(fab):
    g = fab.repo.graph
    g._first_ts["org-event"] = (100.0, None)
    g._first_ts["room-event"] = (100.0, fab.room)
    out = lambda joins: g.strip_before([{"event_id": "org-event"}, {"event_id": "room-event"}], joins)  # noqa: E731
    assert out({fab.room: 50.0, fab.room2: 90.0}) == [{"event_id": "org-event"}, {"event_id": "room-event"}]
    got = out({fab.room: 50.0, fab.room2: 150.0})                       # latest join (150) is after the org event
    assert got[0]["event_id"] is None and got[1]["event_id"] == "room-event"
    assert out({fab.room: 150.0, fab.room2: 40.0})[1]["event_id"] is None      # its own room's join decides


def test_a_missing_join_time_hides_history_before_now_and_a_foreign_room_falls_back_to_the_latest_join(fab):
    f = fab.feed
    put(f, "task:old", "room", label="old", room_id=fab.room)
    fab.s.replay_history = "since_join"
    fab.s._rooms[fab.room]["members"]["bob"].pop("joined_at")
    node = fab.s.graph_read(fab.bob, ["nodes", "task:old"], {"room_id": fab.room})["node"]
    assert node["first_event_id"] is None and node["fields"]["label"]["admitted_at"] is None
    g = fab.repo.graph
    g._first_ts["x"] = (500.0, "room-the-reader-never-joined")        # a shared mission written from another room
    assert g.strip_before({"event_id": "x"}, {fab.room: 1.0}) == {"event_id": "x"}      # after the reader's join: shown
    assert g.strip_before({"event_id": "x"}, {fab.room: 900.0}) == {"event_id": None}   # before it: still stripped


def test_strip_before_treats_an_unknown_event_id_as_pre_join_and_none_as_unrestricted(fab):
    g = fab.repo.graph
    assert g.strip_before({"event_id": "never-seen", "admitted_at": "t"}, {fab.room: 0.0}) == {
        "event_id": None, "admitted_at": None}
    keep = {"event_id": "never-seen"}
    assert g.strip_before(keep, None) == {"event_id": "never-seen"}
    assert g.strip_before({"event_id": "never-seen"}, {}) == {"event_id": None}      # no rooms at all: fail closed


def test_a_hostile_writer_reusing_a_victims_event_id_cannot_keep_it_visible(fab):
    """L2: the id is shared by an old (victim) event and a new (hostile) one; the earlier admission decides."""
    f = fab.feed
    put(f, "task:old", "room", label="old", room_id=fab.room)               # event id pi-0
    fab.s.replay_history = "full"
    ts_old = fab.repo.graph._first_ts["pi-0"][0]
    hostile = {"kind": "ingest", "id": "key:hostile", "authenticated_by": "api_key"}
    f.send("node.upsert", {"node_type": "task", "node_id": "task:new", "label": "n"}, "room", room_id=fab.room,
           principal=hostile, seq=0)                                          # event_id "pi-0" again, other actor
    ts_new = fab.repo.graph._first_ts["pi-0"][0]
    assert ts_new == ts_old                                                   # the earliest admission is kept
    out = fab.repo.graph.strip_before({"event_id": "pi-0"}, {fab.room: ts_old + 1e-3})
    assert out["event_id"] is None


def test_get_edge_of_an_edge_removed_before_the_join_is_not_found(fab):
    f = fab.feed
    put(f, "task:a", "room", label="a", room_id=fab.room)
    put(f, "task:b", "room", label="b", room_id=fab.room)
    f.edge("spawn", "task:a", "task:b", scope="room", nodes=False, room_id=fab.room)
    f.send("edge.remove", {"edge_type": "spawn", "from_id": "task:a", "to_id": "task:b", "reason": "gone"}, "room", room_id=fab.room)
    put(f, "task:c", "room", label="c", room_id=fab.room)
    fab.s.replay_history = "since_join"
    _join_cut(fab, "bob", 4)
    with pytest.raises(CollabError) as exc:
        fab.s.graph_read(fab.bob, ["edge"], {"edge_type": "spawn", "from_id": "task:a", "to_id": "task:b",
                                             "room_id": fab.room})
    assert exc.value.status == 404
    fab.s.replay_history = "full"
    assert fab.s.graph_read(fab.bob, ["edge"], {"edge_type": "spawn", "from_id": "task:a", "to_id": "task:b",
                                                "room_id": fab.room})["edges"]


# ======================================================================== M15 round 2
def test_an_explicit_replay_history_flag_wins_over_the_file(tmp_path):
    path = make_file(tmp_path, collab=dict(COLLAB, replay_history="full"))
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    svc.replay_history = "since_join"                                       # what the flag set at startup
    cli._wire_collab_switches(svc, FleetConfigStore(path), pinned=frozenset({"replay_history"}))
    assert svc.replay_history == "since_join" and svc.replay_history_source is None
    other = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    cli._wire_collab_switches(other, FleetConfigStore(path))
    assert other.replay_history == "full"


def test_an_invalid_switch_block_at_startup_is_logged_loudly_and_the_switch_is_off(tmp_path, caplog):
    path = make_file(tmp_path, collab=dict(COLLAB, steering={"kill_switch": "yes"}, delegation_enabled="yes"))
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    with caplog.at_level(logging.ERROR):
        cli._wire_collab_switches(svc, FleetConfigStore(path))
    text = " ".join(r.message for r in caplog.records)
    assert "control_plane.collab.steering" in text and "control_plane.collab.delegation_enabled" in text
    assert _switch_values(svc)["steering"] is False and svc.delegation_enabled is False


def test_collab_only_keeps_the_store_for_hot_switches_and_the_restart_list_is_documented():
    import inspect
    src = inspect.getsource(cli._cmd_cp_web)
    assert "switch_store, fleet_store = fleet_store, None" in src and "_wire_collab_switches(collab, switch_store" in src
    assert {"review_quorum", "lineage_key_scopes", "api_keys"} <= set(cli.RESTART_REQUIRED_KEYS)
    assert set(cli.RESTART_REQUIRED_KEYS).isdisjoint({"collab"})


# ======================================================================== M7 round 2: the real readers
def test_the_insights_reader_names_the_evidence_writers_to_the_graph(fx):
    f = fx
    seen = []
    graph = f.repo.graph
    real = graph.room_graph

    def spy(reader, *a, **k):
        seen.append(reader.get("keep_writers"))
        return real(reader, *a, **k)
    graph.room_graph = spy
    prog(f)
    assert seen and "key:op" in seen[0] and f"collab:room:{f.room}" in seen[0]


def test_the_fork_evidence_reader_names_the_evidence_writers_to_the_graph(monkeypatch):
    from herdr_engineering.control_plane import forks
    readers = []
    monkeypatch.setattr(forks, "_get", lambda graph, reader, nid, kind: readers.append(reader) or None)
    forks._evidence(object(), {"id": "user:bob"}, "approach:x", "a" * 40, None, {"evidence_writers": ["key:w1"]})
    assert readers and all(r["keep_writers"] == frozenset({"key:w1"}) for r in readers)


# ======================================================================== L1 round 2
def test_an_open_listener_may_not_write_inside_a_service_namespace():
    svc = IngestService(ControlPlaneRepo())
    svc.service_node = lambda node_id: node_id.startswith("task:hcn-")
    open_listener = {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
    body = v2("lineage", "task:hcn-1", "node.upsert", {"node_type": "task", "node_id": "task:hcn-1"}, 0)
    body["visibility_scope"] = "org"
    with pytest.raises(PermissionError):
        svc.ingest(body, open_listener)


# ======================================================================== L1
def test_an_open_listener_may_not_write_inside_a_reserved_namespace():
    svc = IngestService(ControlPlaneRepo())
    svc.reserved_node = lambda node_id: node_id.startswith("agent:ext-")
    open_listener = {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
    body = v2("lineage", "agent:ext-1", "node.upsert", {"node_type": "agent", "node_id": "agent:ext-1"}, 0)
    body["visibility_scope"] = "org"
    with pytest.raises(PermissionError):
        svc.ingest(body, open_listener)
    keyed = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
    assert svc.ingest({**body, "event_id": "pi-1", "source_sequence": 1}, keyed)["event_type"] == "node.upsert"
