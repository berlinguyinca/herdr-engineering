"""`herdr-eng teams create|list|rename|archive`: the teams a person works with, in the Work Graph.

A team is created on purpose (an operator action) with a name, an optional project and an optional room.  The people in
the room are the team's members: they see the team, nobody else does.  Archiving keeps the team and its history; it only
leaves the default list.  The routes answer 404 until ``control_plane.unified_workspace.enabled`` is true.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote, urlencode

from .artifact_shares_client import ShareClient, ShareError, _safe

TEAMS = "/api/v1/graph/work/teams"


def _err(message):
    print(f"herdr-eng: {_safe(message, 400)}", file=sys.stderr)


def _line(team):
    return f"{_safe(team.get('native_id', '?'))}  {_safe(team.get('display_name', '?'))}  [{_safe(team.get('status', '?'))}]"


def _message(error):
    if error.status == 404:
        return "not found (the Work Graph is off, the team or room does not exist, or this key is not an operator key)"
    return str(error)


def run_command(args):
    action = args.teams_action
    key = os.environ.get(args.api_key_env)
    try:
        from .host_config import resolve_control_plane
        client = ShareClient(resolve_control_plane(args.control_plane), api_key=key)
        if action == "create":
            body = {"name": args.name}
            for field, value in (("project_id", args.project), ("room_id", args.room), ("idempotency_key", args.idempotency_key)):
                if value:
                    body[field] = value
            result = client.request(TEAMS, body)
            if args.json:
                print(json.dumps(result))
            else:
                verb = "Created" if result.get("created") else "Already exists:"
                print(f"{verb} {_line(result['team'])}")
                print(f"Open it at #/team-map?root=team:{_safe(quote(result['team'].get('native_id', ''), safe=''))}")
        elif action == "list":
            query = urlencode({k: v for k, v in (("status", args.status), ("limit", args.limit)) if v})
            result = client.request(TEAMS + ("?" + query if query else ""))
            if args.json:
                print(json.dumps(result))
            else:
                for team in result.get("items", []):
                    print(_line(team))
                if not result.get("items"):
                    print("No teams.")
                if result.get("has_more"):
                    print("More teams exist; narrow with --status or raise --limit.")
        else:
            path = f"{TEAMS}/{quote(args.team_id, safe='')}/{action}"
            result = client.request(path, {"name": args.name} if action == "rename" else {})
            if args.json:
                print(json.dumps(result))
            else:
                done = {"rename": "Renamed", "archive": "Archived"}[action] if result.get("changed") else "Unchanged:"
                print(f"{done} {_line(result['team'])}")
        return 0
    except ShareError as error:
        message = _message(error)
        _err(message.replace(key, "[redacted]") if key else message)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        message = str(error)
        _err(message.replace(key, "[redacted]") if key else message)
        return 1


def configure_parser(parser):
    commands = parser.add_subparsers(dest="teams_action", required=True)
    for action in ("create", "list", "rename", "archive"):
        c = commands.add_parser(action)
        c.add_argument("--control-plane")
        c.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
        c.add_argument("--json", action="store_true", help="print the server's JSON only")
        if action == "create":
            c.add_argument("name")
            c.add_argument("--project", help="link the team to this project id")
            c.add_argument("--room", help="link the team to this room id: the room's members are the team's members")
            c.add_argument("--idempotency-key", help="a second team with the same name needs a different key")
        elif action == "list":
            c.add_argument("--status", choices=("active", "archived", "all"), default=None)
            c.add_argument("--limit", type=int)
        else:
            c.add_argument("team_id")
            if action == "rename":
                c.add_argument("name")
        c.set_defaults(func=run_command)
    return parser


def add_teams_subparser(subparsers):
    return configure_parser(subparsers.add_parser("teams", help="your teams in the Work Graph (create, list, rename, archive)"))


def main(argv=None):
    parser = configure_parser(argparse.ArgumentParser(prog="herdr-eng teams"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return run_command(args)
