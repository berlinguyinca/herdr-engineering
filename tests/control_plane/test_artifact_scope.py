"""Artifact ACL -> lineage graph visibility, correction and retraction (spec 10.5, 10.6).

The graph label is stamped once and never narrows for a commons node, so the ACL is applied as a read-time gate:
these tests pin that a grant shows the node, a revoke or expiry hides it at the next read (also after a rebuild from
stored events), and that nobody outside the ACL (operators included) can see node, version nodes or edges.
"""
import pytest

from herdr_engineering.control_plane.artifact_acl import ArtifactAcl
from herdr_engineering.control_plane.artifact_lineage import emit_retraction, emit_version_lineage
from herdr_engineering.control_plane.artifact_scope import (
    artifact_id_of,
    install_gate,
    visibility_fields,
)
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_graph import OPERATOR, _AnyKey

DIGEST = "b" * 64
# graph principals (key:<12 hex>) and the ACL ids of the same keys (api-key:<16 hex>:<role>)
OWNER = {"kind": "agent", "id": "key:aaaaaaaaaaaa", "authenticated_by": "api_key"}
READER = {"kind": "agent", "id": "key:bbbbbbbbbbbb", "authenticated_by": "api_key"}
STRANGER = {"kind": "agent", "id": "key:cccccccccccc", "authenticated_by": "api_key"}
ACL_OWNER, ACL_READER = "api-key:aaaaaaaaaaaa1111:agent", "api-key:bbbbbbbbbbbb2222:agent"


def version(n, parent=None, **extra):
    return {"version_id": f"ver_{n}", "artifact_id": "art_1", "version_number": n, "parent_version_id": parent,
            "output_manifest": [{"sha256": DIGEST, "storage_key": f"sha256/bb/bb/{DIGEST}", "name": "p.png"}],
            "mission_id": "m1", **extra}


class World:
    def __init__(self, clock=None):
        self.repo = ControlPlaneRepo()
        self.svc = IngestService(self.repo, key_scopes=_AnyKey())
        self.acl = ArtifactAcl(now=clock or (lambda: 1000.0))
        self.gate = install_gate(self.repo.graph, self.acl)
        self.acl.grant("art_1", ACL_OWNER, {"read", "edit", "admin"})
        self.scope = visibility_fields(gated=True)

    def commit(self, n, parent=None, who=OWNER, **extra):
        return emit_version_lineage(self.svc, version(n, parent, **extra), who, scope=self.scope)

    def sees(self, who, node="artifact:art_1"):
        try:
            return self.repo.graph.get_node(who, node)["node"]["id"] == node
        except KeyError:
            return False


def test_id_mapping():
    assert artifact_id_of("artifact:art_1") == "art_1" and artifact_id_of("artifact:art_1@ver_2") == "art_1"
    assert artifact_id_of("pr:github.com/o/r#1") is None


def test_gated_scope_is_org_or_the_named_room_or_mission_and_ungated_stays_private():
    assert visibility_fields(gated=False, room_id="r1") == {"visibility_scope": "private"}
    assert visibility_fields(gated=True) == {"visibility_scope": "org"}
    assert visibility_fields(gated=True, room_id="r1") == {"visibility_scope": "room", "room_id": "r1"}
    assert visibility_fields(gated=True, room_id="r1", mission_id="m1") == {
        "visibility_scope": "mission", "room_id": "r1", "mission_id": "m1"}
    assert install_gate(ControlPlaneRepo().graph, None) is None


def test_only_acl_readers_see_the_node_its_versions_and_edges_not_even_operators():
    w = World()
    out = w.commit(1)
    assert out["error"] is None and out["scope"] == {"visibility_scope": "org"}
    assert w.sees(OWNER) and not w.sees(READER) and not w.sees(STRANGER) and not w.sees(OPERATOR)
    w.acl.grant("art_1", ACL_READER, {"read"})
    assert w.sees(READER) and w.sees(READER, "artifact:art_1@ver_1")
    assert not w.sees(STRANGER) and not w.sees(OPERATOR)
    listed = {n["id"] for n in w.repo.graph.list_nodes(STRANGER, "artifact")["nodes"]}
    assert listed == set()
    assert {n["id"] for n in w.repo.graph.list_nodes(READER, "artifact")["nodes"]} >= {"artifact:art_1"}


def test_a_grant_without_read_does_not_show_the_node():
    w = World()
    w.commit(1)
    w.acl.grant("art_1", ACL_READER, {"edit"})
    assert not w.sees(READER)


def test_revoke_and_expiry_narrow_on_the_next_read_and_after_rebuild():
    now = [1000.0]
    w = World(clock=lambda: now[0])
    w.commit(1)
    w.acl.grant("art_1", ACL_READER, {"read"}, expires_at=1500.0)
    assert w.sees(READER)
    now[0] = 1600.0                                    # expired grant
    assert not w.sees(READER)
    w.acl.grant("art_1", ACL_READER, {"read"})
    assert w.sees(READER)
    w.acl.revoke("art_1", ACL_READER)
    assert not w.sees(READER) and not w.sees(READER, "artifact:art_1@ver_1")
    w.acl.grant("art_1", ACL_READER, {"read"})
    w.repo._rebuild_graph()                             # restart: projection rebuilt from stored events
    assert w.sees(READER) and not w.sees(STRANGER)
    w.acl.revoke("art_1", ACL_READER)
    assert not w.sees(READER)


