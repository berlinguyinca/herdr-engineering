"""Server-side hooks that let the assistant's action tools reach EXISTING control-plane endpoints.

Each hook is a thin closure over the running server. The assistant owns no scheduler, store or credential of its
own: a hook acts as the *user the assistant works for* (resolved from the server's key table, never from tool
arguments) and every refusal the underlying service makes (room membership, quorum scope, artifact ACL) stands.
"""
from __future__ import annotations

import hashlib
import json

from .assistant_lineage import principal_resolver


def resource_principal(keys: dict[str, str], user_id: str) -> str | None:
    """The artifact-owner id (``api-key:<digest>:<role>``) of the key that is ``user_id`` (``key:<digest>``)."""
    if not keys:
        return f"api-key:{hashlib.sha256(b'').hexdigest()[:16]}:operator" if user_id == "open-listener" else None
    for key, role in keys.items():
        if f"key:{hashlib.sha256(key.encode()).hexdigest()[:12]}" == user_id:
            return f"api-key:{hashlib.sha256(key.encode()).hexdigest()[:16]}:{role}"
    return None


def rereview_hook(server):
    """(tenant, user, pr_id, head) -> detail. The existing ``.../review-quorum/rereview`` ledger, as this user."""
    resolve = principal_resolver(server._keys)

    def hook(_tenant: str, user_id: str, pr_id: str, head: str | None) -> str:
        from .review_quorum import RereviewError
        principal = resolve(user_id)
        if principal is None:
            raise LookupError("not found")
        if server.review_quorum is None:
            raise ValueError("review quorum is not configured")
        try:
            status, out = server.rereview.request(server.repo.graph, principal, pr_id, server.review_quorum, head)
        except KeyError:
            raise LookupError("not found") from None
        except RereviewError as exc:
            raise ValueError(exc.message) from None
        got = (out.get("rereview_request") or {}).get("head") or ""
        return f"re-review {'recorded' if status == 201 else 'already recorded'} for head {got[:12]}"
    return hook


def comment_hook(server):
    """(collab_user, room_id, body) -> (status, json): the existing collab comments route, as a signed-in USER.

    Built exactly like an authenticated person: not admin, not approver, never an agent. The room's own
    membership and approval-mode checks decide; an unknown or unapproved user is refused here too.
    """
    def hook(collab_user: str, room_id: str, body: dict):
        from .collab import CollabError, Principal
        service = server.collab
        if service is None:
            raise ValueError("collaboration is not enabled")
        if getattr(service, "degraded", False):
            raise CollabError(503, "collaboration is disabled; ask the operator")
        if not service.auth.exists(collab_user):
            raise CollabError(403, "collaboration user is not enabled")
        if service.approval is not None and service._users.get(collab_user, {}).get("status") != "approved":
            raise CollabError(403, "waiting for approval")
        return service.add_comment(Principal("user", collab_user, collab_user), room_id, body)
    return hook


def draft_writer_hook(server):
    """(user, draft, idempotency key) -> committed version: a versioned artifact via the existing registry."""
    def hook(user_id: str, draft: dict, idem: str) -> dict:
        from .artifact_lineage import emit_version_lineage
        store, rustfs = server.artifact_store, server.rustfs
        owner = resource_principal(server._keys, user_id)
        if store is None or rustfs is None or owner is None:
            raise RuntimeError("versioned artifact registry is unavailable")
        call = getattr(store, "sync_call", None) or store
        artifact_id = draft.pop("artifact_id", "") or ""
        data = json.dumps(draft, sort_keys=True, separators=(",", ":")).encode()
        storage_key, sha256 = rustfs.put(data)
        base = None
        if artifact_id:
            may_edit = (server.artifact_acl.can(artifact_id, owner, "edit") if server.artifact_acl is not None
                        else True) or bool(call("report_permission", artifact_id, owner, "edit"))
            record = call("get_record", artifact_id, principal_id=owner) if may_edit else None
            if record is None:
                raise LookupError("not found")
            base = record.get("current_version_id")
        else:
            artifact_id = call("register_logical", original_filename="email-draft.json",
                               artifact_type="email_draft", created_by=owner,
                               mime_type="application/json")["artifact_id"]
            if server.artifact_acl is not None:
                server.artifact_acl.grant(artifact_id, owner, {"read", "edit", "review", "share", "admin"})
        committed = call("commit_version", artifact_id, base_version_id=base, source_manifest_blob=None,
                         output_manifest=[{"storage_key": storage_key, "sha256": sha256,
                                           "size_bytes": len(data), "name": "draft.json"}],
                         provenance={"principal_id": owner, "origin": "personal-assistant",
                                     "kind": "email_draft", "sent": False},
                         created_by=owner, idempotency_key=hashlib.sha256(idem.encode()).hexdigest()[:64])
        principal = principal_resolver(server._keys)(user_id)
        if principal is not None:
            emit_version_lineage(server.ingest, committed, principal)
        return committed
    return hook
