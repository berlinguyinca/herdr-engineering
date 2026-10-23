"""External coworker / remote agents in an HCN room (T10): constants and pure helpers.

An external agent is an ordinary HCN agent record (same credential scheme, same ``grant ∩ sponsor role``
rule, same revocation) that is additionally:

* **bound to one room** at invite time (the room is never read from a request),
* **sponsored** by the room owner who invited it (that person is accountable for it),
* limited to the actions in :data:`EXTERNAL_ACTIONS` (never ``instruct``/``execute``, never a terminal),
* **short lived**: its credential has a hard expiry that rotation cannot extend,
* shown as a distinct principal kind (``external``) everywhere a viewer sees it.

Everything here is import-free of ``collab.py`` so both can use it. Text coming from an external agent is
untrusted: it is cleaned (control, bidi and invisible characters removed, length capped, secrets redacted)
here and escaped again by every renderer.
"""
from __future__ import annotations

import re

from .redact import redact_text

# What an external agent may ever hold. "read"/"post" are the room-feed rights; there is deliberately no
# terminal, task-giving ("instruct") or task-receiving ("execute") right (T11 adds bounded tasks).
EXTERNAL_ACTIONS = frozenset({"read", "post"})
DEFAULT_EXTERNAL_ACTIONS = ("read",)               # nothing is writable unless the owner ticks it

MAX_EXTERNALS_PER_ROOM = 25                         # live agents + open invites in one room
MAX_EXTERNALS_PER_OWNER = 50                        # across all rooms the owner sponsors
INVITE_TTL_DEFAULT = 3600.0
INVITE_TTL_MIN, INVITE_TTL_MAX = 60.0, 24 * 3600.0
CRED_TTL_DEFAULT = 24 * 3600.0
CRED_TTL_MIN, CRED_TTL_MAX = 300.0, 7 * 24 * 3600.0
INVITES_PER_HOUR = 10                               # per sponsoring owner

PRESENCE_STALE = 90.0                               # no heartbeat for this long = stale, not "live"
HEARTBEATS_PER_MINUTE = 12
STATUS_CHANGES_PER_10_MIN = 10                      # durable status events; the rest stay in memory
EXTERNAL_STATUSES = ("idle", "working", "waiting", "blocked", "done", "failed")
MAX_ACTIVITY = 140
MAX_LABEL = 40

LINEAGE_PER_MINUTE = 60
LINEAGE_MAX_EVENTS = 2000                           # per credential lifetime (in memory; the graph quota is the hard cap)
LINEAGE_NODE_TYPES = frozenset({"agent", "attempt", "session"})
LINEAGE_EDGE_TYPES = frozenset({"spawn"})
# Attrs an external agent may never set: they decide how the UI classifies or filters the node, or who else
# may write it. The server sets `external`, `reported_by` and `invited_by` itself.
RESERVED_ATTRS = frozenset({
    "external", "reported_by", "invited_by", "presentation", "workspace_id", "co_writers", "supersedes",
    "writer", "agent_id", "room_id", "mission_id", "terminal", "attach", "pane_id", "host"})
LINEAGE_BODY_FIELDS = frozenset({"event_id", "source_sequence", "event_type", "payload", "occurred_at"})

_INVISIBLE = re.compile(
    "[\x00-\x1f\x7f-\x9f"                 # C0, DEL, C1
    "­؜ᅟᅠ឴឵᠎"      # soft hyphen, Arabic letter mark, Hangul/Khmer fillers, Mongolian
    "​-‏ -‮⁠-⁯"           # zero width, bidi marks/overrides, line/para separators, isolates
    "ㅤ﻿ﾠ￹-￻]")
_SPACES = re.compile(r"\s+")


def clean_text(value: object, limit: int) -> str:
    """Display-safe text from an untrusted source: invisible and direction-changing characters removed,
    whitespace collapsed, secrets redacted, then truncated. Never raises."""
    if not isinstance(value, str):
        return ""
    text = _SPACES.sub(" ", _INVISIBLE.sub("", value)).strip()
    return redact_text(text)[:limit].strip()


def slug(label: str) -> str:
    """A handle-safe stem ([a-z0-9-], at most 16 chars) from an owner-written label."""
    out = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:16].strip("-")
    return out or "remote"


def owns_node(agent_id: str, node_id: str) -> bool:
    """``<kind>:<agent id>`` or ``<kind>:<agent id>.<anything>``: the only ids an external agent may write."""
    _, _, rest = node_id.partition(":")
    return rest == agent_id or (rest.startswith(agent_id + ".") and len(rest) > len(agent_id) + 1)


def namespace_of(node_id: str) -> str | None:
    """The agent id a lineage node id would belong to (``<kind>:<agent id>[.suffix]``)."""
    if not isinstance(node_id, str):
        return None
    _, _, rest = node_id.partition(":")
    return rest.split(".", 1)[0] or None