def test_neighborhood_and_edges_do_not_leak_an_unreadable_artifact():
    w = World()
    w.commit(1)                                         # produced edge from mission:m1
    assert w.repo.graph.neighborhood(OWNER, "artifact:art_1", depth=1)["nodes"]
    with pytest.raises(KeyError):
        w.repo.graph.neighborhood(STRANGER, "artifact:art_1", depth=1)


def test_acl_is_per_artifact():
    w = World()
    w.commit(1)
    w.acl.grant("art_other", ACL_READER, {"read"})
    assert not w.sees(READER)


def test_person_principal_needs_an_explicit_acl_grant():
    w = World()
    w.commit(1)
    person = {"kind": "member", "id": "user:alice", "authenticated_by": "collab",
              "access": {"rooms": frozenset(), "room_ws": frozenset(), "room_missions": frozenset()}}
    # org-labelled lineage is never readable by a person, whatever the ACL says: fail closed
    w.acl.grant("art_1", "user:alice", {"read"})
    assert not w.sees(person)


def test_room_context_label_reaches_room_members_and_the_acl_still_gates():
    w = World()
    w.scope = visibility_fields(gated=True, room_id="r1")
    assert w.commit(1)["scope"] == {"visibility_scope": "room", "room_id": "r1"}
    member = {"kind": "member", "id": "user:alice", "authenticated_by": "collab",
              "access": {"rooms": frozenset({"r1"}), "room_ws": frozenset(), "room_missions": frozenset()}}
    assert not w.sees(member)                           # room member, not on the ACL
    w.acl.grant("art_1", "user:alice", {"read"})
    assert w.sees(member)                               # both the room label and the ACL agree
    w.acl.revoke("art_1", "user:alice")
    assert not w.sees(member)


def test_a_refused_wider_scope_falls_back_to_private_never_wider():
    class Refusing(IngestService):
        def ingest(self, body, principal):
            if body["visibility_scope"] != "private":
                raise PermissionError("key may not address that room")
            return super().ingest(body, principal)

    repo = ControlPlaneRepo()
    svc = Refusing(repo, key_scopes=_AnyKey())
    out = emit_version_lineage(svc, version(1), OWNER, scope=visibility_fields(gated=True, room_id="r9"))
    assert out["error"] is None and out["scope"] == {"visibility_scope": "private"}
    assert repo.graph.get_node(OWNER, "artifact:art_1")["node"]
    with pytest.raises(KeyError):
        repo.graph.get_node(READER, "artifact:art_1")


def test_correction_is_a_new_version_and_retraction_keeps_the_old_one_inspectable():
    w = World()
    w.acl.grant("art_1", ACL_READER, {"read"})
    w.commit(1)
    w.commit(2, parent="ver_1")                         # corrected attachment
    graph = w.repo.graph
    head = graph.get_node(READER, "artifact:art_1")["node"]["attrs"]
    assert head["version_id"] == "ver_2" and head["parent_version_id"] == "ver_1"
    v1 = graph.get_node(READER, "artifact:art_1@ver_1")["node"]
    v2 = graph.get_node(READER, "artifact:art_1@ver_2")["node"]
    assert v1["attrs"]["content_digests"] == [DIGEST] and v2["attrs"]["version_number"] == 2
    sup = graph.get_edge(READER, "supersession", "artifact:art_1@ver_2", "artifact:art_1@ver_1")["edges"]
    assert len(sup) == 1
    # the sender retracts v1 (the wrong attachment) and later v2 too
    ret = emit_retraction(w.svc, "art_1", "ver_1", OWNER, "sent to the wrong thread", scope=w.scope)
    assert ret["error"] is None
    v1 = graph.get_node(READER, "artifact:art_1@ver_1")["node"]
    assert v1["state"] == "retracted" and v1["attrs"]["content_digests"] == [DIGEST]   # still inspectable
    assert graph.get_node(READER, "artifact:art_1@ver_2")["node"]["state"] == "ready"
    emit_retraction(w.svc, "art_1", "ver_2", OWNER, "withdrawn", scope=w.scope, head=True)
    assert graph.get_node(READER, "artifact:art_1")["node"]["state"] == "retracted"
    # a retraction does not widen anything
    assert not w.sees(STRANGER, "artifact:art_1@ver_1") and not w.sees(OPERATOR, "artifact:art_1@ver_1")
    w.acl.revoke("art_1", ACL_READER)
    assert not w.sees(READER, "artifact:art_1@ver_1")   # revoked reader loses the retracted version too


def test_retraction_is_idempotent_and_bounds_the_reason():
    w = World()
    w.commit(1)
    first = emit_retraction(w.svc, "art_1", "ver_1", OWNER, "x" * 5000, scope=w.scope)
    again = emit_retraction(w.svc, "art_1", "ver_1", OWNER, "x" * 5000, scope=w.scope)
    assert first["error"] is None and again["error"] is None
    assert w.repo.graph.get_node(OWNER, "artifact:art_1@ver_1")["node"]["state_reason"] == "x" * 200


def test_replaying_the_commit_is_deterministic():
    w = World()
    w.commit(1)
    before = w.repo.graph.snapshot()
    assert w.commit(1)["error"] is None
    assert w.repo.graph.snapshot() == before
