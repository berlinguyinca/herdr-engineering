"""Rakazo wire shapes for the Herdr assistant, and the per-thread event journal behind ``threads/subscribe``.

Nothing here stores assistant data. The assistant service owns threads, messages, runs and approvals; this module
only RENDERS them in the shapes the Rakazo mobile app reads (``packages/contracts`` of the upstream project) and keeps
a small in-memory journal that numbers what it has seen, because Rakazo clients resume a stream from a ``seq`` cursor.

Sequence numbers. Each thread's journal starts at ``base`` (the boot time in seconds times 1000) and counts up. A
journal is rebuilt from the store after a restart, so the numbers can differ from the previous process; the higher base
guarantees every rebuilt event is newer than any cursor a client kept, and events are keyed by message id, so the
client upserts rather than duplicates.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from .assistant import redact_secrets
from .rakazo_spaces import DEFAULT_SPACE_ID, DEFAULT_SPACE_NAME

SPACE_ID = DEFAULT_SPACE_ID            # kept for callers that mean the default space
SPACE_NAME = DEFAULT_SPACE_NAME
_AVATAR = "#4f46e5"
APPROVAL_PREFIX = "approval:"

# Assistant run state -> Rakazo RunStatus (packages/contracts/src/ids.ts)
_RUN_STATUS = {"submitted": "queued", "authorized": "queued", "queued": "queued", "running": "running",
               "recovering": "running", "waiting_for_approval": "waiting_input",
               "completed": "completed", "failed": "failed", "cancelled": "cancelled"}
ACTIVE = frozenset({"queued", "running", "waiting_input"})


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def run_status(state: str) -> str:
    return _RUN_STATUS.get(state, "running")


def snippet(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# --------------------------------------------------------------------------------------------- bots, spaces, me
def bot_view(assistant, thread_id: str, preview: str, status: str, space_id: str = SPACE_ID) -> dict[str, Any]:
    """An assistant as a Rakazo ``Bot`` (domain.ts BotSchema). ``threadId`` is mandatory in Rakazo."""
    stamp = iso(assistant.updated_at)
    return {
        "id": assistant.assistant_id, "spaceId": space_id, "name": assistant.name, "title": "",
        "description": "", "instructions": assistant.instructions, "color": _AVATAR, "avatarSet": "organic",
        "notifyOnFinish": False, "pinned": False, "sectionId": None, "archivedAt": None, "unread": False,
        "parentBotId": None, "memoryScope": None, "threadId": thread_id, "preview": preview, "status": status,
        "runErrorId": None, "computerMode": "team", "updatedAt": stamp, "createdAt": iso(assistant.created_at),
        "voiceId": None, "autoSpeak": False, "modelProvider": None, "modelId": None, "thinkingLevel": None,
        "teamChatAmbientEnabled": False, "teamChatRules": "", "crossSpaceCommunication": False,
        "fleetManagement": False, "disabledBuiltinTools": [], "webhookConfigured": False, "spawnKey": None,
        "incomingCall": None,
    }


_SPACE_BOT_KEYS = ("id", "spaceId", "name", "title", "color", "avatarSet", "notifyOnFinish", "pinned", "sectionId",
                   "unread", "parentBotId", "preview", "status", "runErrorId", "updatedAt")


def space_bot_view(bot: dict[str, Any]) -> dict[str, Any]:
    return {k: bot[k] for k in _SPACE_BOT_KEYS}


def space_view(space_id: str, name: str, bots: list[dict[str, Any]], *, owned: bool) -> dict[str, Any]:
    """One entry of ``spaces/list`` (domain.ts SpaceSchema). Only a space the account created can be renamed or deleted."""
    return {"id": space_id, "name": name, "isDefault": not owned, "hasContent": bool(bots), "canRename": owned,
            "canDelete": owned, "botCount": len(bots), "bots": [space_bot_view(b) for b in bots], "groups": [],
            "externalConversations": [], "botSections": []}


def spaces_view(current: dict[str, Any], spaces: list[dict[str, Any]], bots: list[dict[str, Any]]) -> dict[str, Any]:
    """``spaces/list``: the active space in full (``current``) and every space of the account in short form."""
    return {"current": {"id": current["id"], "name": current["name"], "bots": bots, "groups": [],
                        "externalConversations": [], "botSections": []}, "spaces": spaces}


def me_view(account, space_id: str = SPACE_ID) -> dict[str, Any]:
    """``me`` (domain.ts MeSchema): every required field, with the features this bridge lacks switched off."""
    return {
        "ticketBoardEnabled": False, "phoneCallsEnabled": False, "navLinks": [],
        "userId": account.principal_id, "email": account.email, "name": account.email.split("@", 1)[0],
        "spaceId": space_id, "isDeploymentOwner": False, "isAdmin": False, "needsModel": False,
        "defaultProvider": None, "defaultModel": None, "hostCredentialProvider": None, "hostCredentialSource": None,
        "computerHost": None, "canChooseHostComputer": False, "sandboxProvider": "none", "mcpStdioEnabled": False,
        "avatarStyle": "robot", "timezone": None, "billingEnabled": False, "needsSignupSurvey": False,
        "paymentWalletProvider": None,
    }


# --------------------------------------------------------------------------------------------- messages, runs
def text_message(message, seq: int, assistant_id: str, thread_id: str) -> dict[str, Any]:
    """A stored message as a Rakazo ``ThreadMessage`` (events.ts). The assistant's reply is role ``bot``."""
    run_id = message.provenance if str(message.provenance).startswith("run_") else None
    out = {"id": message.message_id, "threadId": thread_id, "seq": seq,
           "role": "user" if message.role == "user" else "bot",
           "blocks": [{"kind": "text", "text": message.content}], "createdAt": iso(message.created_at)}
    if message.role != "user":
        out["botId"] = assistant_id
    if run_id:
        out["runId"] = run_id
    return out


