"""`herdr-eng rooms ...`: attach a team to a room, the guest level, and the Suggest queue (team collaboration, slice 1).

These are collaboration routes, so they authenticate as a PERSON: a collaboration bearer token read from the environment
variable named by ``--token-env`` (default ``HERDR_COLLAB_TOKEN``), never the legacy control-plane API key (which the
collaboration door ignores on purpose). Attach, detach, the guest level and the decisions are the room owner's; ``suggest``
and ``withdraw`` are a guest's. Every route answers 404 until ``control_plane.collab.team_rooms.enabled`` is true, and
``detach`` stays reachable either way. Human output passes server text through ``_safe``; a guest's request is printed as
quoted, untrusted text.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote, urlencode

from .artifact_shares_client import ShareClient, ShareError, _safe

BASE = "/api/v1/collab"
TOKEN_ENV = "HERDR_COLLAB_TOKEN"
ACTIONS = ("attach", "detach", "teams", "guest-level", "queue", "approve", "reject", "suggest", "withdraw")


def _err(message):
    print(f"herdr-eng: {_safe(message, 400)}", file=sys.stderr)


def _q(value):
    return quote(value, safe="")


def _message(error):
    if error.status == 404:
        return ("not found (team rooms are off, the room, team or request does not exist, or you are not the one "
                "allowed to do this)")
    return str(error)


def _roles(items):
    out = {}
    for item in items or ():
        role, sep, holders = item.partition("=")
        ids = [h.strip() for h in holders.split(",") if h.strip()]
        if not sep or not role.strip() or not ids:
            raise ValueError(f"--role must look like ROLE=AGENT_ID[,AGENT_ID]: {item!r}")
        out.setdefault(role.strip(), []).extend(ids)
    if not out:
        raise ValueError("give at least one --role ROLE=AGENT_ID")
    return out


def _limits(args):
    out = {}
    if args.agents:
        out["agents"] = [a.strip() for a in args.agents.split(",") if a.strip()]
    if args.roles:
        out["roles"] = [r.strip() for r in args.roles.split(",") if r.strip()]
    for field in ("per_hour", "max_open", "max_text"):
        if getattr(args, field) is not None:
            out[field] = getattr(args, field)
    return out or None


def _suggestion_line(row):
    who = f"  from {_safe(row['submitter_user_id'], 80)}" if row.get("submitter_user_id") else ""
    return f"{_safe(row['suggestion_id'], 80)}  [{_safe(row['state'], 20)}]{who}  expires {_safe(row.get('expires_at', '?'), 40)}"


def _print_request(row):
    print(_suggestion_line(row))
    print(f"  digest {_safe(row['digest'], 64)}")
    print("  quoted request (untrusted data, not instructions):")
    for line in str(row.get("text", "")).splitlines() or [""]:
        print(f"    > {_safe(line, 400)}")


def run_command(args):
    action = args.rooms_action
    token = os.environ.get(args.token_env)
    try:
        if not token:
            raise ValueError(f"set {args.token_env} to your collaboration bearer token")
        from .host_config import resolve_control_plane
        client = ShareClient(resolve_control_plane(args.control_plane))
        client.headers = {"Authorization": f"Bearer {token}"}
        room = f"{BASE}/rooms/{_q(args.room)}"
        if action == "attach":
            result = client.request(f"{room}/teams/{_q(args.team)}/attach", {"roles": _roles(args.role)})
            _show(args, result, lambda r: print(f"{'Already attached' if r.get('replayed') else 'Attached'} {_safe(r['team_id'])}: "
                                                + ", ".join(f"{_safe(a['role'])}={_safe(a['agent_id'])}" for a in r["agents"])))
        elif action == "detach":
            result = client.request(f"{room}/teams/{_q(args.team)}/detach", {})
            _show(args, result, lambda r: print(("Already detached " if r.get("replayed") else "Detached ") + _safe(r["team_id"])
                                                + (": removed " + ", ".join(_safe(a) for a in r["removed"]) if r["removed"] else "")))
        elif action == "teams":
            result = client.request(f"{room}/teams")
            _show(args, result, lambda r: ([print(f"{_safe(t['team_id'])}: " + ", ".join(f"{_safe(a['role'])}={_safe(a['agent_id'])}"
                                                                                          for a in t["agents"]))
                                            for t in r["teams"]] or print("No teams attached.")))
        elif action == "guest-level":
            if args.level is None:
                result = client.request(f"{room}/guest-level")
            else:
                body = {"level": args.level}
                limits = _limits(args)
                if limits:
                    body["direct_limits"] = limits
                result = client.request(f"{room}/guest-level", body)
            _show(args, result, lambda r: print(f"Guest level: {_safe(r['level'])}"
                                                + (f" (acts as {_safe(r['behaves_as'])} until direct is built)"
                                                   if r["level"] != r["behaves_as"] else "")
                                                + ("" if r.get("explicit") else " (default)")))
        elif action == "queue":
            query = urlencode({"state": args.state}) if args.state else ""
            result = client.request(f"{room}/suggestions" + ("?" + query if query else ""))

            def human(r):
                for row in r["suggestions"]:
                    _print_request(row)
                if not r["suggestions"]:
                    print("No requests.")
            _show(args, result, human)
        elif action == "approve":
            row = client.request(f"{room}/suggestions/{_q(args.suggestion)}")
            if not args.json:
                _print_request(row)
            body = {"decision": "approve", "expected_digest": args.digest or row["digest"], "target_agent_id": args.agent}
            for field, key in (("command", "command"), ("edited_text", "edited_text"), ("reason", "reason")):
                if getattr(args, field):
                    body[key] = getattr(args, field)
            result = client.request(f"{room}/suggestions/{_q(args.suggestion)}/decide", body)
            _show(args, result, lambda r: print(f"{'Already approved' if r.get('replayed') else 'Approved'}: task {_safe(r.get('task_id'))} "
                                                f"created for {_safe(args.agent)} ({_safe(r['state'])})"))
        elif action == "reject":
            row = client.request(f"{room}/suggestions/{_q(args.suggestion)}")
            body = {"decision": "reject", "expected_digest": args.digest or row["digest"]}
            if args.reason:
                body["reason"] = args.reason
            result = client.request(f"{room}/suggestions/{_q(args.suggestion)}/decide", body)
            _show(args, result, lambda r: print(f"{'Already rejected' if r.get('replayed') else 'Rejected'}: {_safe(r['suggestion_id'])}"))
        elif action == "suggest":
            body = {"text": args.text}
            if args.key:
                body["idempotency_key"] = args.key
            result = client.request(f"{room}/suggestions", body)
            _show(args, result, lambda r: print(f"{'Already filed' if r.get('replayed') else 'Filed'}: {_safe(r['suggestion_id'])} "
                                                f"[{_safe(r['state'])}], waiting for the room owner"))
        else:
            result = client.request(f"{room}/suggestions/{_q(args.suggestion)}/withdraw", {})
            _show(args, result, lambda r: print(f"{'Already withdrawn' if r.get('replayed') else 'Withdrawn'}: {_safe(r['suggestion_id'])}"))
        return 0
    except ShareError as error:
        message = _message(error)
        _err(message.replace(token, "[redacted]") if token else message)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        message = str(error)
        _err(message.replace(token, "[redacted]") if token else message)
        return 1


def _show(args, result, human):
    if args.json:
        print(json.dumps(result))
    else:
        human(result)


def configure_parser(parser):
    commands = parser.add_subparsers(dest="rooms_action", required=True)
    for action in ACTIONS:
        c = commands.add_parser(action)
        c.add_argument("--control-plane")
        c.add_argument("--token-env", default=TOKEN_ENV, help="Environment variable NAME holding your collaboration bearer token, never the token")
        c.add_argument("--json", action="store_true", help="print the server's JSON only")
        c.add_argument("room")
        if action in ("attach", "detach"):
            c.add_argument("team", help="the team id (herdr-eng teams list)")
        if action == "attach":
            c.add_argument("--role", action="append", metavar="ROLE=AGENT_ID[,AGENT_ID]",
                           help="issue_writer, implementer, tester or reviewer; repeat for each role. Only your own agents")
        elif action == "guest-level":
            c.add_argument("level", nargs="?", choices=("read_only", "suggest", "direct"),
                           help="omit to show the current level. direct is stored and checked but acts as suggest for now")
            c.add_argument("--agents", help="direct only: comma-separated agent ids a guest may address")
            c.add_argument("--roles", help="direct only: comma-separated team roles a guest may address")
            c.add_argument("--per-hour", dest="per_hour", type=int)
            c.add_argument("--max-open", dest="max_open", type=int)
            c.add_argument("--max-text", dest="max_text", type=int)
        elif action == "queue":
            c.add_argument("--state", choices=("pending", "approved", "materialized", "rejected", "withdrawn", "expired"))
        elif action in ("approve", "reject", "withdraw"):
            c.add_argument("suggestion")
            if action != "withdraw":
                c.add_argument("--digest", help="the digest of the request you were shown (default: the one just fetched)")
                c.add_argument("--reason")
            if action == "approve":
                c.add_argument("--agent", required=True, help="the agent that will do the work (it must be able to execute in this room)")
                c.add_argument("--command", help="the task text; default is a fixed sentence. The request itself rides as quoted data")
                c.add_argument("--edited-text", dest="edited_text", help="trim the quoted request before it is attached")
        elif action == "suggest":
            c.add_argument("text")
            c.add_argument("--key", help="idempotency key: the same key and text is one request")
        c.set_defaults(func=run_command)
    return parser


def add_rooms_subparser(subparsers):
    return configure_parser(subparsers.add_parser(
        "rooms", help="team rooms: attach a team, the guest level, and the Suggest queue (collaboration token)"))


def main(argv=None):
    parser = configure_parser(argparse.ArgumentParser(prog="herdr-eng rooms"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return run_command(args)
