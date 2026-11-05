"""The owner-facing Slack ask: one row per message the hub posts that the owner can answer with a tap or a reply.

The row is the authority for a tap.  It names the one request it may decide, holds a one-time nonce, the exact channel
and message timestamp the hub posted, and an expiry; a tap that does not match all of them changes nothing.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

KINDS = ("action", "question", "summary", "deploy")
STATUSES = ("open", "decided", "expired")


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def new_ask_id() -> str:
    return "sa_" + secrets.token_urlsafe(12)


@dataclass
class SlackAsk:
    ask_id: str
    tenant_id: str
    user_id: str
    kind: str = "action"             # action | question | summary | deploy
    request_id: str = ""
    task_id: str = ""
    nonce: str = ""
    channel: str = ""
    ts: str = ""
    status: str = "open"             # open | decided | expired
    remember: bool = False
    answer: str | None = None
    created_at: datetime = _EPOCH
    expires_at: datetime = _EPOCH
    worker_id: str = ""              # the worker that asked (a question) or whose request this is (an action)
    key_id: str = ""                 # action asks: the key a remembered approval would earn a grant for
    activation: str = ""             # action asks tapped "Approve and remember": "" | "pending" (kept intent) | "done"
    question: str = ""               # question asks: what the worker asked (kept so a restart can still post it)
    options: list[str] = field(default_factory=list)
    context: str = ""
    last_activity: datetime = _EPOCH  # summary rows: when the worker's task last did anything (the idle timer's clock)
    final: bool = False               # summary rows: the worker said the task is finished (post at once)


def button_value(ask: SlackAsk) -> str:
    """The value carried by every button of the ask's message: the ask id and its nonce, nothing else."""
    return f"{ask.ask_id}:{ask.nonce}"
