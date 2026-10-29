"""Email attachment -> permission-scoped, versioned artifact linked into the lineage graph (spec 7D, 10.5).

Control-plane level only, over existing parts: ``intel_attachments.triage`` decides whether the bytes may be stored at all,
``ArtifactStore.register_logical`` + ``commit_version`` make a VERSIONED artifact (so a corrected attachment is a new
version, never an overwrite), ``ArtifactAcl`` carries the permission scope, ``artifact_lineage`` records the graph node and
``artifact_scope`` keeps that node readable only by the ACL.

Rules enforced here:
* The ACL is the mail AUDIENCE the caller names (the consenting recipients) plus the owner. Nothing is granted to a room,
  a project or "everyone"; there is no parameter that would. Widening is a separate, explicit ACL grant by an admin.
* Original metadata (message/thread ids, sender, subject, internal date, filename, declared and sniffed type, size, sha256)
  is stored as artifact PROVENANCE (inside the ACL boundary). The graph gets ids and digests only, no subject, address or
  file name.
* The source conversation is a ``thread:`` node written by the same principal; a ``produced`` edge runs conversation ->
  artifact. The thread node stays ``private`` (its writer), so it widens nothing.
* A candidate issue is linked (``issue_link``) ONLY for a project-intelligence relationship that a person confirmed
  (``review_state == "confirmed"`` and ``derivation == "human_verified"`` with a verifier). Inferred, provisional and
  rejected relationships never reach the graph, whatever their confidence.
"""
from __future__ import annotations

import hashlib
import re
from email.utils import parseaddr

from . import intel_attachments as att
from . import intel_gmail as gm
from .artifact_lineage import emit_retraction, emit_version_lineage
from .artifact_scope import PRIVATE, visibility_fields

_REF = re.compile(r"(?P<kind>issue|pr):(?P<repo>\w[\w.-]*/\w[\w.-]*)#(?P<num>\d+)\Z")


def _account_tag(account: str) -> str:
    return hashlib.sha256(account.encode()).hexdigest()[:12]      # the mailbox address itself never enters the graph


def thread_node_id(account: str, thread_id: str) -> str:
    return f"thread:gmail-{_account_tag(account)}-{thread_id}"


def graph_ref(ref: str) -> str | None:
    """``issue:o/r#9`` (project intelligence) -> ``issue:github.com/o/r#9`` (graph); anything else -> None."""
    match = _REF.match(ref or "")
    return f"{match['kind']}:github.com/{match['repo']}#{match['num']}" if match else None


def mail_provenance(msg: dict, part_path: str, result: att.Triage, account: str) -> dict:
    headers = gm._headers(msg)
    part = dict(gm._walk(msg.get("payload") or {})).get(part_path) or {}
    return {"source": "gmail", "account": account, "message_id": msg.get("id"), "thread_id": msg.get("threadId"),
            "attachment_path": part_path, "from": parseaddr(headers.get("from", ""))[1].lower(),
            "subject": (headers.get("subject") or "")[:200], "message_id_header": headers.get("message-id"),
            "internal_date_ms": msg.get("internalDate"), "original_filename": att.safe_filename(part.get("filename") or ""),
            "declared_type": part.get("mimeType"), "sniffed_type": result.sniffed_type, "sha256": result.sha256,
            "triage": result.verdict}


def confirmed_issue_refs(intelligence, viewer, message_ref: str) -> list[str]:
    """Graph ids of issues/PRs that a PERSON confirmed for this message (project intelligence is the source of truth)."""
    out = []
    for rel in intelligence.neighbours(message_ref, viewer):
        if rel.review_state == "confirmed" and rel.derivation == "human_verified" and rel.verified_by \
                and message_ref in (rel.from_ref, rel.to_ref):
            other = rel.to_ref if rel.from_ref == message_ref else rel.from_ref
            mapped = graph_ref(other)
            if mapped and mapped not in out:
                out.append(mapped)
    return sorted(out)


