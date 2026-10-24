"""Terminal button: the exact command that attaches the VIEWER's terminal to an agent's pane.

The dashboard is browsed from any fabric machine, and the agent's HerdR server may live on
another host (e.g. beast). Nothing in a browser can open a terminal on the viewer's machine
(there is no in-browser terminal and no URL-scheme handler), and the server must never spawn
a terminal itself. So the server only *builds* the command, from the verified session record,
and the viewer runs it in their own terminal:

* on the agent's host:   ``herdr --session <s> agent attach <pane>``
* from any other host:   ``ssh -t <host> "bash -lc 'herdr --session <s> agent attach <pane>'"``

HerdR facts this relies on (herdr 0.9.1): ``--machine`` forwarding never forwards interactive
attachment and must not be combined with ``--session``, so the remote form is plain ssh;
``agent attach`` takes a pane id or agent name, not a ``term_...`` terminal id; and a
non-interactive ssh shell does not have ``~/.local/bin`` (where herdr lives) on PATH, hence
``bash -lc``. Every value is checked against a strict charset; a value that fails is refused
and never echoed back.
"""
from __future__ import annotations

import ipaddress
import re
import shlex

_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
_SERVER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PANE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,63}$")
_TERMINAL = re.compile(r"^term_[A-Za-z0-9_-]{1,128}$")  # same rule as the feed plugin
# Characters that need no quoting in either shell layer (no quote, $, `, \, !, space).
_SAFE = re.compile(r"^[A-Za-z0-9 ._:/-]+$")
# Same set as summary fix. herdr's own agent_status values (idle, done, working, blocked,
# unknown) are all live: "done" means the agent finished a turn and waits for input.
_CLOSED = {"closed", "complete", "completed", "finished", "failed", "error", "stopped",
           "cancelled", "canceled", "exited", "dead", "terminated"}


class AttachRefused(Exception):
    def __init__(self, status, error, reason):
        super().__init__(error)
        self.status = status
        self.body = {"error": error, "reason": reason}


def _viewer_host(peer, hosts):
    """host_id whose recorded tailnet IP is the request's peer, or None (unknown viewer)."""
    try:
        ip = ipaddress.ip_address(peer)
    except (TypeError, ValueError):
        return None
    ip = getattr(ip, "ipv4_mapped", None) or ip  # dual-stack listener: ::ffff:100.x.y.z
    if ip.is_loopback:  # a local proxy hides the real viewer; do not guess
        return None
    matches = []
    for host in hosts:
        try:
            if host.get("tailnet_ip") and ipaddress.ip_address(host["tailnet_ip"]) == ip:
                matches.append(host.get("host_id"))
        except ValueError:
            continue
    return matches[0] if len(matches) == 1 else None


def attach_commands(session, *, hosts=(), peer=None):
    """Return the attach commands for ``session`` or raise :class:`AttachRefused`."""
    status = str(session.get("status") or "").lower()
    fields = {key: session.get(key) for key in ("host_id", "herdr_session", "pane_id", "terminal_id")}
    if status in _CLOSED or not all(isinstance(v, str) and v for v in fields.values()):
        raise AttachRefused(409, "This agent is not running or has no live terminal to attach to.",
                            "not_live")
    if not (_HOST.fullmatch(fields["host_id"]) and _SERVER.fullmatch(fields["herdr_session"])
            and _PANE.fullmatch(fields["pane_id"]) and _TERMINAL.fullmatch(fields["terminal_id"])):
        raise AttachRefused(409, "This agent's recorded terminal identity is not safe to put in a "
                                 "command, so no attach command is offered.", "unsafe_identity")
    host, server, pane = fields["host_id"], fields["herdr_session"], fields["pane_id"]
    inner = shlex.join(["herdr", "--session", server, "agent", "attach", pane])
    if not _SAFE.fullmatch(inner):  # belt and braces: the regexes above already guarantee it
        raise AttachRefused(409, "unsafe command", "unsafe_identity")
    remote = f"ssh -t {host} \"bash -lc '{inner}'\""
    viewer = _viewer_host(peer, hosts)
    recommended = "local" if viewer == host else "remote"
    return {
        "session_id": session.get("session_id"),
        "host_id": host, "herdr_session": server, "pane_id": pane,
        "viewer_host": viewer, "recommended": recommended,
        "commands": [
            {"form": "remote", "label": f"From any other machine (ssh to {host})", "command": remote},
            {"form": "local", "label": f"On {host} itself", "command": inner},
        ],
        "note": ("Run it in a terminal on the machine you are browsing from. The command attaches "
                 "to the pane recorded for this agent; if the pane was moved or closed, refresh "
                 "the agent list first."),
    }
