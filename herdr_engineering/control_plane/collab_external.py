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
import unicodedata

from .redact import redact_text

# What an external agent may ever hold. "read"/"post" are the room-feed rights; there is deliberately no
# terminal, task-giving ("instruct") or task-receiving ("execute") right (T11 adds bounded tasks).
EXTERNAL_ACTIONS = frozenset({"read", "post"})
DEFAULT_EXTERNAL_ACTIONS = ("read",)               # nothing is writable unless the owner ticks it

MAX_EXTERNALS_PER_ROOM = 25                         # live agents + open invites in one room
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

LINEAGE_PER_MINUTE = 60                             # a sliding window kept in memory (a restart forgets it)
LINEAGE_MAX_EVENTS = 2000                           # per credential, lifetime; the count survives a restart
# Graph objects live for ever and external agents are separate principals, so the per-principal graph quota
# does not bound them as a group. These do: lifetime counts of lineage writes per sponsor and per room.
LINEAGE_MAX_PER_SPONSOR = 5000
LINEAGE_MAX_PER_ROOM = 5000
USAGE_RESERVE_STEP = 25                             # a counter is made durable this far AHEAD of its value
ATTR_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}")      # same alphabet as ids; anything else is refused
LINEAGE_NODE_TYPES = frozenset({"agent", "attempt", "session"})
LINEAGE_EDGE_TYPES = frozenset({"spawn"})
# Attrs an external agent may never set: they decide how the UI classifies or filters the node, or who else
# may write it. The server sets `external`, `reported_by` and `invited_by` itself.
RESERVED_ATTRS = frozenset({
    "external", "reported_by", "invited_by", "presentation", "workspace_id", "co_writers", "supersedes",
    "writer", "agent_id", "room_id", "mission_id", "terminal", "attach", "pane_id", "host"})
LINEAGE_BODY_FIELDS = frozenset({"event_id", "source_sequence", "event_type", "payload", "occurred_at"})

_INVISIBLE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"               # C0, DEL, C1
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e"   # soft hyphen, CGJ, Arabic mark, fillers, Mongolian
    "\u200b-\u200f\u2028-\u202e\u2060-\u206f"    # zero width (incl. ZWJ/ZWNJ), bidi, separators, isolates
    "\u2800\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffc"  # braille blank, fillers, variation selectors, object mark
    "\U0001d173-\U0001d17a"                # musical formatting characters
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef]")  # tag characters, variation selectors supplement
_SPACES = re.compile(r"\s+")
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _strip(value: str) -> str:
    # The explicit set catches what we know; the category sweep catches formatting, private-use,
    # surrogate and unassigned code points we did not think of.
    value = _INVISIBLE.sub("", value)
    return "".join(c for c in value if c in "\n\t" or unicodedata.category(c) not in _BAD_CATEGORIES)


def clean_text(value: object, limit: int, multiline: bool = False) -> str:
    """Display-safe text from an untrusted source: invisible and direction-changing characters removed
    (zero-width joiners too, so a label cannot hide a second one), whitespace collapsed, secrets
    redacted, then truncated. ``multiline`` keeps line breaks (chat). Never raises."""
    if not isinstance(value, str):
        return ""
    text = _strip(value)
    if multiline:
        text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.replace("\r", "").split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = _SPACES.sub(" ", text).strip()
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
