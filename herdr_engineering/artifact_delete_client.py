"""`herdr-eng artifacts delete|restore`: the owner's bulk delete (tombstone or purge now) and restore.

Talks to ``POST /api/v1/artifacts/delete`` and ``/restore`` with the same API-key client as the other artifact commands
(the key comes from the environment variable *named* by ``--api-key-env``, never printed). Only an owner key is
accepted by the server; anyone else gets a plain "not found". One JSON object is printed per artifact id. Ids are sent
in batches of at most ``BATCH`` (the server's cap). ``--purge-now`` deletes bytes at once and needs ``--yes``; the
confirmation the server wants (the count of ids in the request) is derived from the ids given, so ``--yes`` is the
operator's explicit second step.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid

from .artifact_collab_client import CollabClient
from .artifact_reports_client import ReportClientError
from .artifact_shares_client import _err

BATCH = 200
DEFAULT_REASON = "deleted by the owner with herdr-eng"
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


def _batches(ids):
    return [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]


def _plan(args):
    """``[(path, body)]`` for the parsed command, or an error message."""
    ids = list(dict.fromkeys(args.artifact_ids))
    bad = [i for i in ids if not _ID.fullmatch(i)]
    if bad:
        return f"not an artifact id: {bad[0][:60]!r}"
    key = args.request_key or uuid.uuid4().hex
    if args.artifacts_command == "restore":
        return [("/api/v1/artifacts/restore", {"ids": chunk, "request_key": f"{key}-{n}",
                                               **({"override_legal_hold": True} if args.override_legal_hold else {})})
                for n, chunk in enumerate(_batches(ids))]
    if args.hold_days is not None and args.hold_days < 1:
        return "--hold-days must be at least 1; use --purge-now --yes to delete at once"
    if args.purge_now and not args.yes:
        return f"--purge-now deletes the bytes of {len(ids)} artifact(s) for good; repeat the command with --yes to confirm"
    if args.hold_days is not None and args.purge_now:
        return "--hold-days applies to a restorable delete, not to --purge-now"
    plan = []
    for n, chunk in enumerate(_batches(ids)):
        body = {"ids": chunk, "reason": args.reason or DEFAULT_REASON, "request_key": f"{key}-{n}",
                "mode": "purge_now" if args.purge_now else "tombstone"}
        if args.purge_now:
            body["confirm"] = str(len(chunk))
            if args.override_legal_hold:
                body["override_legal_hold"] = True
        elif args.hold_days is not None:
            body["hold_days"] = args.hold_days
        if args.override_legal_hold and not args.purge_now:
            body["override_legal_hold"] = True
        plan.append(("/api/v1/artifacts/delete", body))
    return plan


def run_command(args):
    plan = _plan(args)
    if isinstance(plan, str):
        _err(plan)
        return 2
    key = os.environ.get(args.api_key_env)
    refused = 0
    try:
        from .host_config import resolve_control_plane
        client = CollabClient(resolve_control_plane(args.control_plane), api_key=key)
        for path, body in plan:
            status, result = client.call("POST", path, body)
            if status >= 400 or not isinstance(result, dict) or not isinstance(result.get("results"), list):
                message = result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), str) else "request failed"
                _err(f"HTTP {status}: {message[:300]}")
                return 1
            for row in result["results"]:
                print(json.dumps(row))
                refused += row.get("result") == "refused"
    except (ReportClientError, OSError, ValueError, RuntimeError) as error:
        message = str(error)
        _err(message.replace(key, "[redacted]") if key else message)
        return 1
    if refused:
        print(f"{refused} artifact(s) were refused; see the JSON above", file=sys.stderr)
    return 0


def add_delete_subparsers(asub):
    def common(parser):
        parser.add_argument("--control-plane")
        parser.add_argument("--api-key-env", default="HERDR_API_KEY", help="Environment variable name, never the key itself")
        parser.add_argument("artifact_ids", nargs="+", metavar="artifact_id")
        parser.add_argument("--request-key", help="repeat a run with the same key to make it idempotent")
        parser.set_defaults(func=run_command)

    d = asub.add_parser("delete", help="owner: delete artifacts (restorable tombstone; --purge-now removes the bytes)")
    d.set_defaults(artifacts_command="delete")
    common(d)
    d.add_argument("--reason", help="why (kept in the audit trail)")
    d.add_argument("--hold-days", type=int, help="days a tombstone stays restorable before the worker purges it (default 30)")
    d.add_argument("--purge-now", action="store_true", help="skip the hold and delete the bytes now; needs --yes")
    d.add_argument("--yes", action="store_true", help="confirm --purge-now")
    d.add_argument("--override-legal-hold", action="store_true", help="act on artifacts under legal hold (re-delete, or with --purge-now purge)")
    r = asub.add_parser("restore", help="owner: restore tombstoned artifacts whose bytes are not yet purged")
    r.set_defaults(artifacts_command="restore", purge_now=False, yes=False, hold_days=None,
                   reason=None)
    common(r)
    r.add_argument("--override-legal-hold", action="store_true", help="restore artifacts under legal hold (recorded)")
