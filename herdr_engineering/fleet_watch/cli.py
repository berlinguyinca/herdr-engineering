"""`herdr-eng fleet-watch once --dry-run` (phase 2: read-only; off unless invoked)."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import socket
import sys
import time
from pathlib import Path

from . import core, events, logs, poll, service, state


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


def local_identity(home: Path | None = None) -> str:
    """Which fleet host this machine is: the host_id of its host.json, else its short hostname."""
    from .. import host_config as hc

    try:
        return hc._read_host_json(Path(home) if home is not None else Path.home())["host_id"]
    except (hc.HostConfigUnavailable, OSError, ValueError):
        return socket.gethostname().split(".")[0].lower()


def _resolve_local(requested: str) -> tuple[str | None, str | None]:
    """(local host or None for all-ssh, refusal text). Local means "run without ssh", so it must be true."""
    from ..control_plane.fleet_rollout import HOST_ORDER

    if requested in ("", "none"):
        return None, None
    identity = local_identity()
    if requested == "auto":
        return (identity if identity in HOST_ORDER else None), None
    if requested not in HOST_ORDER or requested != identity:
        return None, (f"--local-host {requested}: this is not this machine (identity: "
                      f"{identity if identity in HOST_ORDER else 'not a fleet host'}); refusing to run it locally")
    return requested, None


def _engine(args, directory: Path, local: str | None):
    """The runner for this invocation's transport/timeout options (shared by run, resume, abandon, loop)."""
    from . import run as runner

    timeouts = {**runner.DEFAULT_TIMEOUTS, "precheck": args.precheck_timeout, "apply": args.apply_timeout,
                "health": args.health_timeout}
    return runner.Runner(
        directory, events.Emitter(directory, client=_event_client()),
        runner.SshTransport(local_host=local, remote_bin=args.remote_bin, connect_timeout=args.connect_timeout),
        rollback_scope=args.rollback_scope, timeouts=timeouts, lock_path=Path(args.lock).expanduser())


def cmd_run(args) -> int:
    """`fleet-watch run`: plan like `once`, and with --apply execute the rollout serially (or resume one)."""
    repos = args.repo or list(core.DEFAULT_REPOS)
    local, refusal = _resolve_local(args.local_host) if args.apply else (None, None)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    try:
        report = core.run_once(repos, lock_path=args.lock, auth=poll.token())
    except state.WatcherBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if not args.apply:
        print(json.dumps(report, indent=1, sort_keys=True) if args.json else render(report))
        print("dry-run: nothing executed; pass --apply to roll out", file=sys.stderr)
        return 0
    if args.resume:
        print("note: run --resume is deprecated; use `herdr-eng fleet-watch resume [ROLLOUT_ID]`", file=sys.stderr)
    directory = state.state_dir()
    try:
        with state.single_instance(directory):
            saved = state.load(directory)
            engine = _engine(args, directory, local)
            engine.settle_locks(saved)
            outcome = _drive(engine, saved, report["rollout"], args.resume)
    except state.WatcherBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps({"outcome": outcome}) if args.json else f"fleet-watch run: {outcome}")
    return {"succeeded": 0, "idle": 0, "halted": 1, "needs_resume": 4}[outcome]


_ROLLOUT_ID = re.compile(r"[0-9a-f]{16}")


def _pick_halted(saved: dict, rollout_id: str | None, verb: str) -> tuple[str | None, str | None]:
    """(rollout id, refusal text) for an operator command: the named rollout, or the only halted one."""
    rollouts = saved["rollouts"]
    if rollout_id:
        if not _ROLLOUT_ID.fullmatch(rollout_id) or rollout_id not in rollouts:
            return None, f"fleet-watch {verb}: no such rollout"
        if rollouts[rollout_id].get("status") != "halted":
            return None, f"fleet-watch {verb}: rollout {rollout_id} is not halted ({rollouts[rollout_id].get('status')})"
        return rollout_id, None
    halted = sorted(rid for rid, r in rollouts.items() if r.get("status") == "halted")
    if len(halted) == 1:
        return halted[0], None
    if not halted:
        return None, f"fleet-watch {verb}: no halted rollout"
    return None, f"fleet-watch {verb}: several halted rollouts, name one: {' '.join(halted)}"


