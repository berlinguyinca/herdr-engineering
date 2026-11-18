#!/usr/bin/env python3
"""Make the Rakazo Rooms path work end to end, idempotently: two operator-owned collab agents and one room holding them.

Talks to the collab service's OPERATOR listener (``<url>/api/v1/collab/...``, acts as the operator, no token) with the
same routes ``HttpCollab`` uses: GET /me, /agents, /rooms, /rooms/{id}/agents, /rooms/{id}/events/tail;
POST /agents/connect, /projects, /projects/{id}/rooms, /rooms/{id}/agents, /rooms/{id}/messages.

Steps: wait for the service and the bridge -> ensure agents (create only when missing) -> ensure the room and its two
members (create only when missing) -> verify (room listed, nonce message read back, bridge ``groups/list`` when a bridge
token is supplied in HERDR_ROOMS_BRIDGE_TOKEN; the bridge signs in with the account's own password, nothing is invented
here). Prints PASS/FAIL/SKIP per check, exits 1 on any FAIL. Agent credentials returned by connect are discarded
unread, and anything that looks like a secret is redacted from output.

  rooms-bootstrap.py [--dry-run | --check-only] [--operator-url URL] [--bridge-url URL] [--wait SECONDS]
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

DEFAULT_OPERATOR_URL = "http://100.104.39.6:9482"
DEFAULT_BRIDGE_URL = "http://100.104.39.6"
DEFAULT_WAIT = 600.0
BASE = "/api/v1/collab"
ROOM_NAME = "tui-smoke"
AGENT_NAMES = ("tui-smoke-a", "tui-smoke-b")
PROJECT_NAME = "tui-smoke"
CHAT_ACTIONS = ["read", "post"]
GROUP_MEMBER_MIN = 2
_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
_SECRET = re.compile(r"(hca_[A-Za-z0-9_\-]{8,}|Bearer\s+\S+|(?:postgres(?:ql)?|mysql)://\S+|"
                     r"(?:password|passwd|token|secret|api[_-]?key|credential)\"?\s*[:=]\s*\"?[^\s\",}]+)", re.I)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def valid_agent_name(name: str) -> bool:
    return bool(_HANDLE.match(name))


def plan(agents: list[dict[str, Any]], rooms: list[dict[str, Any]], members: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What is missing. ``agents``/``rooms`` are the service's lists; ``members`` the room's agents (None: no room yet)."""
    live = {a["name"] for a in agents if a.get("status", "active") == "active"}
    room = next((r for r in rooms if r.get("name") == ROOM_NAME), None)
    have = {m["name"] for m in members or []}
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


