"""``herdr-eng`` — command-line entry point for the integration layer.

Exposes the real implemented capabilities as subcommands. This is the single
console entry point declared in pyproject.toml (``herdr-engineering.cli:main``).
Every command is a thin, typed wrapper over a module in this package; nothing
here re-implements Herdr or another owner system.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .version import __version__


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="herdr-eng",
        description="Super HerdR / HerdR Engineering — thin curated integration layer over Herdr")
    parser.add_argument("--version", action="version",
                        version=f"herdr-eng {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # doctor ---------------------------------------------------------------
    p = sub.add_parser("doctor", help="capability/health checks (spec 0050/0170)")
    p.add_argument("--json", action="store_true", help="stable machine-readable output")
    p.set_defaults(func=_cmd_doctor)

    # powerpack ------------------------------------------------------------
    pp = sub.add_parser("powerpack", help="distribution & lifecycle (spec 0050)")
    pps = pp.add_subparsers(dest="powerpack_command", required=True)
    pps.add_parser("status", help="show lock, enabled plugins, snapshots").set_defaults(func=_cmd_pp_status)
    p = pps.add_parser("enable", help="enable a plugin policy")
    p.add_argument("plugin"); p.add_argument("--reason", default="")
    p.set_defaults(func=_cmd_pp_enable)
    p = pps.add_parser("disable", help="disable a plugin policy")
    p.add_argument("plugin"); p.add_argument("--reason", default="")
    p.set_defaults(func=_cmd_pp_disable)
    p = pps.add_parser("snapshot", help="snapshot lock+config for rollback")
    p.add_argument("tag"); p.set_defaults(func=_cmd_pp_snapshot)
    pps.add_parser("preflight", help="run update preflight (blocks incompatible)").set_defaults(func=_cmd_pp_preflight)
    p = pps.add_parser("rollback", help="restore prior known-good snapshot")
    p.add_argument("--snapshot", default=None)
    p.set_defaults(func=_cmd_pp_rollback)

    # naming ---------------------------------------------------------------
    p = sub.add_parser("name", help="derive canonical workspace identity (spec 0010)")
    p.add_argument("--user"); p.add_argument("--spec-id"); p.add_argument("--spec-title")
    p.add_argument("--issue-number"); p.add_argument("--issue-title")
    p.add_argument("--mission"); p.add_argument("--prompt")
    p.add_argument("--repo"); p.add_argument("--short-task")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_name)

    # machines / herdr -----------------------------------------------------
    p = sub.add_parser("machines", help="list saved Herdr machines (spec 0030)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_machines)
    p = sub.add_parser("workspaces", help="list Herdr workspaces/agents (spec 0030)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_workspaces)
    p = sub.add_parser("capabilities", help="Herdr capability negotiation (spec 0030)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_capabilities)

    # artifacts ------------------------------------------------------------
    p = sub.add_parser("artifacts", help="shared artifact workspace (spec 0080)")
    asub = p.add_subparsers(dest="artifacts_command", required=True)
    p2 = asub.add_parser("publish", help="publish bytes to the artifact workspace")
    p2.add_argument("--title", required=True); p2.add_argument("--kind", required=True)
    p2.add_argument("--project", required=True); p2.add_argument("--mission", required=True)
    p2.add_argument("--media-type"); p2.add_argument("--repo"); p2.add_argument("--worktree")
    p2.add_argument("--machine"); p2.add_argument("--revision"); p2.add_argument("--name")
    p2.add_argument("data", help="path to file to publish")
    p2.set_defaults(func=_cmd_art_publish)
    asub.add_parser("list", help="list stored artifacts").set_defaults(func=_cmd_art_list)
    p2 = asub.add_parser("retention", help="enforce retention policy")
    p2.set_defaults(func=_cmd_art_retention)

    # journal --------------------------------------------------------------
    p = sub.add_parser("journal", help="session journal & timeline (spec 0090)")
    jsub = p.add_subparsers(dest="journal_command", required=True)
    p2 = jsub.add_parser("ingest", help="ingest an ActivityEvent (JSON)")
    p2.add_argument("event_json"); p2.set_defaults(func=_cmd_journal_ingest)
    p2 = jsub.add_parser("timeline", help="newest-first timeline for a session")
    p2.add_argument("session_id"); p2.set_defaults(func=_cmd_journal_timeline)
    p2 = jsub.add_parser("summary", help="Started/Completed/Current/Next projection")
    p2.add_argument("session_id"); p2.set_defaults(func=_cmd_journal_summary)
    p2 = jsub.add_parser("search", help="search activity by correlation fields")
    p2.add_argument("--repo"); p2.add_argument("--worktree"); p2.add_argument("--branch")
    p2.add_argument("--machine"); p2.add_argument("--session"); p2.add_argument("--text")
    p2.set_defaults(func=_cmd_journal_search)

    # devfabric ------------------------------------------------------------
    p = sub.add_parser("devfabric", help="transparent dev service fabric (spec 0110)")
    dsub = p.add_subparsers(dest="devfabric_command", required=True)
    p2 = dsub.add_parser("register", help="register a dev service lease")
    p2.add_argument("--machine", required=True); p2.add_argument("--host", required=True)
    p2.add_argument("--port", type=int, required=True)
    p2.add_argument("--protocol", default="http")
    p2.add_argument("--session"); p2.add_argument("--repo"); p2.add_argument("--worktree")
    p2.add_argument("--process"); p2.add_argument("--label")
    p2.set_defaults(func=_cmd_dev_register)
    p2 = dsub.add_parser("heartbeat", help="renew a lease")
    p2.add_argument("lease_id"); p2.set_defaults(func=_cmd_dev_heartbeat)
    p2 = dsub.add_parser("close", help="close a lease")
    p2.add_argument("lease_id"); p2.set_defaults(func=_cmd_dev_close)
    dsub.add_parser("list", help="list active leases").set_defaults(func=_cmd_dev_list)
    p2 = dsub.add_parser("resolve", help="resolve a dev port to a lease")
    p2.add_argument("port", type=int); p2.set_defaults(func=_cmd_dev_resolve)
    dsub.add_parser("reconcile", help="expire stale leases").set_defaults(func=_cmd_dev_reconcile)

    # attention ------------------------------------------------------------
    p = sub.add_parser("attention", help="attention/notifications (spec 0160)")
    asub = p.add_subparsers(dest="attention_command", required=True)
    p2 = asub.add_parser("raise", help="raise an attention item")
    p2.add_argument("--kind", required=True); p2.add_argument("--severity", default="notice")
    p2.add_argument("--title", required=True); p2.add_argument("--summary", required=True)
    p2.add_argument("--dedupe-key"); p2.set_defaults(func=_cmd_att_raise)
    asub.add_parser("list", help="list open items").set_defaults(func=_cmd_att_list)
    p2 = asub.add_parser("ack", help="acknowledge an item")
    p2.add_argument("item_id"); p2.set_defaults(func=_cmd_att_ack)
    p2 = asub.add_parser("resolve", help="resolve an item")
    p2.add_argument("item_id"); p2.set_defaults(func=_cmd_att_resolve)

    # tests ----------------------------------------------------------------
    p = sub.add_parser("tests", help="unified test explorer (spec 0120)")
    tsub = p.add_subparsers(dest="tests_command", required=True)
    tsub.add_parser("adapters", help="list available runner adapters").set_defaults(func=_cmd_tests_adapters)
    p2 = tsub.add_parser("run", help="run tests via an adapter")
    p2.add_argument("adapter"); p2.add_argument("--repo", required=True)
    p2.add_argument("--worktree", required=True); p2.add_argument("--machine", required=True)
    p2.add_argument("--revision", default=""); p2.add_argument("--selector")
    p2.add_argument("--workdir"); p2.add_argument("--cancel", action="store_true")
    p2.set_defaults(func=_cmd_tests_run)

    # ci -------------------------------------------------------------------
    p = sub.add_parser("ci", help="Woodpecker/Pileated CI (spec 0130)")
    csub = p.add_subparsers(dest="ci_command", required=True)
    p2 = csub.add_parser("pipelines", help="list pipelines for a repo")
    p2.add_argument("--repo", required=True); p2.set_defaults(func=_cmd_ci_pipelines)

    # browser / review -----------------------------------------------------
    p = sub.add_parser("browser", help="browser preview (spec 0070)")
    p.add_argument("url"); p.add_argument("--viewport", default="desktop")
    p.add_argument("--project", default="preview"); p.add_argument("--mission", default="browser")
    p.add_argument("--machine"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_browser)
    p = sub.add_parser("review", help="Plannotator review integration (spec 0070)")
    rsub = p.add_subparsers(dest="review_command", required=True)
    p2 = rsub.add_parser("request", help="request a review")
    p2.add_argument("--repo"); p2.add_argument("--session"); p2.add_argument("--candidate")
    p2.add_argument("--evidence", action="append", default=[])
    p2.set_defaults(func=_cmd_review_request)
    p2 = rsub.add_parser("resolve", help="resolve a review")
    p2.add_argument("review_id"); p2.add_argument("decision")
    p2.add_argument("--comment"); p2.set_defaults(func=_cmd_review_resolve)

    # candidates -----------------------------------------------------------
    p = sub.add_parser("candidates", help="multi-agent worktree comparison (spec 0140)")
    csub = p.add_subparsers(dest="candidates_command", required=True)
    p2 = csub.add_parser("id", help="deterministic candidate id")
    p2.add_argument("--repo", required=True); p2.add_argument("--worktree", required=True)
    p2.add_argument("--branch", required=True); p2.set_defaults(func=_cmd_cand_id)

    # web ------------------------------------------------------------------
    p = sub.add_parser("web", help="private web/mobile control surface (spec 0060)")
    p.add_argument("--host", default=None); p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=_cmd_web)

    return parser


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------
def _cmd_doctor(args) -> int:
    from .doctor import doctor_main
    return doctor_main(args)


def _cmd_pp_status(args) -> int:
    from .powerpack import PowerpackManager
    _print_json(PowerpackManager().status())
    return 0


def _cmd_pp_enable(args) -> int:
    from .powerpack import PowerpackManager
    PowerpackManager().enable(args.plugin, reason=args.reason)
    _print_json({"ok": True, "plugin": args.plugin, "enabled": True})
    return 0


def _cmd_pp_disable(args) -> int:
    from .powerpack import PowerpackManager
    ok = PowerpackManager().disable(args.plugin, reason=args.reason)
    _print_json({"ok": ok, "plugin": args.plugin, "enabled": False})
    return 0


def _cmd_pp_snapshot(args) -> int:
    from .powerpack import PowerpackManager
    snap = PowerpackManager().snapshot(args.tag)
    _print_json({"ok": True, "snapshot": str(snap)})
    return 0


def _cmd_pp_preflight(args) -> int:
    from .powerpack import PowerpackManager
    res = PowerpackManager().update_preflight()
    _print_json(res.to_dict())
    return 0 if res.ok else 1


def _cmd_pp_rollback(args) -> int:
    from .powerpack import PowerpackManager
    snap = PowerpackManager().rollback(args.snapshot)
    _print_json({"ok": True, "snapshot": str(snap)})
    return 0


def _cmd_name(args) -> int:
    from .naming import WorkspaceNamer, derive_workspace_identity
    ident = derive_workspace_identity(
        user_name=args.user, spec_id=args.spec_id, spec_title=args.spec_title,
        issue_number=args.issue_number, issue_title=args.issue_title,
        mission_title=args.mission, prompt=args.prompt,
        repository=args.repo, short_task=args.short_task)
    final = WorkspaceNamer().apply(ident)
    if args.json:
        _print_json(final.to_dict())
    else:
        print(final.name)
    return 0


def _cmd_machines(args) -> int:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        machines = NativeHerdrAdapter().list_machines()
    except Exception as exc:
        print(f"machines: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json([m.to_dict() for m in machines])
    else:
        for m in machines:
            print(f"{m.id:8} {m.label:24} {m.target}")
    return 0


def _cmd_workspaces(args) -> int:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        ws = NativeHerdrAdapter().list_workspaces()
    except Exception as exc:
        print(f"workspaces: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _print_json([w.to_dict() for w in ws])
    else:
        for w in ws:
            print(f"{w.workspace_id:12} {w.label}")
    return 0


def _cmd_capabilities(args) -> int:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        caps = NativeHerdrAdapter().capabilities(refresh=True)
    except Exception as exc:
        print(f"capabilities: {exc}", file=sys.stderr)
        return 1
    _print_json(caps.to_dict())
    return 0


def _cmd_art_publish(args) -> int:
    from pathlib import Path

    from .artifacts import FilesystemArtifactWorkspace
    data = Path(args.data).read_bytes()
    res = FilesystemArtifactWorkspace().publish_bytes(
        title=args.title, kind=args.kind, data=data, project=args.project,
        mission_or_session=args.mission, media_type=args.media_type,
        repository=args.repo, worktree_id=args.worktree, machine_id=args.machine,
        revision=args.revision, rel_name=args.name)
    _print_json({"ok": res.ok, "status": res.status, "error": res.error,
                 "artifact": res.artifact.to_dict() if res.artifact else None})
    return 0 if res.ok else 1


def _cmd_art_list(args) -> int:
    from .artifacts import FilesystemArtifactWorkspace
    ws = FilesystemArtifactWorkspace()
    _print_json({"root": str(ws.root), "available": ws.is_available(),
                 "files": ws._list("")})
    return 0


def _cmd_art_retention(args) -> int:
    from .artifacts import FilesystemArtifactWorkspace
    removed = FilesystemArtifactWorkspace().enforce_retention()
    _print_json({"ok": True, "removed": removed})
    return 0


def _cmd_journal_ingest(args) -> int:
    import json as _json

    from .contracts import ActivityEvent
    from .journal import SessionJournal
    ev = ActivityEvent(**_json.loads(args.event_json))
    new = SessionJournal().ingest(ev)
    _print_json({"ok": True, "newly_recorded": new})
    return 0


def _cmd_journal_timeline(args) -> int:
    from .journal import SessionJournal
    tl = SessionJournal().timeline(args.session_id)
    _print_json([e.to_dict() for e in tl])
    return 0


def _cmd_journal_summary(args) -> int:
    from .journal import SessionJournal
    _print_json(SessionJournal().summary(args.session_id).to_dict())
    return 0


def _cmd_journal_search(args) -> int:
    from .journal import SessionJournal
    events = SessionJournal().search(
        repository=args.repo, worktree_id=args.worktree, branch=args.branch,
        machine_id=args.machine, session_id=args.session, text=args.text)
    _print_json([e.to_dict() for e in events])
    return 0


def _cmd_dev_register(args) -> int:
    from .devfabric import DevServiceRegistry
    lease = DevServiceRegistry().register(
        target_machine_id=args.machine, target_host=args.host, target_port=args.port,
        protocol=args.protocol, owner_herdr_session_id=args.session,
        owner_repository=args.repo, owner_worktree_id=args.worktree,
        owner_process_id=args.process, label=args.label)
    _print_json(lease.to_dict())
    return 0


def _cmd_dev_heartbeat(args) -> int:
    from .devfabric import DevServiceRegistry
    lease = DevServiceRegistry().heartbeat(args.lease_id)
    _print_json({"ok": lease is not None,
                 "lease": lease.to_dict() if lease else None})
    return 0


def _cmd_dev_close(args) -> int:
    from .devfabric import DevServiceRegistry
    ok = DevServiceRegistry().close(args.lease_id)
    _print_json({"ok": ok})
    return 0


def _cmd_dev_list(args) -> int:
    from .devfabric import DevServiceRegistry
    _print_json([lease.to_dict() for lease in DevServiceRegistry().active_leases()])
    return 0


def _cmd_dev_resolve(args) -> int:
    from .devfabric import DevServiceRegistry
    lease = DevServiceRegistry().resolve_port(args.port)
    _print_json({"port": args.port, "lease": lease.to_dict() if lease else None})
    return 0


def _cmd_dev_reconcile(args) -> int:
    from .devfabric import DevServiceRegistry
    expired = DevServiceRegistry().reconcile()
    _print_json({"expired": expired})
    return 0


def _cmd_att_raise(args) -> int:
    from .attention import AttentionCenter
    item = AttentionCenter().raise_item(
        kind=args.kind, severity=args.severity, title=args.title,
        summary=args.summary, dedupe_key=args.dedupe_key)
    _print_json(item.to_dict())
    return 0


def _cmd_att_list(args) -> int:
    from .attention import AttentionCenter
    _print_json([i.to_dict() for i in AttentionCenter().open_items()])
    return 0


def _cmd_att_ack(args) -> int:
    from .attention import AttentionCenter
    ok = AttentionCenter().acknowledge(args.item_id)
    _print_json({"ok": ok})
    return 0


def _cmd_att_resolve(args) -> int:
    from .attention import AttentionCenter
    ok = AttentionCenter().resolve(args.item_id)
    _print_json({"ok": ok})
    return 0


def _cmd_tests_adapters(args) -> int:
    from .tests import TestExplorer
    _print_json({"adapters": TestExplorer().adapters()})
    return 0


def _cmd_tests_run(args) -> int:
    from .tests import RunContext, TestExplorer
    ctx = RunContext(repository=args.repo, worktree_id=args.worktree,
                     machine_id=args.machine, revision=args.revision,
                     workdir=args.workdir)
    events = TestExplorer().run(args.adapter, ctx, selector=args.selector,
                                cancel=args.cancel)
    _print_json([e.to_dict() for e in events])
    return 0


def _cmd_ci_pipelines(args) -> int:
    from .ci import WoodpeckerProvider
    provider = WoodpeckerProvider()
    if not provider.available:
        _print_json({"available": False, "pipelines": [],
                     "note": "CI provider offline — stale view (never guessed success)"})
        return 0
    views = provider.pipelines(args.repo)
    _print_json({"available": True, "pipelines": [v.to_dict() for v in views]})
    return 0


def _cmd_browser(args) -> int:
    from .browser import NoopBrowserAdapter, PlaywrightBrowserAdapter
    adapter = PlaywrightBrowserAdapter()
    if not adapter.available:
        adapter = NoopBrowserAdapter()
    cap = adapter.capture(args.url, viewport=args.viewport, project=args.project,
                          mission=args.mission, machine_id=args.machine)
    if args.json:
        _print_json({"ok": cap.ok, "url": cap.url,
                     "artifact": cap.artifact.to_dict() if cap.artifact else None,
                     "console_errors": cap.console_errors, "error": cap.error})
    else:
        print(f"{'ok' if cap.ok else 'failed'} {cap.url}"
              + (f" artifact={cap.artifact.id}" if cap.artifact else "")
              + (f" error={cap.error}" if cap.error else ""))
    return 0 if cap.ok else 1


def _cmd_review_request(args) -> int:
    from .browser import PlannotatorAdapter
    review = PlannotatorAdapter().request_review(
        repository=args.repo, session_id=args.session, candidate_id=args.candidate,
        evidence_links=args.evidence)
    _print_json(review.to_dict())
    return 0


def _cmd_review_resolve(args) -> int:
    from .browser import PlannotatorAdapter
    review = PlannotatorAdapter().resolve(args.review_id, args.decision,
                                          comment=args.comment)
    _print_json(review.to_dict())
    return 0


def _cmd_cand_id(args) -> int:
    from .candidates import candidate_id
    _print_json({"candidate_id": candidate_id(args.repo, args.worktree, args.branch)})
    return 0


def _cmd_web(args) -> int:
    from .web import serve
    return serve(host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # structured error surface
        from .errors import HerdrEngineeringError
        if isinstance(exc, HerdrEngineeringError):
            _print_json(exc.to_dict())
        else:
            _print_json({"code": "internal", "message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
