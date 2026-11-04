# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""D03: rooms annotate stable graph entities; every access rides on the room's own permission checks."""
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import REPO_ID  # noqa: E402

from herdr_engineering.architecture import analyze  # noqa: E402
from herdr_engineering.architecture.service import MAX_LINKS_PER_ROOM, ArchitectureService  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402
from tests.control_plane.test_collab import BASE, Api, invite, make_room  # noqa: E402
from tests.control_plane.test_collab_public import H, stack  # noqa: E402,F401
from tests.control_plane.test_collab_tasks import W, code, svc  # noqa: E402,F401


MISSION = "m-arch"
OTHER_REPO = "github.com/example/elsewhere"


def scope_room(svc, owner, room, crepo, mission=MISSION, repository=REPO_ID):
    """A room annotates the repositories of its linked missions' sessions."""
    crepo.upsert_session(f"pi:{mission}", host_id="h1", source="pi", repository_id=repository, mission_id=mission)
    svc.link_mission(owner, room, {"mission_id": mission})


@pytest.fixture
def w(svc, repo):
    crepo = ControlPlaneRepo()
    arch = ArchitectureService(crepo, enabled=True)
    snap = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
    arch.publish(snap)
    svc.architecture = arch
    world = W(svc)
    world.arch, world.sid, world.crepo = arch, snap["snapshot"]["snapshot_id"], crepo
    scope_room(svc, world.alice, world.room, crepo)
    return world


def link(w, who, **kw):
    body = {"snapshot_id": w.sid, "node_id": "function:app.core.run", "note": "why is this here?", **kw}
    return w.s.link_architecture(who, w.room, body)


def test_contributor_and_owner_can_annotate_viewer_guest_outsider_cannot(w):
    assert link(w, w.alice)["replayed"] is False and link(w, w.bob)["link_id"]
    assert code(link, w, w.carol) == 403            # viewer: read only
    assert code(link, w, w.dave) == 403             # guest: read only
    assert code(link, w, w.mallory) == 404          # not a member: identical to an unknown room
    assert len(w.arch.room_links(w.room)["links"]) == 2


def test_members_see_the_entity_guests_see_only_the_note(w):
    link(w, w.alice, note="needs a test")
    for who in (w.alice, w.carol):
        row = w.s.architecture_links(who, w.room)["links"][0]
        assert row["node_id"] == "function:app.core.run" and row["relative_path"] == "app/core.py" and row["line_start"] == 9
        assert row["available"] is True and row["snapshot_id"] == w.sid
    g = w.s.architecture_links(w.dave, w.room)
    assert g["redacted"] is True
    assert set(g["links"][0]) == {"link_id", "note", "created_at"}          # no symbol, path, snapshot, author
    assert code(w.s.architecture_links, w.mallory, w.room) == 404


def test_links_are_room_scoped(w):
    link(w, w.alice)
    assert w.s.architecture_links(w.alice, w.room2)["links"] == []
    assert code(w.s.link_architecture, w.alice, w.room2, {"snapshot_id": w.sid, "node_id": "nope"}) == 404
    assert w.arch.node_rooms(w.sid, "function:app.core.run", reveal=True) == {"rooms": [w.room], "count": 1,
                                                                             "redacted": False}
    # anyone but an operator learns only how many rooms discuss it, never which
    assert w.arch.node_rooms(w.sid, "function:app.core.run") == {"rooms": [], "count": 1, "redacted": True}


def test_a_room_links_only_its_own_repositories_and_unknown_looks_the_same(w):
    """No cross-fleet existence oracle: a real snapshot of another repository is the same 404 as a missing one."""
    body = {"snapshot_id": w.sid, "node_id": "function:app.core.run"}
    assert code(w.s.link_architecture, w.alice, w.room2, body) == 404          # room2 has no mission: no repository
    missing = code(w.s.link_architecture, w.alice, w.room2, {**body, "snapshot_id": "arch_missing"})
    assert missing == 404
    scope_room(w.s, w.alice, w.room2, w.crepo, mission="m-other", repository=OTHER_REPO)
    assert code(w.s.link_architecture, w.alice, w.room2, body) == 404          # its repository is a different one
    scope_room(w.s, w.alice, w.room2, w.crepo, mission="m-same")
    assert w.s.link_architecture(w.alice, w.room2, body)["link_id"]


def test_artifact_reads_happen_outside_the_collaboration_lock(w):
    """Resolving a link may read a large graph artifact; other rooms must not wait for it."""
    w.arch._graphs.clear()                                   # force the artifact read
    entered, release = threading.Event(), threading.Event()
    real = w.crepo.get_artifact_data

    def slow(aid):
        entered.set()
        assert release.wait(10)
        return real(aid)
    w.crepo.get_artifact_data = slow
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", link(w, w.alice)))
    t.start()
    try:
        assert entered.wait(10)
        done = threading.Event()
        threading.Thread(target=lambda: (w.s.architecture_links(w.alice, w.room2), done.set())).start()
        assert done.wait(5), "another collaboration call blocked behind the artifact read"
    finally:
        release.set()
        t.join(10)
    assert out["r"]["link_id"]