def cmd_resume(args) -> int:
    """`fleet-watch resume [ID]`: re-run a halted rollout (operator decision, event reason `operator`)."""
    local, refusal = _resolve_local(args.local_host)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    directory = state.state_dir()
    try:
        with state.single_instance(directory):
            saved = state.load(directory)
            rid, refusal = _pick_halted(saved, args.rollout_id, "resume")
            if refusal:
                print(refusal, file=sys.stderr)
                return 2
            engine = _engine(args, directory, local)
            outcome = engine.resume(saved, rid, operator=True)
            engine.settle_locks(saved)
    except state.WatcherBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps({"outcome": outcome, "rollout_id": rid}) if args.json else f"fleet-watch resume {rid}: {outcome}")
    return {"succeeded": 0, "halted": 1}[outcome]


def cmd_abandon(args) -> int:
    """`fleet-watch abandon [ID]`: give up a halted rollout; newer heads are unblocked (CLI only in v1)."""
    directory = state.state_dir()
    try:
        with state.single_instance(directory):
            saved = state.load(directory)
            rid, refusal = _pick_halted(saved, args.rollout_id, "abandon")
            if refusal:
                print(refusal, file=sys.stderr)
                return 2
            _engine(args, directory, None).abandon(saved, rid)
    except state.WatcherBusy as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(json.dumps({"outcome": "abandoned", "rollout_id": rid}) if args.json else
          f"fleet-watch abandon {rid}: abandoned")
    return 0


def _loop_pass(args, local: str | None) -> tuple[dict, str]:
    """One service pass: poll + plan; with --enable-rollouts also run (or continue) the rollout."""
    report = core.run_once(args.repo or list(core.DEFAULT_REPOS), lock_path=args.lock, auth=poll.token())
    rollout = report["rollout"]
    if not args.enable_rollouts:
        return report, f"plan only ({'rollout ' + rollout['rollout_id'] if rollout else 'nothing to roll out'})"
    directory = state.state_dir()
    with state.single_instance(directory):
        saved = state.load(directory)
        engine = _engine(args, directory, local)
        engine.settle_locks(saved)
        return report, _drive(engine, saved, rollout, False)


def _watchdog_markers(directory: Path) -> list[Path]:
    return [directory / name for name in ("state.json", "outbox.jsonl", "seq.json", "logs")]


def cmd_loop(args) -> int:
    """`fleet-watch loop`: the systemd service body (D11). Plans only unless --enable-rollouts is given."""
    local, refusal = _resolve_local(args.local_host) if args.enable_rollouts else (None, None)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    directory = state.state_dir()
    try:
        with state.single_instance(directory, name="loop.lock"):
            return _loop(args, directory, local)
    except state.WatcherBusy:
        print("fleet-watch loop: another loop is already running", file=sys.stderr)
        return 3


def _loop(args, directory: Path, local: str | None) -> int:
    def soft(elapsed: float) -> None:
        print(f"fleet-watch loop: pass running {int(elapsed)}s (soft limit {int(args.soft_limit)}s); "
              "still making progress, not interrupting", file=sys.stderr, flush=True)

    def hard(stalled: float) -> None:
        print(f"fleet-watch loop: no progress for {int(stalled)}s (hard limit {int(args.hard_limit)}s); "
              "exiting 70, the service restarts and resumes the durable rollout", file=sys.stderr, flush=True)
        os._exit(70)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    rng = random.Random()
    dog = service.Watchdog(_watchdog_markers(directory), soft_s=args.soft_limit, hard_s=args.hard_limit,
                           on_soft=soft, on_hard=hard)
    dog.start()
    passes = 0
    try:
        while True:
            passes += 1
            report: dict = {"repos": {}}
            try:
                with dog.guard():
                    report, outcome = _loop_pass(args, local)
            except state.WatcherBusy:
                outcome = "busy"
            except Exception as exc:                       # a bad pass must not stop the service; the class is enough
                outcome = f"error {type(exc).__name__}"
            print(f"pass {passes}: {outcome}", flush=True)
            if args.max_passes and passes >= args.max_passes:
                return 0
            time.sleep(service.next_sleep(report, args.interval, rng))
    finally:
        dog.stop()


