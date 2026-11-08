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


def brief_hook(server):
    """(collab_user, room_id, brief) -> dict: the collab service's in-process ``submit_brief``, as the room OWNER's user.

    There is no collab HTTP route or MCP tool for this. The user is the one the operator's YAML maps the assistant to
    (``actions.collab_users``); it is built exactly like an authenticated person, never an agent, and the room's own owner check
    decides (anyone who does not own the room gets the same 404 as a missing room)."""
    def hook(collab_user: str, room_id: str, brief: dict):
        from .collab import CollabError, Principal
        service = server.collab
        if service is None:
            raise CollabError(404, "not found")
        if getattr(service, "degraded", False):
            raise CollabError(503, "collaboration is disabled; ask the operator")
        if not service.auth.exists(collab_user):
            raise CollabError(404, "not found")
        if service.approval is not None and service._users.get(collab_user, {}).get("status") != "approved":
            raise CollabError(404, "not found")
        return service.submit_brief(Principal("user", collab_user, collab_user), room_id, brief)
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
            if record is None or record.get("artifact_type") != "email_draft":
                raise LookupError("not found")             # only an existing email draft takes a new draft version
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


class ExternalActionsBridge:
    """Team collaboration slice 5: the in-process bridge from the collab service to the assistant's external action ledger.

    The collab service has already decided WHO may do WHAT (``collab_team_external.py``); this maps the room owner's collaboration
    user to the operator's assistant identity (``actions.collab_users``, the reverse of the slice 4 mapping) and calls the ledger.
    A collaboration user the YAML does not map answers like a missing room. The ledger's own refusals become collab errors."""

    def __init__(self, server) -> None:
        self.server = server

    def _svc(self):
        return self.server.assistant.external_actions

    def _scope(self, collab_user: str) -> tuple[str, str]:
        from .collab import CollabError
        users = self.server.assistant.config["actions"]["collab_users"]
        user_id = next((u for u, c in users.items() if c == collab_user), None)
        if user_id is None:
            raise CollabError(404, "not found")
        return self.server.assistant.tenant_for(user_id), user_id

    @staticmethod
    def error(exc: Exception, state_error: int = 400):
        """The collab error a ledger exception stands for."""
        from .assistant import (
            AssistantError,
            AssistantForbidden,
            AssistantNotFound,
            AssistantUnavailable,
            RateLimited,
        )
        from .collab import CollabError
        from .external_actions import Mismatch
        if isinstance(exc, CollabError):
            return exc
        for cls, status, message in ((AssistantNotFound, 404, "not found"), (AssistantForbidden, 403, "forbidden"),
                                     (RateLimited, 429, str(exc)), (AssistantUnavailable, 503, str(exc)),
                                     (Mismatch, 409, str(exc)), (AssistantError, state_error, str(exc)), (ValueError, 400, str(exc))):
            if isinstance(exc, cls):
                return CollabError(status, message)
        raise exc

    def _guard(self):
        service = self.server.collab
        if service is None or getattr(service, "degraded", False):
            from .collab import CollabError
            raise CollabError(503, "collaboration is disabled; ask the operator")

    def file(self, owner: str, filer: str, kind: str, ctx: dict) -> dict:
        self._guard()
        tenant, user = self._scope(owner)
        try:
            return self._svc().file(tenant_id=tenant, user_id=user, kind=kind, ctx=ctx,
                                    requested_by=f"assistant:{user}" if filer == "assistant" else filer)
        except Exception as exc:                                   # noqa: BLE001 - mapped, or re-raised if unexpected
            raise self.error(exc) from None

    def rows(self, owner: str, room_id: str, filer: str | None) -> list[dict]:
        self._guard()
        tenant, user = self._scope(owner)
        try:
            return self._svc().for_room(room_id, tenant_id=tenant, user_id=user, filed_by=filer)
        except Exception as exc:                                   # noqa: BLE001
            raise self.error(exc) from None

    def room_of(self, owner: str, action_id: str) -> str | None:
        tenant, user = self._scope(owner)
        try:
            return (self._svc().get(action_id, tenant_id=tenant, user_id=user) or {}).get("room_id")
        except Exception:                                          # noqa: BLE001 - unknown means no room
            return None

    def act(self, owner: str, room_id: str, action_id: str, op: str, body: dict) -> dict:
        """Decide or retry. The action must belong to THIS room: another room's id is a missing action."""
        from .collab import CollabError
        self._guard()
        tenant, user = self._scope(owner)
        svc = self._svc()
        try:
            if svc.get(action_id, tenant_id=tenant, user_id=user).get("room_id") != room_id:
                raise CollabError(404, "not found")
            if op == "retry":
                return svc.retry(action_id, tenant_id=tenant, user_id=user, actor=user, expected_sha256=body.get("expected_sha256"))
            return svc.decide(action_id, tenant_id=tenant, user_id=user, actor=user, decision=body.get("decision"),
                              expected_sha256=body.get("expected_sha256"))
        except Exception as exc:                                   # noqa: BLE001
            raise self.error(exc) from None


class _MappedWorkspace:
    """The Google workspace with the single-operator mapping the other assistant Google hooks use (the data key, never the actor)."""

    def __init__(self, server) -> None:
        self._server = server

    def create_draft(self, tenant, user, *args, **kwargs):
        return self._server.google.create_draft(*self._server._canonical_person(tenant, user), *args, **kwargs)

    def find_draft_by_key(self, tenant, user, key):
        return self._server.google.find_draft_by_key(*self._server._canonical_person(tenant, user), key)


def wire_external_providers(server) -> None:
    """Give the assistant's external action ledger its two provider clients (slice 5). Called only with the flag on.

    GitHub: the REST client over the GitHub App's ``team_issues`` write capability, else the server-held ``github.token`` (the
    same credential storage the escalation issues use, YAML/host config only, never an environment variable). Gmail: the
    person's own Google connection, DRAFTS only. Neither is reachable when it is not configured; the action then fails with a
    fixed reason and the operator sees it."""
    from .deployment_escalation import GitHubIssueSink
    from .external_actions import GitHubIssues, GmailDrafts, sink_credential
    ea = server.assistant.external_actions
    gh = ea.cfg["github"]
    ea.github = GitHubIssues(gh["api_base"], sink_credential(GitHubIssueSink(capability="team_issues")), timeout=gh["timeout_seconds"])
    ea.gmail = GmailDrafts(lambda: _MappedWorkspace(server) if getattr(server, "google", None) is not None else None)
