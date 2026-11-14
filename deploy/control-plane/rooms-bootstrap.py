#!/usr/bin/env python3
"""Make the Rakazo Rooms path work end to end, idempotently: two operator-owned collab agents and one room holding them.

Talks to the collab service's OPERATOR listener (``<url>/api/v1/collab/...``, acts as the operator, no token) with the
same routes ``HttpCollab`` uses: GET /me, /agents, /rooms, /rooms/{id}/agents, /rooms/{id}/events/tail;
POST /agents/connect, /projects, /projects/{id}/rooms, /rooms/{id}/agents, /rooms/{id}/messages.

Steps: wait for the service and the bridge (one shared deadline) -> ensure agents (create only when missing; an expired one of the operator's is renewed in place, keeping its id and grants)
-> ensure the room (a new project is always created with it: the service cannot list projects, so re-creating a
deleted room may leave an extra project) and its two members (create only when missing) -> verify (room listed, a smoke message POSTED
to the room with a nonce and read back, bridge ``groups/list`` when a bridge token is supplied in
HERDR_ROOMS_BRIDGE_TOKEN; the bridge signs in with the account's own password, nothing is invented here). ``--check-only``
creates no agents/rooms/memberships but still posts the smoke message; add ``--no-post`` to skip it (that check reports
SKIP). Prints PASS/FAIL/SKIP per check, exits 1 on any FAIL. Agent credentials returned by connect are discarded unread,
and anything that looks like a secret is redacted from output.

  rooms-bootstrap.py [--dry-run | --check-only] [--no-post] [--operator-url URL] [--bridge-url URL] [--wait SECONDS]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

SUMMARY = ("Make the Rakazo Rooms path work end to end, idempotently: two operator-owned collab agents and one room "
           "holding them. --check-only creates nothing but still posts a smoke message to the room unless --no-post.")
DEFAULT_OPERATOR_URL = "http://127.0.0.1:9482"
DEFAULT_BRIDGE_URL = "http://127.0.0.1"
DEFAULT_WAIT = 600.0
BASE = "/api/v1/collab"
ROOM_NAME = "tui-smoke"
AGENT_NAMES = ("tui-smoke-a", "tui-smoke-b")
PROJECT_NAME = "tui-smoke"
CHAT_ACTIONS = ["read", "post"]
GROUP_MEMBER_MIN = 2
_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
_SECRET = re.compile(
    r"((?:hca|hcj)_[A-Za-z0-9_\-]{8,}"
    r"|(?:Proxy-)?Authorization\"?\s*[:=]\s*(?:\"[^\"]*\"|[^\r\n\",}]+)"
    r"|(?:Set-)?Cookie\"?\s*[:=]\s*(?:\"[^\"]*\"|[^\r\n\"]+)"
    r"|\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]+"
    r"|\w+(?:\+\w+)?://[^\s:/@]*:[^\s@]+@\S+"
    r"|(?:password|passwd|token|secret|api[_-]?key|private[_-]?key|credential|session)[A-Za-z_-]*\"?\s*[:=]\s*(?:\"[^\"]*\"|[^\s\",}]+))", re.I)
CAUGHT = (RuntimeError, KeyError, ValueError, TypeError)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None                      # a 3xx is the answer; the bridge bearer token never follows it


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def valid_agent_name(name: str) -> bool:
    return bool(_HANDLE.match(name))


def is_active(agent: dict[str, Any], now: float | None = None) -> bool:
    """The one rule for a usable agent: status active and not past its expires_at (epoch seconds)."""
    expires = agent.get("expires_at")
    expired = isinstance(expires, (int, float)) and expires <= (time.time() if now is None else now)
    return agent.get("status", "active") == "active" and not expired


def grant_lapsed(member: dict[str, Any], now: float | None = None) -> bool:
    """A listed room grant whose expires_at has passed: the service drops it at its next housekeeping (up to 30s later), but
    it already gives no access, so it counts as absent."""
    expires = member.get("expires_at")
    return isinstance(expires, (int, float)) and expires <= (time.time() if now is None else now)


def plan(agents: list[dict[str, Any]], rooms: list[dict[str, Any]], members: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What is missing. ``agents``/``rooms`` are the service's lists; ``members`` the room's agents (None: no room yet)."""
    live = {a["name"] for a in agents if is_active(a)}
    live_ids = {a.get("agent_id") for a in agents if is_active(a)}
    room = next((r for r in rooms if r.get("name") == ROOM_NAME), None)
    have = {m["name"] for m in members or [] if m.get("agent_id") in live_ids and not grant_lapsed(m)}   # nor is a lapsed grant
    return {"agents": [n for n in AGENT_NAMES if n not in live], "room": room is None,
            "members": [n for n in AGENT_NAMES if n not in have]}


def is_noop(p: dict[str, Any]) -> bool:
    return not (p["agents"] or p["room"] or p["members"])


