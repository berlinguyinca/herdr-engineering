# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fabric trust slice 5: what the dashboard reads. Events and audit rows of a fabric caller carry ``via`` and ``machine``; agent
listings carry the fabric bit, expiry and machine; a coordinator's grant shows its team and exact role list. Real service, real
HTTP server, real unix-socket whois fixture. No mocks."""
from __future__ import annotations

from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_fabric_auth import (  # noqa: F401
    BENDER,
    auth,
    fabric_agent,
    fw,
    http_world,
    req,
)
from tests.control_plane.test_collab_fabric_coordinate import (  # noqa: F401
    ALL_ROLES,
    TEAM,
    coordinator,
    cw,
)
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import code, svc  # noqa: F401


def test_http_events_read_route_returns_via_and_machine_for_a_fabric_message(http_world):
    w = http_world
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    s, _ = req(w, "POST", f"{BASE}/rooms/{w.room}/messages", headers=auth(info), body={"text": "from the fabric", "idempotency_key": "d1"})
    assert s in (200, 201)
    cred = w.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    w.s.post_message(cred, w.room, {"text": "from a secret", "idempotency_key": "d2"})
    s, b = req(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=auth(info))
    assert s == 200
    msgs = {e["text"]: e for e in b["events"] if e["event_type"] == "message"}
    assert (msgs["from the fabric"]["via"], msgs["from the fabric"]["machine"]) == ("fabric", "bender")
    assert "via" not in msgs["from a secret"] and "machine" not in msgs["from a secret"]


def test_audit_rows_carry_via_and_machine_only_for_fabric_actors(fw):
    info = fabric_agent(fw)
    pr = fw.s.authenticate(f"Fabric {info['agent_id']}", None, peer_ip=BENDER)
    fw.s.post_message(pr, fw.room, {"text": "x", "idempotency_key": "a1"})
    fw.s.post_message(fw.alice, fw.room, {"text": "y", "idempotency_key": "a2"})
    proj = fw.s._rooms[fw.room]["project_id"]
    rows = [r for r in fw.s.audit(fw.alice, proj, 0, 500)["events"] if r["action"] == "message.post"]
    assert [(r.get("via"), r.get("machine")) for r in rows][-2:] == [("fabric", "bender"), (None, None)]


def test_listings_show_machine_bit_expiry_and_a_coordinators_team_and_roles(cw):
    w = cw
    info = coordinator(w, roles=["reviewer", "implementer"])
    mine = {a["agent_id"]: a for a in w.s.list_my_agents(w.alice)["agents"]}[info["agent_id"]]
    assert mine["machine"] == "bender" and mine["fabric"] is True and mine["fabric_expires_at"]
    assert mine["rooms"][0]["coordinator"] == {"team_id": TEAM, "roles": ["implementer", "reviewer"]}
    room_row = {a["agent_id"]: a for a in w.s.list_room_agents(w.alice, w.room)["agents"]}[info["agent_id"]]
    assert room_row["coordinator"] == {"team_id": TEAM, "roles": ["implementer", "reviewer"]}
    plain = [a for a in w.s.list_room_agents(w.alice, w.room)["agents"] if a["agent_id"] != info["agent_id"]]
    assert plain and all("coordinator" not in a for a in plain)
    node = [n for n in w.s.room_flow(w.alice, w.room)["nodes"] if n.get("agent_id") == info["agent_id"]][0]
    assert node["coordinator"]["roles"] == ["implementer", "reviewer"]