async def attach_to_artifact(*, store, acl, ingest, principal: dict, msg: dict, part_path: str, data: bytes, account: str,
                             owner: str, audience: tuple[str, ...] = (), previous: dict | None = None,
                             intelligence=None, viewer=None, gated: bool = True, dedupe: dict | None = None) -> dict:
    """Store one accepted attachment as a new artifact version (``previous={"artifact_id", "version_id"}`` makes it a
    correction of that artifact). Returns ``{"verdict", "artifact_id", "version_id", "audience", "thread_node",
    "issue_links", "graph"}``; quarantined or rejected bytes are never stored (verdict and reasons only).
    ``owner`` and ``audience`` are ACL principal ids (``api-key:...``). ``gated=False`` keeps the graph node
    ``private``; ``gated=True`` is honoured only when the ACL read gate is really installed on the ingest's graph.
    ``dedupe`` (a caller-owned dict) makes a redelivered (account, message, part, sha256) return the first result
    instead of creating a second artifact. A correction (``previous``) needs ``edit`` on that artifact for ``owner`` and
    may not add readers: a wider audience is a separate, explicit ACL grant."""
    part = dict(gm._walk(msg.get("payload") or {})).get(part_path) or {}
    filename = part.get("filename") or "attachment"
    result = att.triage(data, part.get("mimeType"), filename)
    out = {"verdict": result.verdict, "reasons": list(result.reasons), "sha256": result.sha256, "artifact_id": None,
           "version_id": None, "audience": [], "thread_node": None, "issue_links": [], "graph": None}
    if result.verdict != "accept":
        return out
    gated = gated and getattr(getattr(getattr(ingest, "repo", None), "graph", None), "_node_gates", {}).get("artifact") \
        is not None
    key_ = (account, str(msg.get("id")), part_path, result.sha256)
    if dedupe is not None and key_ in dedupe:
        return {**dedupe[key_], "verdict": "duplicate"}
    if previous:
        if not acl.can(previous["artifact_id"], owner, "edit"):
            return {**out, "verdict": "forbidden", "reasons": ["owner may not edit the artifact being corrected"]}
        readers = set(acl.principals_with(previous["artifact_id"], "read"))
        if set(audience) - readers:
            return {**out, "verdict": "audience_widened",
                    "reasons": ["a correction cannot add readers; grant access to the artifact explicitly"]}
    provenance = mail_provenance(msg, part_path, result, account)
    key, digest = store._rf.put(data)
    if previous:
        artifact_id, base = previous["artifact_id"], previous["version_id"]
    else:
        record = await store.register_logical(original_filename=att.safe_filename(filename),
                                              artifact_type="email_attachment", created_by=owner,
                                              mime_type=result.sniffed_type)
        artifact_id, base = record["artifact_id"], None
    version = await store.commit_version(
        artifact_id, base_version_id=base, source_manifest_blob=None,
        output_manifest=[{"name": att.safe_filename(filename), "media_type": result.sniffed_type, "storage_key": key,
                          "sha256": digest, "size_bytes": len(data)}],
        provenance=provenance, created_by=owner)
    granted = [owner]
    acl.grant(artifact_id, owner, {"read", "edit", "review", "share", "admin"})
    for principal_id in dict.fromkeys(audience):
        if principal_id != owner:
            acl.grant(artifact_id, principal_id, {"read"})          # read only; never edit/share
            granted.append(principal_id)
    version = {**version, "artifact_id": artifact_id}
    scope = visibility_fields(gated=gated)
    out.update(artifact_id=artifact_id, version_id=version["version_id"], audience=granted)
    out["graph"] = emit_version_lineage(ingest, version, principal, scope=scope)
    node_id = f"artifact:{artifact_id}"
    thread = thread_node_id(account, str(msg["threadId"]))
    source = f"mail-{artifact_id}-{version['version_id']}"[:64]     # one source per stored version: own sequence
    try:
        ingest.ingest({"schema_version": 2, "event_id": f"{source}-thread", "source": source,
                       "source_sequence": 1, "entity_type": "lineage", "entity_id": thread, "event_type": "node.upsert",
                       "payload": {"node_type": "thread", "node_id": thread, "label": "mail conversation",
                                   "attrs": {"source_ref": f"gmail:{_account_tag(account)}:{msg['id']}"}}, **PRIVATE}, principal)
        ingest.ingest({"schema_version": 2, "event_id": f"{source}-produced",
                       "source": source, "source_sequence": 2, "entity_type": "lineage",
                       "entity_id": thread, "event_type": "edge.add",
                       "payload": {"edge_type": "produced", "from_id": thread, "to_id": node_id}, **scope}, principal)
        out["thread_node"] = thread
    except Exception as exc:                                    # noqa: BLE001 - the artifact is already durable
        out["graph"] = {**(out["graph"] or {}), "thread_error": str(exc) or type(exc).__name__}
    if intelligence is not None and viewer is not None:
        for issue in confirmed_issue_refs(intelligence, viewer, f"email:{msg['id']}"):
            try:
                seq = 3 + 2 * len(out["issue_links"])
                ingest.ingest({"schema_version": 2, "event_id": f"{source}-issue-{seq}", "source": source,
                               "source_sequence": seq, "entity_type": "lineage", "entity_id": issue,
                               "event_type": "node.upsert", "payload": {"node_type": "issue", "node_id": issue},
                               **PRIVATE}, principal)          # an empty "ensure": defines the endpoint, owns nothing
                ingest.ingest({"schema_version": 2, "event_id": f"{source}-link-{seq + 1}", "source": source,
                               "source_sequence": seq + 1, "entity_type": "lineage", "entity_id": node_id, "event_type": "edge.add",
                               "payload": {"edge_type": "issue_link", "from_id": node_id, "to_id": issue}, **scope},
                              principal)
                out["issue_links"].append(issue)
            except Exception:                                   # noqa: BLE001 - issue node unknown/unreadable: no link
                pass
    if dedupe is not None:
        dedupe[key_] = dict(out)
    return out


def retract_attachment(*, ingest, principal: dict, artifact_id: str, version_id: str, reason: str,
                       head: bool = False, gated: bool = True, graph=None) -> dict:
    """Record that a sender retracted an attachment. The version stays stored and readable by its audience."""
    return emit_retraction(ingest, artifact_id, version_id, principal, reason, scope=visibility_fields(gated=gated),
                           head=head, graph=graph)
