"""`herdr-eng fleet-watch once --dry-run` (phase 2: read-only; off unless invoked)."""

from __future__ import annotations

import json
import sys

from . import core, events, logs, poll, state


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


def _event_client():
    """Control-plane client for live delivery from `run`: this host's host.json, else outbox-only (None)."""
    import argparse

    from .. import host_config as hc

    try:
        return _outbox_client(argparse.Namespace(api_key_file=None, control_plane=None))
    except (hc.HostConfigUnavailable, OSError, ValueError):
        return None


def cmd_run(args) -> int:
    """`fleet-watch run`: plan like `once`, and with --apply execute the rollout serially (or resume one)."""
    from . import run as runner

    repos = args.repo or list(core.DEFAULT_REPOS)
    try:
        report = core.run_once(repos, lock_path=args.lock, auth=poll.token())
    except state.WatcherBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if not args.apply:
        print(json.dumps(report, indent=1, sort_keys=True) if args.json else render(report))
        print("dry-run: nothing executed; pass --apply to roll out", file=sys.stderr)
        return 0
    local = None if args.local_host in ("", "none") else args.local_host
    timeouts = {**runner.DEFAULT_TIMEOUTS, "precheck": args.precheck_timeout, "apply": args.apply_timeout,
                "health": args.health_timeout}
    directory = state.state_dir()
    try:
        with state.single_instance(directory):
            saved = state.load(directory)
            engine = runner.Runner(
                directory, events.Emitter(directory, client=_event_client()),
                runner.SshTransport(local_host=local, remote_bin=args.remote_bin,
                                    connect_timeout=args.connect_timeout),
                rollback_scope=args.rollback_scope, timeouts=timeouts)
            outcome = _drive(engine, saved, report["rollout"], args.resume)
    except state.WatcherBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps({"outcome": outcome}) if args.json else f"fleet-watch run: {outcome}")
    return {"succeeded": 0, "idle": 0, "halted": 1, "needs_resume": 4}[outcome]


def _drive(engine, saved: dict, plan: dict | None, resume: bool) -> str:
    rollouts = saved["rollouts"]
    running = [rid for rid, r in rollouts.items() if r.get("status") == "running"]
    if running:                                           # a crashed run: continue where it stopped
        return engine.resume(saved, running[0])
    halted = [rid for rid, r in rollouts.items() if r.get("status") == "halted"]
    if plan and plan["rollout_id"] in halted:
        if not resume:
            print(f"rollout {plan['rollout_id']} halted earlier; fix the cause and pass --resume", file=sys.stderr)
            return "needs_resume"
        return engine.resume(saved, plan["rollout_id"], operator=True)
    if plan:
        for rid in halted:
            engine.supersede(saved, rid)
        return engine.start(saved, plan)
    if resume and halted:
        return engine.resume(saved, halted[-1], operator=True)
    print("plan: nothing to roll out", file=sys.stderr)
    return "idle"


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
        report = {"pending": len(pending), "rejected": rejected, "dropped": emitter.dropped(),
                  "directory": str(emitter.directory)}
        print(json.dumps(report) if args.json else
              f"outbox: {report['pending']} pending, {report['rejected']} parked, {report['dropped']} dropped "
              f"({report['directory']})")
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


def cmd_log(args) -> int:
    """Print a rollout log from THIS host's private store (events carry only its SHA-256, never the text)."""
    import hashlib

    store = logs.LogStore()
    try:
        kinds = [args.path_id] if args.path_id else store.kinds(args.rollout_id, args.host)
        if not kinds:
            raise logs.LogNotFound(f"no log for {args.host} in rollout {args.rollout_id}")
        for kind in kinds:
            raw = store.read_bytes(args.rollout_id, args.host, kind)
            print(f"== {args.host} {kind} sha256={hashlib.sha256(raw).hexdigest()}")     # hash the bytes, then decode
            text = raw.decode("utf-8", "replace")
            sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
    except logs.LogNotFound as exc:
        print(f"fleet-watch log show: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"fleet-watch log show: {exc}", file=sys.stderr)
        return 2
    return 0


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
    ru = wsub.add_parser("run", help="plan, then (with --apply) roll out serially beast -> bender -> macbook-pro")
    ru.add_argument("--apply", action="store_true", help="execute the rollout; without it this is a dry-run plan")
    ru.add_argument("--repo", action="append", metavar="OWNER/NAME", help="watched repo (repeatable)")
    ru.add_argument("--lock", default=core.DEFAULT_LOCK, help="fleet.lock.yaml read as the baseline")
    ru.add_argument("--json", action="store_true")
    ru.add_argument("--resume", action="store_true", help="re-run a halted rollout (operator decision)")
    ru.add_argument("--local-host", default="beast", help="host this runs on, applied without ssh ('none' = ssh all)")
    ru.add_argument("--remote-bin", default="$HOME/.local/bin/herdr-eng",
                    help="installed herdr-eng on each host (expanded by the remote shell)")
    ru.add_argument("--rollback-scope", choices=("failed_host", "all_updated"), default="failed_host")
    ru.add_argument("--precheck-timeout", type=float, default=60.0)
    ru.add_argument("--apply-timeout", type=float, default=1800.0)
    ru.add_argument("--health-timeout", type=float, default=120.0)
    ru.add_argument("--connect-timeout", type=int, default=15)
    ru.set_defaults(func=cmd_run)
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
    lg = wsub.add_parser("log", help="show a rollout log kept on this host (never sent to the control plane)")
    lsub = lg.add_subparsers(dest="log_command", required=True)
    sh = lsub.add_parser("show", help="print the stored log(s) of one rollout and host")
    sh.add_argument("rollout_id", help="16-hex rollout id")
    sh.add_argument("--host", required=True, help="fleet host the log belongs to (beast, bender, macbook-pro)")
    sh.add_argument("--path-id", choices=sorted(__import__("herdr_engineering.control_plane.fleet_rollout",
                                                           fromlist=["LOG_KINDS"]).LOG_KINDS),
                    help="one log kind (default: all stored for that host)")
    sh.set_defaults(func=cmd_log)