def wait_for(label: str, probe: Callable[[], Any], seconds: float, interval: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            probe()
            return True
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() >= deadline:
                print(f"FAIL wait {label}: no answer within {seconds:.0f}s ({redact(str(exc))})")
                return False
            time.sleep(min(interval, max(0.1, deadline - time.monotonic())))


class Result:
    def __init__(self) -> None:
        self.failed = False

    def line(self, ok: bool | None, name: str, detail: str = "") -> None:
        tag = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        self.failed |= ok is False
        print(redact(f"{tag} {name}" + (f": {detail}" if detail else "")))


def ensure(api: Http, p: dict[str, Any], dry: bool) -> None:
    for name in p["agents"]:
        if dry:
            print(f"WOULD create agent {name}")
            continue
        info = api.call("POST", BASE + "/agents/connect", {"harness_type": "tui-smoke", "name": name})
        print(f"CREATED agent {info['name']} id={info['agent_id']}")           # the credential is dropped unread
    ids = {a["name"]: a["agent_id"] for a in api.call("GET", BASE + "/agents")["agents"] if a.get("status") == "active"}
    room = next((r for r in api.call("GET", BASE + "/rooms")["rooms"] if r["name"] == ROOM_NAME), None)
    if room is None:
        if dry:
            print(f"WOULD create project {PROJECT_NAME!r} and room {ROOM_NAME!r}")
        else:
            proj = api.call("POST", BASE + "/projects", {"name": PROJECT_NAME})
            room = api.call("POST", BASE + f"/projects/{proj['project_id']}/rooms", {"name": ROOM_NAME})
            print(f"CREATED room {ROOM_NAME} id={room['room_id']}")
    for name in p["members"]:
        if dry or room is None:
            print(f"WOULD add {name} to room {ROOM_NAME}")
            continue
        api.call("POST", BASE + f"/rooms/{room['room_id']}/agents", {"agent_id": ids[name], "actions": CHAT_ACTIONS})
        print(f"CREATED membership {name} -> {ROOM_NAME}")


def current(api: Http) -> tuple[dict[str, Any], dict[str, Any] | None]:
    agents = api.call("GET", BASE + "/agents")["agents"]
    rooms = api.call("GET", BASE + "/rooms")["rooms"]
    room = next((r for r in rooms if r["name"] == ROOM_NAME), None)
    members = api.call("GET", BASE + f"/rooms/{room['room_id']}/agents")["agents"] if room else None
    return plan(agents, rooms, members), room


def verify(api: Http, bridge_url: str, res: Result) -> None:
    try:
        p, room = current(api)
        res.line(room is not None and is_noop(p), "rooms list has tui-smoke with both agents",
                 f"id={room['room_id']}" if room else "room missing")
        if room is None or not is_noop(p):
            return
        nonce = "nonce-" + uuid.uuid4().hex
        api.call("POST", BASE + f"/rooms/{room['room_id']}/messages",
                 {"text": f"rooms-bootstrap smoke {nonce}", "idempotency_key": "rb-" + nonce})
        tail = api.call("GET", BASE + f"/rooms/{room['room_id']}/events/tail", query={"limit": 20})["events"]
        res.line(any(nonce in json.dumps(e) for e in tail), "message posted and read back from room events", nonce)
    except RuntimeError as exc:
        res.line(False, "collab verify", str(exc))
    token = os.environ.get("HERDR_ROOMS_BRIDGE_TOKEN", "")
    if not token:
        res.line(None, "bridge groups/list", "set HERDR_ROOMS_BRIDGE_TOKEN (a signed-in Rakazo session value) to check")
        return
    req = urllib.request.Request(bridge_url.rstrip("/") + "/rpc/groups/list", data=b'{"json":{}}', method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with _OPENER.open(req, timeout=15) as resp:
            body = resp.read(4_000_000).decode()
        res.line(ROOM_NAME in body, "bridge groups/list returns tui-smoke")
    except Exception as exc:  # noqa: BLE001
        res.line(False, "bridge groups/list", type(exc).__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would be created; create nothing")
    mode.add_argument("--check-only", action="store_true", help="verify only; create nothing")
    ap.add_argument("--operator-url", default=os.environ.get("HERDR_COLLAB_OPERATOR_URL", DEFAULT_OPERATOR_URL))
    ap.add_argument("--bridge-url", default=os.environ.get("HERDR_ROOMS_BRIDGE_URL", DEFAULT_BRIDGE_URL))
    ap.add_argument("--wait", type=float, default=DEFAULT_WAIT, help="seconds to wait for the services (default 600)")
    a = ap.parse_args(argv)
    api, bridge, res = Http(a.operator_url), Http(a.bridge_url), Result()
    ok_collab = wait_for("collab /me", lambda: api.call("GET", BASE + "/me"), a.wait)
    ok_bridge = ok_collab and wait_for("bridge health", lambda: bridge.call("GET", "/api/v1/health"), a.wait)
    res.line(ok_collab, "collab operator listener answers /me")
    res.line(ok_bridge, "bridge health answers")
    if not (ok_collab and ok_bridge):
        return 1
    if not a.check_only:
        try:
            p, _ = current(api)
            print(f"PLAN missing agents={p['agents']} room={'yes' if p['room'] else 'no'} members={p['members']}")
            ensure(api, p, a.dry_run)
        except RuntimeError as exc:
            res.line(False, "ensure", str(exc))
            return 1
    if not a.dry_run:
        verify(api, a.bridge_url, res)
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
