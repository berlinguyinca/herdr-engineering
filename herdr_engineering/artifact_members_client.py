"""`herdr-eng artifacts members list|show|links|limits|disable-sharing|enable-sharing|revoke-share|purge`.

Operator commands over ``/api/v1/artifact-members``. They use the API-key client of the share commands (the key is read
from the environment variable *named* by --api-key-env, never printed) and print the server's JSON as is, also for HTTP
error statuses. Any HTTP error status exits non-zero. ``purge`` removes the bytes of a deleted artifact for good, so it
needs ``--confirm`` naming exactly that artifact.
"""
from __future__ import annotations

import argparse
import json
import os
from urllib.parse import quote, urlencode

from .artifact_collab_client import CollabClient
from .artifact_reports_client import ReportClientError
from .artifact_shares_client import _err

LIMIT_FLAGS = (("max_artifact_bytes", "--max-artifact-bytes"), ("max_member_bytes", "--max-member-bytes"),
               ("max_artifacts", "--max-artifacts"))
BASE = "/api/v1/artifact-members"


def _limit(text: str):
    """A positive integer, or ``none`` to clear the override (back to the configured default)."""
    if text.lower() in {"none", "null"}:
        return None
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive integer or 'none'") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive integer or 'none'")
    return value


def _member(args) -> str:
    return BASE + "/" + quote(args.member, safe="")


def _request_for(args):
    """(method, path, body); a None body means no JSON body."""
    action = args.members_action
    if action == "list":
        return "GET", BASE, None
    if action == "show":
        return "GET", _member(args), None
    if action == "links":
        query = {k: v for k, v in (("audience", args.audience), ("state", args.state)) if v}
        return "GET", BASE + "/links" + ("?" + urlencode(query) if query else ""), None
    if action == "limits":
        body = {name: getattr(args, name) for name, _ in LIMIT_FLAGS if getattr(args, name) is not _UNSET}
        return "POST", _member(args) + "/limits", body
    if action in {"disable-sharing", "enable-sharing"}:
        return "POST", _member(args) + "/sharing", {"enabled": action == "enable-sharing"}
    if action == "revoke-share":
        return "POST", _member(args) + "/shares/" + quote(args.share_id, safe="") + "/revoke", {}
    return "POST", _member(args) + "/artifacts/" + quote(args.artifact_id, safe="") + "/purge", {"confirm": args.confirm}


_UNSET = object()


def run_command(args):
    action = args.members_action
    if action == "purge" and args.confirm != args.artifact_id:
        _err("purge is permanent: --confirm must repeat the artifact id exactly")
        return 2
    if action == "limits" and not any(getattr(args, name) is not _UNSET for name, _ in LIMIT_FLAGS):
        _err("give at least one of " + ", ".join(flag for _, flag in LIMIT_FLAGS))
        return 2
    key = os.environ.get(args.api_key_env)
    try:
        from .host_config import resolve_control_plane
        client = CollabClient(resolve_control_plane(args.control_plane), api_key=key)
        method, path, body = _request_for(args)
        status, result = client.call(method, path, body)
    except (ReportClientError, OSError, ValueError, RuntimeError) as error:
        message = str(error)
        _err(message.replace(key, "[redacted]") if key else message)
        return 1
    if result is not None:
        print(json.dumps(result))
    if status >= 400:
        message = result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), str) else "request failed"
        _err(f"HTTP {status}: {message[:300]}".replace(key, "[redacted]") if key else f"HTTP {status}: {message[:300]}")
        return 1
    if result is None:
        _err("control plane did not return structured JSON")
        return 1
    return 0


def _common(parser, *, member=True):
    parser.add_argument("--control-plane")
    parser.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
    if member:
        parser.add_argument("member", help="member user id (or member:<id>)")
    parser.set_defaults(func=run_command)
    return parser


def add_members_subparser(asub):
    members = asub.add_parser("members", help="operator: member-owned artifacts, usage, links and purge")
    members.set_defaults(artifacts_command="members")
    act = members.add_subparsers(dest="members_action", required=True)
    _common(act.add_parser("list", help="every member with usage and live link counts"), member=False)
    _common(act.add_parser("show", help="one member: usage, effective limits, artifacts and links"))
    p = _common(act.add_parser("links", help="every member-created link, across members"), member=False)
    p.add_argument("--audience", choices=("lab", "public"))
    p.add_argument("--state", choices=("pending", "live", "revoked"))
    p = _common(act.add_parser("limits", help="set per-member limits (positive integer, or 'none' to clear)"))
    for name, flag in LIMIT_FLAGS:
        p.add_argument(flag, dest=name, type=_limit, default=_UNSET, metavar="N|none")
    _common(act.add_parser("disable-sharing", help="stop the member creating links and revoke the ones they have"))
    _common(act.add_parser("enable-sharing", help="allow the member to create links again"))
    p = _common(act.add_parser("revoke-share", help="revoke one link the member created (lab or public)"))
    p.add_argument("share_id")
    p = _common(act.add_parser("purge", help="PERMANENT: remove a deleted artifact's bytes"))
    p.add_argument("artifact_id")
    p.add_argument("--confirm", required=True, metavar="ARTIFACT_ID", help="repeat the artifact id to confirm")
    return members


def main(argv=None):
    parser = argparse.ArgumentParser(prog="herdr-eng artifacts")
    add_members_subparser(parser.add_subparsers(dest="artifacts_command", required=True))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return run_command(args)
