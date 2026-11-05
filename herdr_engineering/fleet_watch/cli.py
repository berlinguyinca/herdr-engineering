"""`herdr-eng fleet-watch once --dry-run` (phase 2: read-only; off unless invoked)."""

from __future__ import annotations

import json
import sys

from . import core, poll, state


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