def test_membership_is_rechecked_after_the_unlocked_resolve(w):
    """A member removed while the target was being resolved does not get the link recorded."""
    real = w.arch.prepare_room_link

    def revoke_then(body, repos):
        got = real(body, repos)
        w.s._rooms[w.room]["members"].pop(w.bob.id, None)
        return got
    w.arch.prepare_room_link = revoke_then
    assert code(link, w, w.bob) == 404
    assert w.arch.room_links(w.room)["links"] == []


def test_validation_idempotency_and_forged_identity(w):
    a = link(w, w.alice, idempotency_key="k1")
    assert link(w, w.alice, idempotency_key="k1") == {"link_id": a["link_id"], "replayed": True}
    assert code(link, w, w.alice, idempotency_key="k1", note="different") == 409
    assert len(w.arch.room_links(w.room)["links"]) == 1
    assert code(link, w, w.alice, author="bob") == 400                       # identity comes from the credential
    assert code(link, w, w.alice, node_id="ghost") == 404
    assert code(link, w, w.alice, snapshot_id="arch_missing") == 404
    assert code(link, w, w.alice, note="x" * 501) == 400
    assert w.arch.room_links(w.room)["links"][0]["author"] == "alice"
    link(w, w.alice, note="a\x00b\x07c")
    assert [r["note"] for r in w.arch.room_links(w.room)["links"]][-1] == "abc"


def test_per_room_cap(w):
    for i in range(MAX_LINKS_PER_ROOM):
        link(w, w.alice, idempotency_key=f"k{i}")
    assert code(link, w, w.alice, idempotency_key="over") == 400


def test_feature_flag_off_hides_the_surface(w):
    w.arch.enabled = False
    assert code(link, w, w.alice) == 404 and code(w.s.architecture_links, w.alice, w.room) == 404


def test_annotation_survives_restart_and_events_carry_no_graph_data(w):
    link(w, w.alice, note="keep")
    fresh = ArchitectureService(w.crepo, enabled=True)
    assert [r["note"] for r in fresh.room_links(w.room)["links"]] == ["keep"]
    ev = w.crepo.stream("architecture_link", w.room)[0]
    assert ev["event_type"] == "architecture.link.created" and "def run" not in json.dumps(ev) and "nodes" not in ev["payload"]


def test_agent_needs_a_post_grant(w):
    ro, _ = w.agent("alice", ("read",))
    rw, _ = w.agent("alice", ("read", "post"))
    assert code(link, w, ro) == 403
    assert link(w, rw)["link_id"]


def test_http_routes_enforce_room_roles_end_to_end(stack, repo):
    """Through the real dispatcher: owner links, viewer cannot, outsider sees 404, guest-style read is redacted."""
    arch = stack.srv.architecture
    arch.enabled = True
    snap = analyze.analyze_commit(str(repo), "HEAD", "h1", REPO_ID)
    arch.publish(snap)
    sid = snap["snapshot"]["snapshot_id"]
    class Https(Api):          # the TLS-terminating proxy's header, on every call
        def call(self, *a, headers=None, **k):
            return super().call(*a, headers={**H, **(headers or {})}, **k)
    pub = Https(stack.pub.port)
    _, room = make_room(pub)
    assert invite(pub, room, "carol", "viewer")[0] in (200, 201)
    stack.srv.repo.upsert_session("pi:m-arch", host_id="h1", source="pi", repository_id=REPO_ID, mission_id=MISSION)
    assert pub.call("POST", f"{BASE}/rooms/{room}/missions", {"mission_id": MISSION}, user="alice")[0] in (200, 201)
    path = f"{BASE}/rooms/{room}/architecture-links"
    body = {"snapshot_id": sid, "node_id": "function:app.core.run", "note": "hello"}
    assert pub.call("POST", path, body, user="alice", headers=H)[0] == 201
    assert pub.call("POST", path, body, user="carol", headers=H)[0] == 403
    assert pub.call("POST", path, body, user="mallory", headers=H)[0] == 404
    assert pub.call("POST", path, body, headers=H)[0] in (401, 403)
    st, got = pub.call("GET", path, user="carol", headers=H)
    assert st == 200 and got["links"][0]["qualified_name"] == "app.core.run"
    assert pub.call("GET", path, user="mallory", headers=H)[0] == 404
    arch.enabled = False
    assert pub.call("GET", path, user="alice", headers=H)[0] == 404


def test_public_listener_exposes_no_architecture_api(stack):
    # the internet-facing listener serves collaboration surfaces only
    assert stack.pub.call("GET", "/api/v1/architecture/snapshots", headers=H)[0] == 404
    assert stack.pub.call("POST", "/api/v1/architecture/snapshots", {}, headers=H)[0] in (404, 405)
    assert stack.pub.call("POST", "/api/v1/architecture/analyses/request", {}, headers=H)[0] in (404, 405)
