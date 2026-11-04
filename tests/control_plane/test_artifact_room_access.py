"""Room-derived read access: membership drives plain read grants, nothing more."""
from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_room_access import RoomArtifactAccess, principal_for


class Store:
    def __init__(self):
        self.rooms = {"r1": ["a1", "a2"], "r2": ["a2"]}

    def call(self, method, *args):
        if method == "room_artifact_ids":
            return list(self.rooms.get(args[0], []))
        if method == "list_links":
            return [{"room_id": r} for r, arts in self.rooms.items() if args[0] in arts]
        raise AssertionError(method)


def make(readers, now=100.0):
    store, acl = Store(), ArtifactAcl(now=lambda: now)
    return store, acl, RoomArtifactAccess(acl, store.call, lambda room: readers.get(room, {}), now=lambda: now)


def test_members_get_read_and_only_read_on_the_rooms_artifacts():
    store, acl, ra = make({"r1": {"bob": None}})
    assert ra.sync_room("r1") == {"granted": 2, "revoked": 0}
    assert acl.can("a1", principal_for("bob"), "read") and acl.can("a2", principal_for("bob"), "read")
    for permission in ("edit", "review", "share", "admin"):
        assert not acl.can("a1", principal_for("bob"), permission)
    assert not acl.can("a1", principal_for("mallory"), "read")


def test_a_person_who_leaves_loses_access_and_a_second_sync_changes_nothing():
    readers = {"r1": {"bob": None, "carol": None}}
    store, acl, ra = make(readers)
    ra.sync_room("r1")
    assert ra.sync_room("r1") == {"granted": 0, "revoked": 0}
    del readers["r1"]["carol"]
    assert ra.sync_room("r1") == {"granted": 0, "revoked": 2}
    assert not acl.can("a1", principal_for("carol"), "read") and acl.can("a1", principal_for("bob"), "read")


def test_an_artifact_in_two_rooms_stays_readable_while_either_room_has_the_person():
    store, acl, ra = make({"r1": {"bob": None}, "r2": {"bob": None}})
    ra.sync_room("r1")
    ra.sync_room("r2")                                           # r2 now owns bob's grant on a2
    store.rooms["r2"] = []
    ra.sync_room("r2")                                           # r2 lets go; r1 still has bob and gets it back
    assert acl.can("a2", principal_for("bob"), "read") is True
    store.rooms["r1"] = []
    ra.sync_room("r1")
    assert acl.can("a2", principal_for("bob"), "read") is False


def test_a_guest_grant_ends_with_the_membership():
    store, acl, ra = make({"r1": {"guest": 150.0}}, now=100.0)
    ra.sync_room("r1")
    assert acl.can("a1", principal_for("guest"), "read")
    acl.now = lambda: 151.0
    assert not acl.can("a1", principal_for("guest"), "read")


def test_a_newly_linked_artifact_is_synced_for_its_rooms_only():
    store, acl, ra = make({"r1": {"bob": None}})
    store.rooms["r1"].append("a3")
    assert ra.sync_artifact("a3") == {"granted": 1, "revoked": 0}
    assert acl.can("a3", principal_for("bob"), "read") and not acl.can("a1", principal_for("bob"), "read")


def test_an_explicit_grant_is_never_touched_by_the_sync():
    store, acl, ra = make({"r1": {}})
    acl.grant("a1", "api-key:abc:operator", {"read", "edit"})
    acl.grant("a1", principal_for("bob"), {"read"}, granted_by="someone-else")
    ra.sync_room("r1")
    assert acl.can("a1", "api-key:abc:operator", "edit") and acl.can("a1", principal_for("bob"), "read")


def test_notify_queues_and_drain_syncs_and_retries_a_failure():
    readers = {"r1": {"bob": None}}
    store, acl, ra = make(readers)
    ra.notify("r1")
    ra.notify("r1")
    assert ra.drain() == 1 and acl.can("a1", principal_for("bob"), "read")
    boom = {"on": True}
    real = store.call

    def flaky(method, *args):
        if boom["on"]:
            raise RuntimeError("db down")
        return real(method, *args)
    ra.call = flaky
    ra.notify("r1")
    ra.drain()
    boom["on"] = False
    assert ra.drain() == 1                                       # the failed room was queued again


