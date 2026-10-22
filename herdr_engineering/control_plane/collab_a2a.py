"""A2A (Agent2Agent, JSON-RPC binding) adapter for HCN rooms.

A thin, stateless translation layer, not a second source of truth: an A2A ``message/send``
becomes one room message through the same authorization as the REST API and MCP (agent
credential, room grant, per-agent limits). There is no task store; the room log is the only
record, so task methods answer with the protocol's standard "no such task / unsupported"
errors. Remote content is never echoed back: replies carry only our own sequence number.

Scope on purpose: send-only text. Reading a room stays on MCP/REST, where it is wrapped as
untrusted data. No files, URLs, push-notification callbacks or streaming exist here."""
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


def agent_card(public_url: str) -> dict:
    """Public, static, non-secret description of this service (A2A discovery document)."""
    base = public_url.rstrip("/")
    return {
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


def _result(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _send(service: CollabService, pr, rid: Any, params: Any):
    msg = params.get("message") if isinstance(params, dict) else None
    if not isinstance(msg, dict):
        return 200, _error(rid, -32602, "message is required")
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
        return 200, _error(rid, -32000 if exc.status < 500 else -32603, exc.message)
    note = f"posted at sequence {out['event']['sequence']}" + (" (replayed)" if out.get("replayed") else "")
    return 200, _result(rid, {"kind": "message", "role": "agent", "messageId": secrets.token_hex(12),
                              "contextId": room, "parts": [{"kind": "text", "text": note}]})


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
    if method in ("tasks/get", "tasks/cancel", "tasks/pushNotificationConfig/set",
                  "tasks/pushNotificationConfig/get"):
        return 200, _error(rid, _TASK_NOT_FOUND, "no such task: this service keeps no task state")
    if method in ("message/stream", "tasks/resubscribe"):
        return 200, _error(rid, _UNSUPPORTED, "streaming is not supported")
    if method == "agent/getAuthenticatedExtendedCard":
        return 200, _error(rid, _NO_CARD, "no authenticated extended card")
    return 200, _error(rid, -32601, "method not found")
