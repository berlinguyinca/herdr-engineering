"""`herdr-eng assistant create|list|inspect|link|unlink|archive|types|graph`: manage the person's assistants.

A thin client of the Personal Assistant API (/api/v1/assistants...). Identity and tenant come from the API key the
server maps to a principal, exactly as for `herdr-eng teams` and the dashboard; the CLI offers no user, tenant or
approval override. Output is the `assistant` group's versioned envelope. None of these verbs can grant anything:
a link is management only (docs/specs/assistant-management-20261031).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any
from urllib.parse import quote, urlencode

from .artifact_shares_client import ShareClient, ShareError, _safe

SCHEMA_VERSION = 1
BASE = "/api/v1/assistants"


class RefError(ValueError):
    pass


def _success(result: Any) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": True, "result": result}


def _failure(code: str, message: str, status: int | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if status is not None:
        error["status"] = status
    return {"schema_version": SCHEMA_VERSION, "ok": False, "error": error}


def _derived_key(*parts: str | None) -> str:
    """A repeat of the same command replays instead of duplicating."""
    return "cli-" + hashlib.sha256("\x1f".join(p or "" for p in parts).encode()).hexdigest()[:32]


def resolve_ref(assistants: list[dict], ref: str) -> dict:
    """An id, or an exact name that is unique among the caller's active assistants."""
    for a in assistants:
        if a.get("assistant_id") == ref:
            return a
    live = [a for a in assistants if a.get("enabled") is not False]
    named = [a for a in live if a.get("name") == ref] or [a for a in assistants if a.get("name") == ref]
    if len(named) == 1:
        return named[0]
    if not named:
        raise RefError(f"no assistant with id or name {_safe(ref)!r}")
    ids = ", ".join(sorted(a["assistant_id"] for a in named))
    raise RefError(f"name {_safe(ref)!r} is ambiguous; use one of these ids: {ids}")


def _list(client) -> list[dict]:
    rows = client.request(BASE).get("assistants")
    if not isinstance(rows, list):
        raise ShareError("control plane returned an invalid assistant list", 502)
    return [r for r in rows if isinstance(r, dict)]