def approval_message(approval, action, seq: int, assistant_id: str, thread_id: str) -> dict[str, Any]:
    """A pending or decided assistant approval as an ``ask`` card (``isApprovalAskBlock`` in the app's core)."""
    block: dict[str, Any] = {
        "kind": "ask", "text": f"Allow {action.tool_id} on {snippet(action.scope, 80)}?",
        "approvalEffectId": approval.approval_id,
        "actions": [{"id": "allow", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
        "status": "pending",
    }
    detail = redact_secrets(approval.dry_run or "")
    if detail:
        block["detail"] = snippet(detail, 1500)
    if approval.decision != "pending":
        block["status"] = "answered"
        block["answer"] = "allow" if approval.decision == "approved" else "deny"
    return {"id": APPROVAL_PREFIX + approval.approval_id, "threadId": thread_id, "seq": seq, "role": "bot",
            "blocks": [block], "botId": assistant_id, "runId": action.run_id,
            "createdAt": iso(approval.created_at)}


def run_view(run, bot_id: str) -> dict[str, Any]:
    """An assistant run as a Rakazo ``Run`` (domain.ts RunSchema)."""
    status = run_status(run.state)
    done = status in ("completed", "failed", "cancelled")
    return {"id": run.run_id, "userId": run.user_id, "botId": bot_id, "threadId": run.thread_id,
            "taskId": run.task_id, "status": status, "trigger": "user", "routineId": None,
            "modelProvider": None, "modelId": run.model_route or None,
            "error": (snippet(redact_secrets(run.error) or "", 300) or None) if status == "failed" else None,
            "startedAt": iso(run.created_at), "completedAt": iso(run.updated_at) if done else None,
            "createdAt": iso(run.created_at)}


# --------------------------------------------------------------------------------------------- the journal
class ThreadJournal:
    """What this process has numbered for one thread, in order."""

    def __init__(self, thread_id: str, assistant_id: str, space_id: str, base: int):
        self.thread_id, self.assistant_id, self.space_id = thread_id, assistant_id, space_id
        self.next_seq = base
        self.base = base
        self.events: list[dict[str, Any]] = []
        self.messages: dict[str, dict[str, Any]] = {}     # message id -> current view (carries its own seq)
        self._approval_sig: dict[str, str] = {}
        self._runs: dict[str, dict[str, bool]] = {}       # run id -> {"started", "waiting", "final"}

    @property
    def head(self) -> int:
        return self.next_seq - 1

    def _emit(self, kind: str, stamp: datetime | None, payload: dict[str, Any], run_id: str | None = None):
        seq = self.next_seq
        self.next_seq += 1
        event = {"id": f"{self.thread_id}:{seq}", "spaceId": self.space_id, "threadId": self.thread_id,
                 "botId": self.assistant_id, "seq": seq, "type": kind, "createdAt": iso(stamp) or "",
                 "payload": payload}
        if run_id:
            event["runId"] = run_id
        self.events.append(event)
        if len(self.events) > 2000:
            del self.events[:500]
        return event

    def _message_event(self, kind: str, view: dict[str, Any], stamp: datetime | None) -> None:
        payload = {"messageId": view["id"], "seq": view["seq"], "role": view["role"], "blocks": view["blocks"],
                   "botId": self.assistant_id}
        self._emit(kind, stamp, payload, view.get("runId"))

    def sync(self, messages: list, runs: list, approvals: list[tuple]) -> None:
        """Number whatever the store holds that this journal has not seen. ``approvals`` is [(approval, action)].

        New facts are applied in time order (a run starts before its reply; a reply precedes the run's end), so a
        rebuild after a restart and the live path produce the same sequence for the same store.
        """
        facts: list[tuple[Any, int, str, Callable[[], None]]] = []
        for m in messages:
            if m.visibility == "visible" and m.message_id not in self.messages:
                facts.append((m.created_at, 1, m.message_id, lambda m=m: self._new_message(m)))
        for run in runs:
            seen = self._runs.setdefault(run.run_id, {"started": False, "waiting": False, "final": False})
            status = run_status(run.state)
            if not seen["started"]:
                facts.append((run.created_at, 0, run.run_id, lambda r=run, s=seen: self._run_started(r, s)))
            if status == "waiting_input" and not seen["waiting"]:
                facts.append((run.updated_at, 2, run.run_id + ":w", lambda r=run, s=seen: self._run_waiting(r, s)))
            if status in ("completed", "failed", "cancelled") and not seen["final"]:
                facts.append((run.updated_at, 3, run.run_id + ":f", lambda r=run, s=seen: self._run_final(r, s)))
        for approval, action in approvals:
            key = approval.approval_id
            if key not in self._approval_sig:
                facts.append((approval.created_at, 2, key, lambda a=approval, x=action: self._new_approval(a, x)))
            elif self._approval_sig[key] != approval.decision:
                facts.append((approval.decision_at or approval.created_at, 2, key + ":d",
                              lambda a=approval, x=action: self._new_approval(a, x, update=True)))
        for _, _, _, apply in sorted(facts, key=lambda f: (f[0], f[1], f[2])):
            apply()

    def _new_message(self, m) -> None:
        view = text_message(m, self.next_seq, self.assistant_id, self.thread_id)
        self.messages[m.message_id] = view
        self._message_event("thread.message.created", view, m.created_at)

    def _new_approval(self, approval, action, update: bool = False) -> None:
        mid = APPROVAL_PREFIX + approval.approval_id
        old = self.messages.get(mid)
        seq = old["seq"] if old else self.next_seq
        view = approval_message(approval, action, seq, self.assistant_id, self.thread_id)
        self.messages[mid] = view
        self._approval_sig[approval.approval_id] = approval.decision
        self._message_event("thread.message.updated" if old else "thread.message.created", view,
                            approval.decision_at if old and approval.decision_at else approval.created_at)

    def _run_started(self, run, seen) -> None:
        seen["started"] = True
        self._emit("run.started", run.created_at, {"trigger": "user"}, run.run_id)

    def _run_waiting(self, run, seen) -> None:
        seen["waiting"] = True
        self._emit("run.waiting_input", run.updated_at, {"userId": run.user_id}, run.run_id)

    def _run_final(self, run, seen) -> None:
        seen["final"] = True
        status = run_status(run.state)
        payload: dict[str, Any] = {}
        if status == "failed":
            payload["error"] = snippet(redact_secrets(run.error) or "", 300) or "The run failed."
        self._emit("run." + status, run.updated_at, payload, run.run_id)

    # reads ------------------------------------------------------------------------------------------------
    def after(self, cursor: int) -> list[dict[str, Any]]:
        return [e for e in self.events if e["seq"] > cursor]

    def page(self, before: int | None = None, limit: int = 50) -> tuple[list[dict[str, Any]], int | None]:
        """The newest ``limit`` messages older than ``before``, oldest first, and the cursor for the page before."""
        ordered = sorted(self.messages.values(), key=lambda m: m["seq"])
        candidates = [m for m in ordered if before is None or m["seq"] < before]
        page = candidates[-limit:]
        older = page[0]["seq"] if page and len(candidates) > len(page) else None
        return page, older

    def user_message_after(self, known: set[str]) -> dict[str, Any] | None:
        fresh = [m for mid, m in self.messages.items() if mid not in known and m["role"] == "user"]
        return min(fresh, key=lambda m: m["seq"]) if fresh else None


class JournalRegistry:
    """One :class:`ThreadJournal` per thread, created on first use."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._base = int(clock()) * 1000
        self._journals: dict[str, ThreadJournal] = {}
        self.lock = threading.RLock()

    def get(self, thread_id: str, assistant_id: str, space_id: str = SPACE_ID) -> ThreadJournal:
        with self.lock:
            journal = self._journals.get(thread_id)
            if journal is None:
                journal = self._journals[thread_id] = ThreadJournal(thread_id, assistant_id, space_id, self._base)
            return journal


# --------------------------------------------------------------------------------------------- SSE framing
HEARTBEAT_FRAME = b'data: {"json":{"type":"heartbeat"}}\n\n'


def frame(event: dict[str, Any]) -> bytes:
    """One server-sent-event frame in the shape ``subscribeThread`` parses: ``data: {"json": <event>}`` + blank line."""
    return b"data: " + json.dumps({"json": event}, separators=(",", ":")).encode() + b"\n\n"


def event_stream(fetch: Callable[[int], list[dict[str, Any]]], cursor: int, *, poll_seconds: float = 0.5,
                 heartbeat_seconds: float = 15.0, max_seconds: float = 600.0,
                 should_stop: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> Iterator[bytes]:
    """Frames for one subscription: a heartbeat at once, every event newer than ``cursor``, a heartbeat whenever
    ``heartbeat_seconds`` pass quietly (the app reconnects after 45 s of silence), and a clean end after
    ``max_seconds`` so a client reconnects with its cursor instead of holding one connection for ever."""
    started = last = clock()
    yield HEARTBEAT_FRAME
    while not should_stop() and clock() - started < max_seconds:
        for event in fetch(cursor):
            cursor = max(cursor, event["seq"])
            yield frame(event)
            last = clock()
        if clock() - last >= heartbeat_seconds:
            yield HEARTBEAT_FRAME
            last = clock()
        sleep(poll_seconds)
