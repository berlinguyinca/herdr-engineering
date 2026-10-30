"""`herdr-eng artifacts show|versions|for`: read-only browsing of the versioned artifact registry (P03 `/artifact` adapter)."""
import argparse
import json
import os
from urllib.parse import quote, urlencode

from .artifact_shares_client import ShareClient, ShareError, _err, _safe


def _show_path(artifact_id, version_id=None):
    out = "/api/v1/artifacts/" + quote(artifact_id, safe="")
    return out + "/versions/" + quote(version_id, safe="") if version_id else out


def run_command(args):
    action = args.browse_action
    if action == "for" and args.limit is not None and not 1 <= args.limit <= 200:
        _err("--limit must be 1..200")
        return 2
    try:
        from .host_config import resolve_control_plane
        client = ShareClient(resolve_control_plane(args.control_plane), api_key=os.environ.get(args.api_key_env))
        if action == "show":
            result = client.request(_show_path(args.artifact_id, args.version_id))
        elif action == "versions":
            result = client.request(_show_path(args.artifact_id) + "/versions?summary=true")
        else:
            query = {k: v for k, v in (("q", args.query), ("media_type", args.media_type),
                                       ("limit", args.limit), ("cursor", args.cursor)) if v is not None}
            path = f"/api/v1/artifacts/for/{quote(args.kind, safe='')}/{quote(args.ref, safe='')}"
            result = client.request(path + ("?" + urlencode(query) if query else ""))
    except ShareError as error:
        _err("not found, or you cannot read it" if error.status == 404 else _safe(str(error), 300))
        return 1
    print(json.dumps(result, indent=None if args.json else 2, sort_keys=True))
    return 0


def add_browse_subparsers(subparsers):
    """Register show/versions/for directly under `artifacts` so they read as `artifacts show <id>`."""
    for name in ("show", "versions", "for"):
        c = subparsers.add_parser(name)
        configure_parser_single(c, name)


def configure_parser_single(c, action):
    c.add_argument("--control-plane")
    c.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
    c.add_argument("--json", action="store_true", help="compact JSON on one line")
    if action == "for":
        c.add_argument("kind", help="context kind, e.g. project, issue, task, room")
        c.add_argument("ref")
        c.add_argument("--query")
        c.add_argument("--media-type")
        c.add_argument("--limit", type=int)
        c.add_argument("--cursor")
    else:
        c.add_argument("artifact_id")
    if action == "show":
        c.add_argument("version_id", nargs="?")
    c.set_defaults(func=run_command, browse_action=action)