class Http:
    def __init__(self, base: str, timeout: float = 15.0):
        self.base, self.timeout = base.rstrip("/"), timeout

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, query: dict[str, Any] | None = None) -> Any:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                raw = resp.read(4_000_000)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {redact(exc.read(500).decode('utf-8', 'replace'))}") from None
        except Exception as exc:  # noqa: BLE001 - refused, timed out, DNS
            raise RuntimeError(f"{method} {path} unreachable: {type(exc).__name__}") from None
        return json.loads(raw) if raw else {}


def wait_for(label: str, probe: Callable[[], Any], deadline: float, interval: float = 5.0) -> bool:
    """Probe until it answers or the monotonic ``deadline`` passes (callers share one deadline)."""
    while True:
        try:
            probe()
            return True
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() >= deadline:
                print(f"FAIL wait {label}: no answer before the deadline ({redact(str(exc))})")
                return False
            time.sleep(min(interval, max(0.1, deadline - time.monotonic())))


class Result:
    def __init__(self) -> None:
        self.failed = False

    def line(self, ok: bool | None, name: str, detail: str = "") -> None:
        tag = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        self.failed |= ok is False
        print(redact(f"{tag} {name}" + (f": {detail}" if detail else "")))


def has_group(body: Any) -> bool:
    """True when the groups array of the bridge's groups/list JSON holds a group named exactly ROOM_NAME."""
    node = body.get("json", body) if isinstance(body, dict) else body
    groups = node.get("groups", []) if isinstance(node, dict) else node
    return isinstance(groups, list) and any(isinstance(g, dict) and g.get("name") == ROOM_NAME for g in groups)


