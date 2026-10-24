"""A2A (Agent2Agent, JSON-RPC binding) adapter for HCN rooms.

A thin, stateless translation layer, not a second source of truth: an A2A ``message/send``
becomes one room message through the same authorization as the REST API and MCP (agent
credential, room grant, per-agent limits). There is no task store; the room log is the only
record, so task methods answer with the protocol's standard "no such task / unsupported"
errors. Remote content is never echoed back: replies carry only our own sequence number.

Scope on purpose: send-only text. Reading a room stays on MCP/REST, where it is wrapped as
untrusted data. No files, URLs, push-notification callbacks or streaming exist here.

Bounded delegation (T11), only while the operator's kill switch is on: a coworker agent's *bounded task*
(the same HCN task store, never a second one) is an A2A Task. ``tasks/get`` and ``tasks/cancel`` read and
cancel it, ``tasks/list`` (an extension, A2A 0.3 has none) lists the open ones, and ``message/send`` with a
``taskId`` and ONE ``data`` part answers it (``accept``/``decline``/``progress``/``complete``/``fail``/``cancel``)
or submits an artifact proposal (``propose``). Each of these calls the very same CollabService method as the
MCP tool and the REST route, so the checks cannot differ by protocol. Deciding a proposal is not offered here:
that is a person, over REST."""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

from .collab import MAX_TEXT, CollabError, CollabService

PROTOCOL = "0.3.0"
_TASK_NOT_FOUND = -32001
_UNSUPPORTED = -32004
_CONTENT_TYPE = -32005
_NO_CARD = -32007


def agent_card(public_url: str, delegation: bool = False) -> dict:
    """Public, static, non-secret description of this service (A2A discovery document)."""
    base = public_url.rstrip("/")
    card = {
        "protocolVersion": PROTOCOL,
        "name": "HerdR Collaboration Network",
        "description": ("Post plain-text messages into HerdR collaboration rooms you have been "
                        "granted. Replies from other participants are read over MCP, not here."),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "version": "1",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"agentCredential": {
            "type": "apiKey", "in": "header", "name": "Authorization",
            "description": "'Agent <credential>' issued when a person connects the agent, plus an "
                           "X-Herdr-Device header with its device id. User tokens are refused."}},
        "security": [{"agentCredential": []}],
        "skills": [{"id": "room-post", "name": "Post to a room",
                    "description": "Send a text message to a room. Set the message contextId to the room id.",
                    "tags": ["collaboration", "chat"], "inputModes": ["text/plain"],
                    "outputModes": ["text/plain"]}],
        "supportsAuthenticatedExtendedCard": False,
    }
    if delegation:
        card["description"] += (" Coworker agents can also answer bounded tasks and propose artifacts "
                                "(quarantined until a person accepts them; never applied).")
        card["skills"].append({
            "id": "bounded-task", "name": "Answer a bounded task with an artifact proposal",
            "description": "tasks/list, tasks/get and tasks/cancel work on bounded tasks. message/send with the "
                           "message taskId and one data part {action: accept|decline|progress|complete|fail|cancel|propose, "
                           "expected_version, ...} answers one. A proposal is quarantined and never applied.",
            "tags": ["delegation", "proposal"], "inputModes": ["application/json"], "outputModes": ["application/json"]})
    return card


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


_STATE = {"pending": "submitted", "accepted": "working", "done": "completed", "failed": "failed",
          "declined": "rejected", "cancelled": "canceled", "expired": "failed"}


def _task(view: dict) -> dict:
    """A bounded task as an A2A Task. The proposals appear as artifact stubs (id, digest, state): the content
    the delegate itself wrote is never echoed back."""
    return {"kind": "task", "id": view["task_id"], "contextId": view["room_id"],
            "status": {"state": _STATE.get(view["status"], "unknown")},
            "artifacts": [{"artifactId": p["proposal_id"], "name": p["kind"],
                           "metadata": {"state": p["state"], "digest": p["digest"]}} for p in view["proposals"]],
            "metadata": {"herdr": {"version": view["version"], "text": view["text"], "bounds": view["bounds"],
                                   "usage": view["usage"], "status": view["status"]}}}


def _fail(rid: Any, exc: CollabError):
    data = {"status": exc.status, **({"code": exc.code} if exc.code else {})}
    return 200, _error(rid, -32000 if exc.status < 500 else -32603, exc.message, data)


def _delegation_send(service: CollabService, pr, rid: Any, msg: dict):
    parts = msg.get("parts")
    if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
            or parts[0].get("kind") != "data" or not isinstance(parts[0].get("data"), dict)):
        return 200, _error(rid, _CONTENT_TYPE, "a task answer is exactly one data part")
    data = dict(parts[0]["data"])
    action = data.pop("action", None)
    try:
        if action == "propose":
            out = service.submit_proposal(pr, msg["taskId"], data)
        elif isinstance(action, str):
            out = service.delegation_respond(pr, msg["taskId"], {"action": action, **data})
        else:
            return 200, _error(rid, -32602, "action is required")
        view = service.delegation_status(pr, msg["taskId"])
    except CollabError as exc:
        return _fail(rid, exc)
    task = _task(view)
    task["metadata"]["herdr"]["result"] = {k: v for k, v in out.items() if k != "event"}
    return 200, _result(rid, task)