def render_tree(graph: dict) -> str:
    """Indented text of the same projection the dashboard draws."""
    nodes = {n["id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and "id" in n}
    edges = [e for e in graph.get("edges", []) if isinstance(e, dict)]
    children: dict[str, list[str]] = {}
    for e in edges:
        if e.get("kind") in ("linked", "delegated"):
            children.setdefault(e["from"], []).append(e["to"])
    linked = {e["to"] for e in edges if e.get("kind") == "linked"}
    runs_on = {e["from"]: e["to"] for e in edges if e.get("kind") == "runs_on"}
    placed = {e["from"]: e["to"] for e in edges if e.get("kind") == "placed_on"}

    def label(n: dict) -> str:
        kind = n.get("kind")
        if kind == "assistant":
            where = nodes.get(placed.get(n["id"], ""), {}).get("name")
            bits = [n.get("type", "rakazo"), str(n.get("status", ""))]
            if where:
                bits.append(f"on {where}")
            if n.get("can_delegate"):
                bits.append("can delegate")
            todo = next((st for st in n.get("setup", []) if isinstance(st, dict) and st.get("state") != "done"), None)
            if todo:
                bits.append(f"next: {_safe(todo.get('detail'))}")
            return f"{_safe(n.get('name'))} ({', '.join(b for b in bits if b)})  {n['id']}"
        if kind == "delegation":
            target = nodes.get(runs_on.get(n["id"], ""), {}).get("name")
            return f"{n.get('status')}: {_safe(n.get('name'))}" + (f"  -> {_safe(target)}" if target else "")
        return _safe(n.get("name", n["id"]))

    lines = ["You"]

    def walk(node_id: str, depth: int) -> None:
        n = nodes.get(node_id)
        if n is None:
            return
        lines.append("  " * depth + "- " + label(n))
        for child in children.get(node_id, []):
            walk(child, depth + 1)

    for n in nodes.values():
        if n.get("kind") == "assistant" and n["id"] not in linked:
            walk(n["id"], 1)
    specialists = [n for n in nodes.values() if n.get("kind") == "specialist"]
    if specialists:
        lines.append("Specialists")
        for n in specialists:
            lines.append(f"  - {_safe(n.get('name'))}: {'ready' if n.get('bound') else 'not set up'}")
    hosts = [n for n in nodes.values() if n.get("kind") == "host"]
    if hosts:
        lines.append("Where agents run")
        for n in hosts:
            lines.append(f"  - {_safe(n.get('name'))} [{n.get('backend', 'host')}]: {n.get('status')}")
    return "\n".join(lines)


def run_command(args: argparse.Namespace) -> int:
    op = args.assistant_manage
    key = os.environ.get(args.api_key_env)
    try:
        from .host_config import resolve_control_plane
        client = ShareClient(resolve_control_plane(args.control_plane), api_key=key)
        tree = None
        if op == "types":
            result: Any = client.request("/api/v1/assistant-types")
        elif op == "list":
            rows = _list(client)
            if args.type:
                rows = [a for a in rows if a.get("type", "rakazo") == args.type]
            if args.parent:
                parent = resolve_ref(_list(client), args.parent)
                rows = [a for a in rows if a.get("parent_id") == parent["assistant_id"]]
            result = {"assistants": rows}
        elif op == "inspect":
            result = {"assistant": resolve_ref(_list(client), args.ref)}
        elif op == "create":
            rows = _list(client)
            parent_id = resolve_ref(rows, args.parent)["assistant_id"] if args.parent else None
            body = {"name": args.name, "type": args.type,
                    "idempotency_key": args.idempotency_key or _derived_key("create", args.name, args.type, parent_id)}
            if parent_id:
                body["parent_id"] = parent_id
            result = {"assistant": client.request(BASE, body)}
        elif op == "link":
            rows = _list(client)
            child, parent = resolve_ref(rows, args.child), resolve_ref(rows, args.parent)
            result = {"assistant": client.request(f"{BASE}/{quote(child['assistant_id'], safe='')}/link",
                                                  {"parent_id": parent["assistant_id"]})}
        elif op == "unlink":
            child = resolve_ref(_list(client), args.child)
            result = {"assistant": client.request(f"{BASE}/{quote(child['assistant_id'], safe='')}/unlink", {})}
        elif op == "archive":
            target = resolve_ref(_list(client), args.ref)
            client.request(f"{BASE}/{quote(target['assistant_id'], safe='')}", {"archive": True})
            result = {"archived": target["assistant_id"]}
        elif op == "place":
            query = urlencode({"type": args.type, **({"require": args.require} if args.require else {})})
            result = client.request(f"/api/v1/assistant-placement?{query}")
        elif op == "graph":
            result = client.request("/api/v1/assistant-graph")
            if args.tree:
                tree = render_tree(result)
        else:  # pragma: no cover - argparse constrains this
            raise RefError("unknown assistant operation")
        print(tree if tree is not None else json.dumps(_success(result), indent=2, sort_keys=True))
        return 0
    except ShareError as error:
        message = _safe(str(error))
        print(json.dumps(_failure("api_error", message.replace(key, "[redacted]") if key else message,
                                  getattr(error, "status", None)), indent=2, sort_keys=True))
        return 1
    except (RefError, OSError, ValueError, RuntimeError) as error:
        message = _safe(str(error))
        print(json.dumps(_failure("invalid_request", message.replace(key, "[redacted]") if key else message),
                         indent=2, sort_keys=True))
        return 1


def _common(parser: argparse.ArgumentParser, operation: str) -> None:
    parser.add_argument("--control-plane")
    parser.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
    parser.add_argument("--json", action="store_true", help="versioned JSON output (default)")
    parser.set_defaults(func=run_command, assistant_manage=operation)


def register_management(groups: argparse._SubParsersAction) -> None:
    c = groups.add_parser("types", help="list the assistant types this control plane accepts")
    _common(c, "types")
    c = groups.add_parser("list", help="list your assistants")
    c.add_argument("--type")
    c.add_argument("--parent", help="only the assistants linked under this assistant (id or name)")
    _common(c, "list")
    c = groups.add_parser("inspect", help="show one assistant")
    c.add_argument("ref", help="assistant id, or its exact name when unique")
    _common(c, "inspect")
    c = groups.add_parser("create", help="create an assistant of a type, optionally linked under a parent")
    c.add_argument("name")
    c.add_argument("--type", required=True, help="see `herdr-eng assistant types`")
    c.add_argument("--parent", help="parent assistant (id or name); must be a rakazo assistant")
    c.add_argument("--idempotency-key", help="default: derived from the command, so a repeat replays")
    _common(c, "create")
    c = groups.add_parser("link", help="associate an assistant with a parent (management only; grants nothing)")
    c.add_argument("child", help="assistant id, or its exact name when unique")
    c.add_argument("--parent", required=True)
    _common(c, "link")
    c = groups.add_parser("unlink", help="remove an assistant's parent link")
    c.add_argument("child")
    _common(c, "unlink")
    c = groups.add_parser("archive", help="archive an assistant (disables it and revokes its grants)")
    c.add_argument("ref")
    _common(c, "archive")
    c = groups.add_parser("place", help="show where a specialist type would run now, and why other places were rejected")
    c.add_argument("--type", required=True)
    c.add_argument("--require", help="extra capabilities, comma separated; can only add to the type's own")
    _common(c, "place")
    c = groups.add_parser("graph", help="your assistants, delegations and where they run")
    c.add_argument("--tree", action="store_true", help="indented text instead of JSON")
    _common(c, "graph")
