"""`herdr-eng fleet-watch once --dry-run` (phase 2: read-only; off unless invoked)."""

from __future__ import annotations

import json
import sys

from . import core, events, poll, state


def render(report: dict) -> str:
    lines = []
    for repo, info in report["repos"].items():
        extra = f" ({info['detail']})" if info.get("detail") else ""
        lines.append(f"{repo}: {info['outcome']} head={(info.get('head') or '-')[:12]}{extra}")
    for repo, info in report["refused"].items():
        lines.append(f"REFUSED {repo} {info['sha'][:12]}: {info['reason']}")
    rollout = report["rollout"]
    if rollout is None:
        lines.append("plan: nothing to roll out")
    else:
        lines.append(f"plan: rollout {rollout['rollout_id']} (dry-run, nothing executed)")
        for repo, sha in rollout["targets"].items():
            before = rollout["before"].get(repo)
            lines.append(f"  {repo}: {(before or 'unknown')[:12]} -> {sha[:12]}")
        lines.append("  hosts: " + " -> ".join(
            h["host"] + ("?" if h["optional"] else "") for h in rollout["hosts"]))
    return "\n".join(lines)


def cmd_fleet_watch(args) -> int:
    if not args.dry_run:
        print("fleet-watch is read-only in this release: pass --dry-run", file=sys.stderr)
        return 2
    try:
        report = core.run_once(args.repo or list(core.DEFAULT_REPOS), lock_path=args.lock,
                               auth=poll.token())
    except state.WatcherBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else render(report))
    return 0


def _outbox_client(args):
    """The control-plane client for `outbox flush`: explicit flags, else this host's host.json."""
    from pathlib import Path

    from .. import host_config as hc
    from ..service_metadata_feed import MetadataClient

    key = None
    if args.api_key_file:
        with Path(args.api_key_file).expanduser().open() as stream:
            key = stream.read(8193).strip()
        if len(key) > 8192:
            raise ValueError("invalid API key file")
    if key is None:
        try:
            key = hc.host_config().api_key
        except hc.HostConfigUnavailable:
            if not args.control_plane:
                raise
    return MetadataClient(hc.resolve_control_plane(args.control_plane), key)


def cmd_outbox(args) -> int:
    from .. import host_config as hc

    if args.outbox_command == "status":
        emitter = events.Emitter()
        pending = emitter.pending()
        rejected = emitter.rejected.read_text().count("\n") if emitter.rejected.exists() else 0
        report = {"pending": len(pending), "rejected": rejected, "directory": str(emitter.directory)}
        print(json.dumps(report) if args.json else
              f"outbox: {report['pending']} pending, {report['rejected']} parked ({report['directory']})")
        return 0
    try:
        emitter = events.Emitter(client=_outbox_client(args))
    except (hc.HostConfigUnavailable, OSError, ValueError) as exc:
        print(f"fleet-watch outbox flush: {exc}", file=sys.stderr)
        return 2
    report = emitter.flush()
    print(json.dumps(report) if args.json else
          f"outbox: sent {report['sent']}, pending {report['pending']}, parked {report['rejected']}"
          + (f" ({report['error']})" if report["error"] else ""))
    return 1 if report["error"] else 0


def register(sub) -> None:
    p = sub.add_parser("fleet-watch", help="merge-triggered fleet rollout watcher (read-only core)")
    wsub = p.add_subparsers(dest="fleet_watch_command", required=True)
    po = wsub.add_parser("once", help="poll watched repos once and print the planned rollout")
    po.add_argument("--dry-run", action="store_true",
                    help="required in this release: plan only, no ssh, no events, no lock write")
    po.add_argument("--repo", action="append", metavar="OWNER/NAME",
                    help="watched repo (repeatable; default: autospec, pi-engineering, herdr-engineering)")
    po.add_argument("--lock", default=core.DEFAULT_LOCK, help="fleet.lock.yaml read as the baseline")
    po.add_argument("--json", action="store_true")
    po.set_defaults(func=cmd_fleet_watch)
    ob = wsub.add_parser("outbox", help="inspect or deliver the queued rollout events (control-plane outage buffer)")
    osub = ob.add_subparsers(dest="outbox_command", required=True)
    st = osub.add_parser("status", help="count queued and parked events")
    st.add_argument("--json", action="store_true")
    fl = osub.add_parser("flush", help="deliver queued events to the control plane now (exit 1 if any stay queued)")
    fl.add_argument("--control-plane", default=None, help="control plane URL (default: host.json)")
    fl.add_argument("--api-key-file", default=None, help="read the ingest key from this file (default: host.json)")
    fl.add_argument("--json", action="store_true")
    for sp in (st, fl):
        sp.set_defaults(func=cmd_outbox)