# ---- through the real collab service: membership events drive the grants, the route reads only through them
from tests.control_plane.test_collab_tasks import code, svc, w  # noqa: E402,F401


class RegistryStub:
    """Just enough registry: artifacts bound to rooms, ACL-filtered like list_by_context."""

    def __init__(self, acl):
        self.acl, self.bound = acl, {"a1": "ROOM"}

    def call(self, method, *args, **kwargs):
        if method == "room_artifact_ids":
            return [a for a, r in self.bound.items() if r == args[0]]
        if method == "list_links":
            return [{"room_id": self.bound[args[0]]}]
        if method == "list_by_context":
            who = kwargs["principal_id"]
            return {"artifacts": [{"artifact_id": a} for a, r in self.bound.items()
                                  if r == kwargs["ref"] and self.acl.can(a, who, "read")]}
        raise AssertionError(method)


def wire(w):
    acl = ArtifactAcl(now=w.s._now)
    reg = RegistryStub(acl)
    reg.bound = {"a1": w.room}
    ra = RoomArtifactAccess(acl, reg.call, w.s.room_readers, now=w.s._now)
    w.s.on_room_members_changed = ra.notify
    w.s.artifact_room_handle = lambda uid, room, query: reg.call(
        "list_by_context", principal_id=principal_for(uid), kind="room", ref=room, **query)
    return acl, reg, ra


def test_the_route_is_a_404_while_the_feature_is_off(w):
    assert code(w.s.room_artifacts, w.bob, w.room, {}) == 404


def test_a_member_lists_the_rooms_artifacts_a_stranger_gets_a_404(w):
    acl, reg, ra = wire(w)
    ra.sync_room(w.room)
    assert [a["artifact_id"] for a in w.s.room_artifacts(w.bob, w.room, {})["artifacts"]] == ["a1"]
    assert [a["artifact_id"] for a in w.s.room_artifacts(w.carol, w.room, {})["artifacts"]] == ["a1"]   # a viewer reads
    assert code(w.s.room_artifacts, w.mallory, w.room, {}) == 404
    agent, _ = w.agent("alice")
    assert code(w.s.room_artifacts, agent, w.room, {}) == 404


def test_removing_a_member_removes_their_grants_through_the_hook(w):
    acl, reg, ra = wire(w)
    ra.sync_room(w.room)
    w.s.remove_member(w.alice, w.room, "bob")
    assert ra.drain() == 1
    assert code(w.s.room_artifacts, w.bob, w.room, {}) == 404
    assert not acl.can("a1", principal_for("bob"), "read")
    assert acl.can("a1", principal_for("carol"), "read")


def test_adding_a_member_gives_access_after_the_hook_drains(w):
    acl, reg, ra = wire(w)
    ra.sync_room(w.room)
    w.s.add_member(w.alice, w.room, {"user_id": "mallory", "role": "viewer"})
    assert ra.drain() == 1
    mallory = next(p for p in [w.mallory])
    assert [a["artifact_id"] for a in w.s.room_artifacts(mallory, w.room, {})["artifacts"]] == ["a1"]


def test_a_failing_hook_never_fails_the_membership_change(w):
    def boom(room_id):
        raise RuntimeError("queue broken")
    w.s.on_room_members_changed = boom
    w.s.remove_member(w.alice, w.room, "bob")
    assert w.s.stats["member_hook_failures"] == 1


# ---- the server wiring: default off, needs both the umbrella flag and its own
def _wiring(config, enabled=True, store=True, acl=True):
    from types import SimpleNamespace

    from herdr_engineering.control_plane.api import ControlPlaneServer
    me = SimpleNamespace(unified_workspace_enabled=enabled)
    collab = SimpleNamespace(on_room_members_changed=None, artifact_room_handle=None,
                             room_readers=lambda room: {}, room_ids=lambda: [])
    ControlPlaneServer._wire_room_artifact_access(me, collab, (lambda *a, **k: {}) if store else None,
                                                  ArtifactAcl() if acl else None, config)
    if me.room_artifact_access is not None:
        me.room_artifact_access.stop()
    return me, collab


