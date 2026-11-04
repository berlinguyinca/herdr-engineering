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
import threading

PRIVATE = {"visibility_scope": "private"}
_NODE = re.compile(r"artifact:(?P<id>[^@]+)(?:@(?P<version>.+))?\Z")


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


def version_id_of(node_id: str) -> str | None:
    """The ``<version_id>`` of ``artifact:<id>@<version_id>``; None for the logical node."""
    match = _NODE.match(node_id or "")
    return match.group("version") if match else None


class ArtifactGraphGate:
    """``fn(principal, node) -> bool`` for ``GraphProjection.set_node_gate('artifact', gate)``.

    Besides the ACL ``read`` check it holds the RETRACTION set. A retracted artifact or version is hidden from every
    reader (admins included) on the next read, in every replay (the gate is evaluated against the current projection,
    also for as-of reads), and for its edges (an edge needs both endpoints). The set is server state, persisted by the
    registry (``ArtifactStore.record_retraction`` / ``load_retractions``), not graph state: a label can never narrow, and
    a state marker only counts for its first writer. A retracted version that is still the head the logical node
    reports hides the logical node too (it carries that version's id and digests); a later correction replaces the head
    and the logical node reappears for the ACL readers."""

    def __init__(self, acl):
        self.acl = acl
        self._lock = threading.Lock()
        self._retracted_artifacts: set[str] = set()
        self._retracted_versions: set[tuple[str, str]] = set()

    def retract(self, artifact_id: str, version_id: str | None = None) -> None:
        with self._lock:
            if version_id:
                self._retracted_versions.add((artifact_id, version_id))
            else:
                self._retracted_artifacts.add(artifact_id)

    def load_retractions(self, rows) -> int:
        """Rows of ``{"artifact_id", "version_id"|None}`` from the registry at startup."""
        n = 0
        for row in rows or ():
            if row.get("artifact_id"):
                self.retract(row["artifact_id"], row.get("version_id") or None)
                n += 1
        return n

    def artifact_retracted(self, artifact_id: str) -> bool:
        with self._lock:
            return artifact_id in self._retracted_artifacts

    def version_retracted(self, artifact_id: str, version_id: str) -> bool:
        with self._lock:
            return artifact_id in self._retracted_artifacts or (artifact_id, version_id) in self._retracted_versions

    def is_retracted(self, node) -> bool:
        """``node`` is a graph node (the read gate) or a node id."""
        node_id = node if isinstance(node, str) else getattr(node, "id", "")
        artifact_id = artifact_id_of(node_id)
        if not artifact_id:
            return False
        version_id = version_id_of(node_id)
        if version_id:
            return self.version_retracted(artifact_id, version_id)
        if self.artifact_retracted(artifact_id):
            return True
        # the logical node reports its head's id and digests as attributes: hide it while the head it SHOWS (the
        # displayed primary value) or the newest one written is a retracted version (fail closed on either)
        if isinstance(node, str):
            return False
        heads = set()
        try:
            shown = node.attr_values().get("version_id")
            reg = node.attrs.get("version_id")
            newest = reg.value() if reg is not None else None
            heads = {h for h in (shown, newest) if isinstance(h, str)}
        except Exception:                                  # noqa: BLE001 - an unreadable node fails closed
            return True
        with self._lock:
            return any((artifact_id, head) in self._retracted_versions for head in heads)

    def _holds(self, principal, node_id, permission) -> bool:
        pid = (principal or {}).get("id")
        artifact_id = artifact_id_of(node_id)
        if not pid or not artifact_id:
            return False
        return any(_same_identity(pid, holder) for holder in self.acl.principals_with(artifact_id, permission))

    def __call__(self, principal, node) -> bool:
        if self.is_retracted(node):
            return False
        return self._holds(principal, getattr(node, "id", ""), "read")

    def may_write(self, actor, node_id) -> bool:
        """Admission: only a principal holding ``edit`` on the artifact may write fields of its graph nodes, so a stranger
        cannot pre-create or overlay ``artifact:<id>`` with a forged label or version facts."""
        return self._holds(actor, node_id, "edit")


def install_gate(graph, acl) -> ArtifactGraphGate | None:
    """Install the ACL read gate. ``acl=None`` (no ACL configured) installs nothing and emission stays ``private``."""
    if acl is None:
        return None
    gate = ArtifactGraphGate(acl)
    graph.set_node_gate("artifact", gate)
    graph.set_write_gate("artifact", gate.may_write)
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
