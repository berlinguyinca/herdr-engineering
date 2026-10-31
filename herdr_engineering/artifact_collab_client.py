"""`herdr-eng artifacts collab list|grant|revoke|restrict` and `herdr-eng artifacts retract`.

Owner-side commands over the control plane's collaborator and retract routes. They use the same API-key client as
the share commands (key read from the environment variable *named* by --api-key-env, never printed). The server's
JSON is printed as is, also for HTTP error statuses: a failed retract carries ``restrict_all_offered`` and
``collab_*`` fields the operator needs. Any HTTP error status exits non-zero.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
from urllib.parse import quote

from .artifact_reports_client import ReportClientError
from .artifact_shares_client import REQUEST_TIMEOUT, ShareClient, _err, _strip_secrets

MIN_DAYS, MAX_DAYS, DEFAULT_DAYS = 1, 90, 30


class CollabClient(ShareClient):
    """ShareClient transport that hands back the JSON body of an error status instead of discarding it."""

    def call(self, method, path, body=None):
        """Return (status, parsed JSON or None). Transport failures raise ReportClientError."""
        from urllib.request import Request
        headers = {**self.headers, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=REQUEST_TIMEOUT) as response:
                status, raw = response.status, response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read(65536)
        except (OSError, ValueError):
            raise ReportClientError("control plane is unreachable") from None
        try:
            return status, _strip_secrets(json.loads(raw))
        except ValueError:
            return status, None


def _apath(aid, *segments, quoted=()):
    out = "/api/v1/artifacts/" + quote(aid, safe="")
    for seg in segments:
        out += "/" + (quote(seg, safe="") if seg in quoted else seg)
    return out


def _parse_days(text):
    try:
        days = int(text)
    except (TypeError, ValueError):
        return None
    return days if MIN_DAYS <= days <= MAX_DAYS else None


def _request_for(args):
    """(method, path, body) for the parsed command; None body means no JSON body."""
    aid = args.artifact_id
    if args.artifacts_command == "retract":
        body = {}
        if args.version_id:
            body["version_id"] = args.version_id
        if args.reason:
            body["reason"] = args.reason
        if args.restrict_all:
            body["restrict_all"] = True
        return "POST", _apath(aid, "retract"), body
    action = args.collab_action
    if action == "list":
        return "GET", _apath(aid, "collaborators"), None
    if action == "grant":
        return "POST", _apath(aid, "collaborators"), {
            "agent_id": args.agent_id, "permission": "write" if args.write else "read",
            "expires_in_days": _parse_days(args.days)}
    if action == "revoke":
        return "POST", _apath(aid, "collaborators", args.agent_id, "revoke", quoted=(args.agent_id,)), {}
    return "POST", _apath(aid, "collab-restrict"), {}


def run_command(args):
    command = getattr(args, "artifacts_command", None)
    if command == "collab" and args.collab_action == "grant" and _parse_days(args.days) is None:
        _err(f"--days must be an integer from {MIN_DAYS} to {MAX_DAYS}, got {args.days!r}")
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
        _err(f"HTTP {status}: {message[:300]}")
        return 1
    if result is None:
        _err("control plane did not return structured JSON")
        return 1
    return 0


def _common(parser):
    parser.add_argument("--control-plane")
    parser.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
    parser.add_argument("artifact_id")
    parser.set_defaults(func=run_command)


def add_collab_subparser(asub):
    collab = asub.add_parser("collab", help="owner: manage agent collaborators of an artifact")
    collab.set_defaults(artifacts_command="collab")
    actions = collab.add_subparsers(dest="collab_action", required=True)
    _common(actions.add_parser("list", help="show grants, pending invite grants and writability"))
    g = actions.add_parser("grant", help="grant an agent read (default) or write access that expires")
    _common(g)
    g.add_argument("agent_id")
    g.add_argument("--write", action="store_true", help="grant write permission (default: read)")
    g.add_argument("--days", default=str(DEFAULT_DAYS), help=f"grant lifetime in days, {MIN_DAYS}..{MAX_DAYS} (default {DEFAULT_DAYS})")
    r = actions.add_parser("revoke", help="revoke one agent's grant")
    _common(r)
    r.add_argument("agent_id")
    _common(actions.add_parser("restrict", help="operator action: revoke ALL collaborator grants of the artifact, "
                                                 "READ access included"))
    t = asub.add_parser("retract", help="hide an artifact (or one version) and revoke collaborator write access")
    t.set_defaults(artifacts_command="retract")
    _common(t)
    t.add_argument("--version-id", help="retract only this version")
    t.add_argument("--reason")
    t.add_argument("--restrict-all", action="store_true",
                   help="operator action: also revoke ALL collaborator grants, READ access included")
    return collab


def main(argv=None):
    parser = argparse.ArgumentParser(prog="herdr-eng artifacts")
    add_collab_subparser(parser.add_subparsers(dest="artifacts_command", required=True))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return run_command(args)