def test_wiring_is_off_unless_both_flags_are_exactly_true():
    for config, enabled in ((None, True), ({}, True), ({"room_artifact_access": "yes"}, True),
                            ({"room_artifact_access": True}, False)):
        me, collab = _wiring(config, enabled)
        assert me.room_artifact_access is None and collab.on_room_members_changed is None
    for kwargs in ({"store": False}, {"acl": False}):
        me, collab = _wiring({"room_artifact_access": True}, **kwargs)
        assert me.room_artifact_access is None


def test_wiring_hooks_membership_and_the_route_when_on():
    me, collab = _wiring({"room_artifact_access": True})
    assert collab.on_room_members_changed == me.room_artifact_access.notify and callable(collab.artifact_room_handle)


# ---- R-RA2: room sync never overwrites or weakens a grant another source holds
def test_a_direct_write_grant_survives_room_sync_and_room_removal():
    readers = {"r1": {"bob": None}}
    store, acl, ra = make(readers)
    acl.grant("a1", principal_for("bob"), {"read", "edit"}, granted_by="control-plane")
    assert ra.sync_room("r1") == {"granted": 1, "revoked": 0}                 # only a2 is new
    held = acl.held("a1", principal_for("bob"))
    assert held["permissions"] == ["edit", "read"] and held["granted_by"] == "control-plane"
    del readers["r1"]["bob"]
    ra.sync_room("r1")                                                        # room removal revokes its own part only
    assert acl.can("a1", principal_for("bob"), "edit")
    assert not acl.can("a2", principal_for("bob"), "read")


def test_a_direct_read_grant_is_not_taken_over_by_a_room_and_so_not_revoked_with_it():
    readers = {"r1": {"bob": None}}
    store, acl, ra = make(readers)
    acl.grant("a1", principal_for("bob"), {"read"}, expires_at=500.0, granted_by="someone")
    ra.sync_room("r1")
    assert acl.held("a1", principal_for("bob"))["granted_by"] == "someone"
    readers["r1"].clear()
    ra.sync_room("r1")
    assert acl.can("a1", principal_for("bob"), "read")


def test_a_room_does_not_weaken_another_rooms_longer_grant():
    store, acl, ra = make({"r1": {"bob": None}, "r2": {"bob": 150.0}}, now=100.0)
    ra.sync_room("r1")
    ra.sync_room("r2")                                                        # a guest-style expiry must not shorten r1's
    assert acl.held("a2", principal_for("bob"))["expires_at"] is None
    acl.now = lambda: 200.0
    assert acl.can("a2", principal_for("bob"), "read")


def test_a_longer_room_grant_replaces_a_shorter_one_from_another_room():
    store, acl, ra = make({"r1": {"bob": 150.0}, "r2": {"bob": None}}, now=100.0)
    ra.sync_room("r1")
    ra.sync_room("r2")
    assert acl.held("a2", principal_for("bob"))["expires_at"] is None


# ---- turning the flag off leaves written grants behind; this is the explicit way to remove them
def test_revoke_all_room_grants_removes_only_room_sourced_grants_and_is_idempotent():
    store, acl, ra = make({"r1": {"bob": None}, "r2": {"bob": None}})
    ra.sync_room("r1")
    ra.sync_room("r2")
    acl.grant("a9", principal_for("bob"), {"read", "edit"}, granted_by="control-plane")
    acl.grant("a9", "api-key:abc:operator", {"read"})
    assert ra.revoke_all_room_grants() == 2                                   # a1 (r1) and a2
    assert acl.held("a1", principal_for("bob")) is None and acl.held("a2", principal_for("bob")) is None
    assert acl.can("a9", principal_for("bob"), "edit") and acl.can("a9", "api-key:abc:operator", "read")
    assert ra.revoke_all_room_grants() == 0
