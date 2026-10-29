"""Map an artifact's real ACL onto lineage-graph visibility.

The graph's scope labels are WRITE-ONCE facts of an event and never narrow for a commons node (a writer's labels only
widen, lineage-events.md "Visibility is per writer"). An ACL, by contrast, changes: grants are added and revoked. So the
mapping has two halves, neither of which widens anything:

* ``visibility_fields``: the label stamped on the artifact's events. With the read gate installed it is ``org`` (readable
  by fleet keys, never by a signed-in person) or, when the caller knows the room/mission the artifact belongs to, that
  contextual label. Without the gate it is ``private`` (the committer alone), which is the pre-existing fail-closed default.
* ``ArtifactGraphGate``: a read-time check, evaluated per request like room membership, that ANDs the artifact's ACL
  ``read`` permission onto the label. A revoke or an expired grant therefore hides the node (and its edges, whose
  visibility needs both endpoints) on the very next read. The label can only be narrowed by this gate, never widened by it.

Identity spaces: the ACL speaks ``api-key:<sha256(key)[:16]>:<role>``; the graph speaks ``key:<sha256(key)[:12]>``.
The first 12 hex digits are the same digest, so a graph key principal matches an ACL id by that prefix. A signed-in person
(``user:<id>``) matches only an ACL grant naming exactly ``user:<id>``; no such grant is created today, so people see
artifact nodes only after an operator grants them explicitly (fail closed).
"""
from __future__ import annotations

import re

PRIVATE = {"visibility_scope": "private"}
_NODE = re.compile(r"artifact:(?P<id>[^@]+)(?:@.+)?\Z")


def _same_identity(graph_id: str, acl_id: str) -> bool:
    if graph_id == acl_id:
        return True
    if graph_id.startswith("key:") and acl_id.startswith("api-key:"):
        digest = acl_id.split(":", 2)[1] if acl_id.count(":") >= 1 else ""
        return len(graph_id) == 16 and len(digest) >= 12 and digest[:12] == graph_id[4:]
    return False


def artifact_id_of(node_id: str) -> str | None:
    """``artifact:<id>`` (logical) and ``artifact:<id>@<version_id>`` (immutable version) share one ACL."""
    match = _NODE.match(node_id or "")
    return match.group("id") if match else None


class ArtifactGraphGate:
    """``fn(principal, node) -> bool`` for ``GraphProjection.set_node_gate('artifact', gate)``."""

    def __init__(self, acl):
        self.acl = acl

    def __call__(self, principal, node) -> bool:
        pid = (principal or {}).get("id")
        artifact_id = artifact_id_of(getattr(node, "id", ""))
        if not pid or not artifact_id:
            return False
        return any(_same_identity(pid, holder) for holder in self.acl.principals_with(artifact_id, "read"))


def install_gate(graph, acl) -> ArtifactGraphGate | None:
    """Install the ACL read gate. ``acl=None`` (no ACL configured) installs nothing and emission stays ``private``."""
    if acl is None:
        return None
    gate = ArtifactGraphGate(acl)
    graph.set_node_gate("artifact", gate)
    return gate


def visibility_fields(*, gated: bool, room_id: str | None = None, mission_id: str | None = None) -> dict:
    """Envelope fields for an artifact event. Never wider than the gate can narrow back down."""
    if not gated:
        return dict(PRIVATE)
    if room_id and mission_id:
        return {"visibility_scope": "mission", "room_id": room_id, "mission_id": mission_id}
    if room_id:
        return {"visibility_scope": "room", "room_id": room_id}
    return {"visibility_scope": "org"}