def _drive(engine, saved: dict, plan: dict | None, resume: bool) -> str:
    rollouts = saved["rollouts"]
    running = [rid for rid, r in rollouts.items() if r.get("status") == "running"]
    if running:                                           # a crashed run: continue where it stopped
        return engine.resume(saved, running[0])
    halted = [rid for rid, r in rollouts.items() if r.get("status") == "halted"]
    if plan and plan["rollout_id"] in halted:
        if not resume:
            print(f"rollout {plan['rollout_id']} halted earlier; fix the cause, then `fleet-watch resume` "
                  "(or `fleet-watch abandon`); --resume is its deprecated alias", file=sys.stderr)
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


def _runner_options(p, hidden: bool = False) -> None:
    """Transport/timeout options shared by every command that builds a Runner."""
    h = (lambda text: argparse.SUPPRESS) if hidden else (lambda text: text)
    p.add_argument("--local-host", default="auto",
                   help=h("host applied without ssh: 'auto' (default) = this machine if its host.json/hostname is a "
                          "fleet host; an explicit name must be this machine or the run is refused; 'none' = ssh all"))
    p.add_argument("--remote-bin", default="$HOME/.local/bin/herdr-eng",
                   help=h("installed herdr-eng on each host (expanded by the remote shell)"))
    p.add_argument("--rollback-scope", choices=("failed_host", "all_updated"), default="failed_host",
                   help=h("D8 rollback scope"))
    p.add_argument("--precheck-timeout", type=float, default=60.0, help=h("per-call timeout, seconds"))
    p.add_argument("--apply-timeout", type=float, default=1800.0, help=h("per-call timeout, seconds"))
    p.add_argument("--health-timeout", type=float, default=120.0, help=h("per-call timeout, seconds"))
    p.add_argument("--connect-timeout", type=int, default=15, help=h("ssh connect timeout, seconds"))


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
    ru.add_argument("--resume", action="store_true",
                    help="DEPRECATED alias: use `fleet-watch resume`; re-runs a halted rollout")
    _runner_options(ru)
    ru.set_defaults(func=cmd_run)
    lp = wsub.add_parser("loop", help="service body: poll, plan and (only with --enable-rollouts) roll out, forever")
    lp.add_argument("--enable-rollouts", action="store_true",
                    help="actually run rollouts; without it the loop only polls and plans")
    lp.add_argument("--repo", action="append", metavar="OWNER/NAME", help="watched repo (repeatable)")
    lp.add_argument("--lock", default=core.DEFAULT_LOCK, help="fleet.lock.yaml: baseline, moved after a success")
    lp.add_argument("--interval", type=int, default=None, metavar="SECONDS",
                    help="pause between passes (default: the poller's 120 s +-20 s cadence and backoff)")
    lp.add_argument("--max-passes", type=int, default=0, help="stop after N passes (default 0: run until stopped)")
    lp.add_argument("--soft-limit", type=float, default=service.SOFT_LIMIT_S,
                    help="warn when one pass has run this long (seconds); never interrupts")
    lp.add_argument("--hard-limit", type=float, default=service.HARD_LIMIT_S,
                    help="exit 70 after this long with NO progress (seconds); progress resets it")
    _runner_options(lp)
    lp.set_defaults(func=cmd_loop)
    rs = wsub.add_parser("resume", help="re-run a halted rollout after fixing the cause (operator decision)")
    rs.add_argument("rollout_id", nargs="?", help="16-hex rollout id (default: the only halted rollout)")
    rs.add_argument("--lock", default=core.DEFAULT_LOCK, help="fleet.lock.yaml moved if the rollout then succeeds")
    rs.add_argument("--json", action="store_true")
    _runner_options(rs)
    rs.set_defaults(func=cmd_resume)
    ab = wsub.add_parser("abandon", help="give up a halted rollout; newer heads are unblocked, nothing is changed")
    ab.add_argument("rollout_id", nargs="?", help="16-hex rollout id (default: the only halted rollout)")
    ab.add_argument("--lock", default=core.DEFAULT_LOCK, help=argparse.SUPPRESS)
    ab.add_argument("--json", action="store_true")
    _runner_options(ab, hidden=True)
    ab.set_defaults(func=cmd_abandon)
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