def _send(service: CollabService, pr, rid: Any, params: Any):
    msg = params.get("message") if isinstance(params, dict) else None
    if not isinstance(msg, dict):
        return 200, _error(rid, -32602, "message is required")
    # A message with a taskId and a data part is a task answer, but only while delegation is switched on. Otherwise
    # (the default) this is the old text-only send and a taskId, which many A2A clients set on follow-ups, is ignored.
    parts_ = msg.get("parts")
    if (service.delegation_enabled and msg.get("taskId") is not None and isinstance(parts_, list)
            and any(isinstance(p, dict) and p.get("kind") == "data" for p in parts_)):
        if not isinstance(msg["taskId"], str):
            return 200, _error(rid, -32602, "taskId must be a string")
        return _delegation_send(service, pr, rid, msg)
    room = msg.get("contextId")
    if not isinstance(room, str) or not room:
        return 200, _error(rid, -32602, "contextId must be the room id")
    parts = msg.get("parts")
    if not isinstance(parts, list) or not parts or len(parts) > 20:
        return 200, _error(rid, -32602, "parts must be a short non-empty list")
    texts = []
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") != "text" or not isinstance(part.get("text"), str):
            return 200, _error(rid, _CONTENT_TYPE, "only text parts are supported")
        texts.append(part["text"])
    text = "\n".join(texts)
    if len(text) > MAX_TEXT:
        return 200, _error(rid, -32602, "message is too long")
    mid = msg.get("messageId")
    if mid is not None and (not isinstance(mid, str) or not 0 < len(mid) <= 200):
        return 200, _error(rid, -32602, "invalid messageId")
    # Retries of the same A2A messageId are idempotent; the key is derived, never trusted raw.
    key = "a2a-" + (hashlib.sha256(mid.encode()).hexdigest()[:40] if mid else secrets.token_hex(12))
    body = {"text": text, "idempotency_key": key}
    try:
        _, out = service.post_message(pr, room, body)
    except CollabError as exc:
        return _fail(rid, exc)
    note = f"posted at sequence {out['event']['sequence']}" + (" (replayed)" if out.get("replayed") else "")
    return 200, _result(rid, {"kind": "message", "role": "agent", "messageId": secrets.token_hex(12),
                              "contextId": room, "parts": [{"kind": "text", "text": note}]})


def _tasks(service: CollabService, pr, rid: Any, method: str, params: Any):
    tid = params.get("id") if isinstance(params, dict) else None
    try:
        if method == "tasks/list":
            return 200, _result(rid, {"tasks": [_task(v) for v in service.delegations_inbox(pr)["delegations"]]})
        if not isinstance(tid, str):
            return 200, _error(rid, -32602, "id is required")
        if method == "tasks/cancel":
            service.delegation_respond(pr, tid, {"action": "cancel"})
        return 200, _result(rid, _task(service.delegation_status(pr, tid)))
    except CollabError as exc:
        if exc.status == 404:           # not yours, not a task, not an agent that takes tasks: one answer
            return 200, _error(rid, _TASK_NOT_FOUND, "no such task")
        return _fail(rid, exc)


def handle(service: CollabService, message: Any, authorization: str, device: str | None):
    """One JSON-RPC request in, (HTTP status, JSON) out."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return 400, _error(None, -32600, "invalid request")
    rid, method = message.get("id"), message.get("method")
    if rid is None or isinstance(rid, (dict, list, bool)):
        return 400, _error(None, -32600, "invalid request")   # A2A has no notifications
    try:
        pr = service.authenticate(authorization, device)
    except CollabError:
        return 401, _error(rid, -32001, "unauthorized")
    if not pr.is_agent:
        return 403, _error(rid, -32002, "use an agent credential, not a user token")
    if method == "message/send":
        return _send(service, pr, rid, message.get("params"))
    if method in ("tasks/get", "tasks/cancel", "tasks/list") and service.delegation_enabled:
        return _tasks(service, pr, rid, method, message.get("params"))
    if method in ("tasks/get", "tasks/cancel", "tasks/pushNotificationConfig/set",
                  "tasks/pushNotificationConfig/get"):
        return 200, _error(rid, _TASK_NOT_FOUND, "no such task: this service keeps no task state")
    if method in ("message/stream", "tasks/resubscribe"):
        return 200, _error(rid, _UNSUPPORTED, "streaming is not supported")
    if method == "agent/getAuthenticatedExtendedCard":
        return 200, _error(rid, _NO_CARD, "no authenticated extended card")
    return 200, _error(rid, -32601, "method not found")