def expired_agent(agents: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The operator's own agent of this handle that is active but past its expires_at: renewable, never to be replaced."""
    return next((a for a in agents if a.get("name") == name and a.get("status", "active") == "active" and not is_active(a)), None)


def ensure(api: Http, p: dict[str, Any], dry: bool) -> None:
    listed = api.call("GET", BASE + "/agents")["agents"]               # /agents lists the operator's own agents only
    for name in p["agents"]:
        old = expired_agent(listed, name)
        if dry:
            print(f"WOULD {'renew' if old else 'create'} agent {name}")
            continue
        if old:                          # keeps the id, name and room grants; the new credential in the reply is dropped unread
            try:
                api.call("POST", BASE + f"/agents/{old['agent_id']}/renew")
                print(f"RENEWED agent {name}")
                continue
            except RuntimeError as exc:
                print(redact(f"RENEW refused for {name}, creating a new agent instead: {exc}"))
        info = api.call("POST", BASE + "/agents/connect", {"harness_type": "tui-smoke", "name": name})
        print(f"CREATED agent {info['name']} id={info['agent_id']}")           # the credential is dropped unread
    mine = api.call("GET", BASE + "/agents")["agents"]
    ids = {a["name"]: a["agent_id"] for a in mine if is_active(a)}
    expired_ids = {a["agent_id"] for a in mine if not is_active(a)}
    rooms = api.call("GET", BASE + "/rooms")["rooms"]
    room = next((r for r in rooms if r["name"] == ROOM_NAME), None)
    if room is None:
        if dry:
            print(f"WOULD create project {PROJECT_NAME!r} and room {ROOM_NAME!r}")
        else:
            project_id = api.call("POST", BASE + "/projects", {"name": PROJECT_NAME})["project_id"]
            room = api.call("POST", BASE + f"/projects/{project_id}/rooms", {"name": ROOM_NAME})
            print(f"CREATED room {ROOM_NAME} id={room['room_id']}")
    removable = room and p["members"] and not dry
    stale = api.call("GET", BASE + f"/rooms/{room['room_id']}/agents")["agents"] if removable else []
    operator = api.call("GET", BASE + "/me").get("user_id") if removable else None
    if removable and not operator:       # an empty id would match every unowned grant: never remove on that
        raise RuntimeError("GET /me answered without a user id; refusing to remove any membership")
    refused: list[str] = []
    for name in p["members"]:
        if dry or room is None:
            print(f"WOULD add {name} to room {ROOM_NAME}")
            continue
        if any(m["agent_id"] == ids[name] and not grant_lapsed(m) for m in stale):    # a renewed agent kept its grant
            continue
        for m in stale:                  # only the operator's own grant to an expired agent of the operator's holds the handle
            if m["name"] == name and m.get("owner_user_id") == operator and m["agent_id"] in expired_ids:
                api.call("POST", BASE + f"/rooms/{room['room_id']}/agents/{m['agent_id']}/remove")
                print(f"REMOVED stale membership {name} (expired agent) from {ROOM_NAME}")
        try:
            api.call("POST", BASE + f"/rooms/{room['room_id']}/agents", {"agent_id": ids[name], "actions": CHAT_ACTIONS})
        except RuntimeError as exc:
            if "HTTP 409" not in str(exc):
                raise
            print(redact(f"CONFLICT membership {name} -> {ROOM_NAME}, continuing with the others: {exc}"))
            refused.append(name)
            continue
        print(f"CREATED membership {name} -> {ROOM_NAME}")
    if refused:                          # the others were still added; the run is not clean
        raise RuntimeError(f"membership refused with 409 for {refused}")


def current(api: Http) -> tuple[dict[str, Any], dict[str, Any] | None]:
    agents = api.call("GET", BASE + "/agents")["agents"]
    rooms = api.call("GET", BASE + "/rooms")["rooms"]
    room = next((r for r in rooms if r["name"] == ROOM_NAME), None)
    members = api.call("GET", BASE + f"/rooms/{room['room_id']}/agents")["agents"] if room else None
    return plan(agents, rooms, members), room


def verify(api: Http, bridge_url: str, res: Result, post: bool = True) -> None:
    try:
        p, room = current(api)
        res.line(room is not None and is_noop(p), "rooms list has tui-smoke with both agents",
                 "room missing" if room is None else
                 "ok" if is_noop(p) else f"missing or expired agents={p['agents']} members without a live agent={p['members']}")
        if room is None or not is_noop(p):
            res.line(None, "bridge groups/list", "skipped: room or its live members incomplete")
            return
        if not post:
            res.line(None, "message posted and read back from room events", "skipped by --no-post")
            return _verify_bridge(bridge_url, res)
        nonce = "nonce-" + uuid.uuid4().hex
        api.call("POST", BASE + f"/rooms/{room['room_id']}/messages",
                 {"text": f"rooms-bootstrap smoke {nonce}", "idempotency_key": "rb-" + nonce})
        tail = api.call("GET", BASE + f"/rooms/{room['room_id']}/events/tail", query={"limit": 20})["events"]
        res.line(any(nonce in json.dumps(e) for e in tail), "message posted and read back from room events", nonce)
    except CAUGHT as exc:
        res.line(False, "collab verify", f"{type(exc).__name__}: {exc}")
    _verify_bridge(bridge_url, res)


def _verify_bridge(bridge_url: str, res: Result) -> None:
    token = os.environ.get("HERDR_ROOMS_BRIDGE_TOKEN", "")
    if not token:
        res.line(None, "bridge groups/list", "set HERDR_ROOMS_BRIDGE_TOKEN (a signed-in Rakazo session value) to check")
        return
    req = urllib.request.Request(bridge_url.rstrip("/") + "/rpc/groups/list", data=b'{"json":{}}', method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with _OPENER.open(req, timeout=15) as resp:
            body = json.loads(resp.read(4_000_000))
        res.line(has_group(body), "bridge groups/list returns tui-smoke")
    except Exception as exc:  # noqa: BLE001
        res.line(False, "bridge groups/list", type(exc).__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=SUMMARY)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would be created; create nothing")
    mode.add_argument("--check-only", action="store_true", help="create no agents/rooms/memberships; verify only (still posts one smoke message unless --no-post)")
    ap.add_argument("--no-post", action="store_true", help="do not post the smoke message (that check reports SKIP)")
    ap.add_argument("--operator-url", default=os.environ.get("HERDR_COLLAB_OPERATOR_URL", DEFAULT_OPERATOR_URL))
    ap.add_argument("--bridge-url", default=os.environ.get("HERDR_ROOMS_BRIDGE_URL", DEFAULT_BRIDGE_URL))
    ap.add_argument("--wait", type=float, default=DEFAULT_WAIT, help="seconds to wait for the services (default 600)")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except CAUGHT as exc:
        print(redact(f"FAIL bootstrap: {type(exc).__name__}: {exc}"))
        return 1


def run(a: argparse.Namespace) -> int:
    api, bridge, res = Http(a.operator_url), Http(a.bridge_url), Result()
    deadline = time.monotonic() + a.wait
    ok_collab = wait_for("collab /me", lambda: api.call("GET", BASE + "/me"), deadline)
    ok_bridge = wait_for("bridge health", lambda: bridge.call("GET", "/api/v1/health"), deadline) if ok_collab else None
    res.line(ok_collab, "collab operator listener answers /me")
    res.line(ok_bridge, "bridge health answers", "" if ok_collab else "not probed: collab did not answer")
    if not (ok_collab and ok_bridge):
        return 1
    if not a.check_only:
        try:
            p, _ = current(api)
            print(f"PLAN missing agents={p['agents']} room_missing={'yes' if p['room'] else 'no'} members={p['members']}")
            ensure(api, p, a.dry_run)
        except CAUGHT as exc:
            res.line(False, "ensure", f"{type(exc).__name__}: {exc}")
            return 1
    if not a.dry_run:
        verify(api, a.bridge_url, res, post=not a.no_post)
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
