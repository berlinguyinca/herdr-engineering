"""`herdr-eng corrections enable|disable|status|scan|list|forget`: the opt-in record of your corrections to agents."""
from __future__ import annotations

import argparse
import json
from typing import Any

from . import corrections as cx
from .session_history import AGENTS

SCHEMA_VERSION = 1


def _out(result: Any, ok: bool = True) -> int:
    print(json.dumps({"schema_version": SCHEMA_VERSION, "ok": ok, **({"result": result} if ok else {"error": result})},
                     indent=2, sort_keys=True))
    return 0 if ok else 1


def run(args: argparse.Namespace) -> int:
    log = cx.CorrectionLog.default()
    op = args.corrections_op
    if op == "enable":
        log.enable()
        return _out(log.stats())
    if op == "disable":
        log.disable()
        return _out(log.stats())
    if op == "status":
        return _out(log.stats())
    if op == "list":
        rows = log.read(agent=args.agent, kind=args.kind, session_id=args.session)
        return _out({"corrections": rows[-args.limit:], "total": len(rows)})
    if op == "unforget":
        return _out({"released": log.unforget()})
    if op == "forget":
        if not args.all and not args.session:
            return _out({"code": "invalid_request", "message": "name a --session, or pass --all"}, ok=False)
        return _out({"removed": log.forget(session_id=None if args.all else args.session, everything=args.all)})
    try:
        if args.recent:
            return _out(cx.scan_recent(log, agents=[args.agent] if args.agent else ("claude", "codex", "pi"),
                                       limit=args.recent))
        return _out(cx.scan_session(log, agent=args.agent, session=args.session, cwd=args.cwd))
    except PermissionError as error:
        return _out({"code": "disabled", "message": str(error)}, ok=False)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("corrections", help="opt-in record of when you corrected an agent (measures only, changes nothing)")
    ops = p.add_subparsers(dest="corrections_op", required=True)
    for name, text in (("enable", "start allowing scans"), ("disable", "stop allowing scans (kept records stay)"),
                       ("status", "counts by agent and kind")):
        ops.add_parser(name, help=text).set_defaults(func=run)
    c = ops.add_parser("scan", help="read one session (read-only) and record its corrections")
    c.add_argument("--agent", choices=AGENTS)
    c.add_argument("--session", help="session id or transcript path (default: the current directory's newest)")
    c.add_argument("--cwd")
    c.add_argument("--recent", type=int, metavar="N", help="scan each harness's newest N session records instead")
    c.set_defaults(func=run)
    c = ops.add_parser("list", help="show recorded corrections")
    c.add_argument("--agent", choices=AGENTS)
    c.add_argument("--kind", choices=cx.KINDS)
    c.add_argument("--session")
    c.add_argument("--limit", type=int, default=50)
    c.set_defaults(func=run)
    ops.add_parser("unforget", help="let forgotten sessions be recorded again").set_defaults(func=run)
    c = ops.add_parser("forget", help="delete recorded corrections (and keep them from coming back on the next scan)")
    c.add_argument("--session")
    c.add_argument("--all", action="store_true")
    c.set_defaults(func=run)
