"""``herdr-eng`` — command-line entry point for the integration layer.

Exposes the real implemented capabilities as subcommands. This is the single
console entry point declared in pyproject.toml (``herdr-engineering.cli:main``).
Every command is a thin, typed wrapper over a module in this package; nothing
here re-implements Herdr or another owner system.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from .version import __version__

# The central fleet file, alone in its own directory (deploy.sh mounts the directory).
# Same value as fleet_config_cli.DEFAULT_CONFIG_PATH (not imported: keeps startup light).
FLEET_CONFIG_DEFAULT = "~/.config/herdr-engineering/fleet/fleet-config.yaml"


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="herdr-eng",
        description="Super HerdR / HerdR Engineering — thin curated integration layer over Herdr")
    parser.add_argument("--version", action="version",
                        version=f"herdr-eng {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build-cleanup", help="reclaim stale Rust build caches")
    cleanup_sub = p.add_subparsers(dest="cleanup_command", required=True)
    for action in ("run", "watch", "status"):
        cp = cleanup_sub.add_parser(action)
        cp.add_argument("--root", action="append", default=None,
                        help="allowed project root (repeatable; otherwise use config)")
        cp.add_argument("--dry-run", action="store_true")
        cp.set_defaults(func=_cmd_build_cleanup)

    from .open_client import register_parser
    register_parser(sub)

    # build-coordinate ----------------------------------------------------
    # Cross-agent single-flight build gate: bound concurrent cargo/tsc builds
    # across ALL agents on a host so they can't create a CPU build-storm.
    p = sub.add_parser("build-coordinate",
                       help="run a build command under the per-host build gate "
                            "(prevents CPU build-storms from concurrent agents)")
    p.add_argument("--slots", type=int, default=None,
                   help="max concurrent builds host-wide (default: auto = "
                        "max(2, cpu_cores-4); e.g. 20 on 24 cores)")
    p.add_argument("--max-load", type=float, default=1.5,
                   help="back off while 1-min load per core exceeds this (0=off)")
    p.add_argument("--wait", type=float, default=0.0,
                   help="seconds to wait for a slot at capacity (0=indefinite)")
    p.add_argument("--bypass-on-timeout", action="store_true",
                   help="if no slot frees within --wait, run the command ungated "
                        "instead of failing (best-effort coordination)")
    p.add_argument("--status", action="store_true",
                   help="print current build-gate occupancy and exit")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="the build command to run, e.g. `-- cargo check -j 4 --workspace --tests`")
    p.set_defaults(func=_cmd_build_coordinate)

    # doctor ---------------------------------------------------------------
    p = sub.add_parser("doctor", help="capability/health checks (spec 0050/0170)")
    p.add_argument("--json", action="store_true", help="stable machine-readable output")
    p.add_argument("--strict", action="store_true", help="fail when required integrations are missing")
    p.set_defaults(func=_cmd_doctor)

    # agent-mine -----------------------------------------------------------
    p = sub.add_parser("agent-mine", help="mine this host's agent transcripts into a redacted "
                                          "JSON summary (fleet dashboard / HPC-ssh adoption)")
    p.add_argument("--agents", default="claude,pi,codex,opencode")
    p.add_argument("--days", type=int, default=45, help="0 = all time")
    p.add_argument("--full", action="store_true", help="include truncated command bodies")
    p.add_argument("--out", default="~/.cache/agent-mine")
    p.add_argument("--limit", type=int, default=None, help="max files per agent (debug)")
    p.add_argument("--json-summary", action="store_true",
                   help="emit a compact JSON summary; skips the text report and TSV")
    p.add_argument("--summary-out", default=None,
                   help="with --json-summary, write the JSON to this file instead of stdout")
    p.add_argument("--since", default=None,
                   help="since-install date for the adoption-since metric")
    p.set_defaults(func=_cmd_agent_mine)

    # llm-env --------------------------------------------------------------
    p = sub.add_parser("llm-env", help="mint a project key for the fabric LLM gateway and write "
                                       "an env file (OPENAI_BASE_URL / OPENAI_API_KEY)")
    p.add_argument("--project", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--control-plane", default="http://dev.lan",
                   help="control-plane base URL (default http://dev.lan)")
    p.add_argument("--gateway", default="http://dev.lan:8088/v1",
                   help="fabric LLM gateway base URL")
    p.add_argument("--out", default=None,
                   help="env file to write (default ~/.config/herdr-engineering/llm-<project>.env)")
    p.set_defaults(func=_cmd_llm_env)

    # session run ----------------------------------------------------------
    p = sub.add_parser("session", help="run a command as a project session with the fabric "
                                       "LLM env injected (per-project token tracking)")
    ssub = p.add_subparsers(dest="session_command", required=True)
    p2 = ssub.add_parser("run", help="herdr-eng session run --project P -- <command...>")
    p2.add_argument("--project", required=True)
    p2.add_argument("--session", default=None); p2.add_argument("--mission", default=None)
    p2.add_argument("--key-mode", choices=["session", "project"], default=None,
                    help="session (default): mint per session, revoke at end; "
                         "project: reuse the cached project key")
    p2.add_argument("--no-llm-env", action="store_true",
                    help="opt out (also llm.inject: false in the local config)")
    p2.add_argument("--require-llm-env", action="store_true",
                    help="fail instead of starting without a key when the control plane is down")
    p2.add_argument("command", nargs=argparse.REMAINDER, help="-- command and args")
    p2.set_defaults(func=_cmd_session_run)

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
    p.add_argument("target", nargs="?", default=None,
                   help="snapshot path or tag (default: most recent)")
    p.add_argument("--snapshot", default=None, help=argparse.SUPPRESS)
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

    # fleet ----------------------------------------------------------------
    p = sub.add_parser("fleet", help="one-command fleet onboarding + version sync")
    fsub = p.add_subparsers(dest="fleet_command", required=True)
    pj = fsub.add_parser("join", help="attach a machine via herdr AND join it to the "
                                       "fabric fleet (herdr-eng fleet join <ssh-target>)")
    pj.add_argument("ssh_target", help="SSH target of the machine to attach (hostname/ip)")
    pj.add_argument("--label", default=None, help="fleet/host label (defaults to ssh_target)")
    pj.add_argument("--os", default="linux", choices=["linux", "macos"],
                    help="target OS group (linux|macos)")
    pj.add_argument("--user", default=None, help="fleet ssh user for ansible host_vars")
    pj.add_argument("--ansible-host", default=None,
                    help="tailnet MagicDNS/ip for ansible (defaults to ssh_target)")
    pj.add_argument("--gateway", default=None,
                    help="IGNORED for the fabric agent (warns); set fabric.url in "
                         "fleet-config.yaml or fabric.gateway_url in config.yaml")
    pj.add_argument("--no-attach", action="store_true",
                    help="skip the herdr machine add step")
    pj.add_argument("--no-provision", action="store_true",
                    help="skip the fleet-sync provisioning playbook")
    pj.add_argument("--roles", default=None,
                    help="comma separated fleet-config roles (default: keep existing, else host)")
    pj.add_argument("--fleet-config", default=FLEET_CONFIG_DEFAULT,
                    help="central fleet config file to add the host to")
    pj.add_argument("--control-plane-url", default=None,
                    help="control plane URL written to host.json (default: control_plane.public_url)")
    pj.add_argument("--no-credentials", action="store_true",
                    help="skip minting/installing the host's fleet-config token")
    pj.add_argument("--json", action="store_true")
    pj.set_defaults(func=_cmd_fleet_join)
    pc = fsub.add_parser("config", help="central fleet configuration file tools")
    csub = pc.add_subparsers(dest="fleet_config_command", required=True)
    for name, hlp in (("init", "create a new fleet-config.yaml interactively"),
                      ("validate", "validate the file (prints revision and host count only)"),
                      ("token", "mint a fresh token for an existing host and install it"),
                      ("revoke", "remove a host (its token stops working); backup first"),
                      ("render-compose", "write the compose env file (0600)"),
                      ("render-collab", "write the collab container's config slice (0600)"),
                      ("render-llm", "write the llm-proxy container's config slice (0600)")):
        sp = csub.add_parser(name, help=hlp)
        sp.add_argument("--path", default=FLEET_CONFIG_DEFAULT)
        if name == "init":
            sp.add_argument("--from-env", default=None, metavar="PATH",
                            help="import an existing docker-compose env file (e.g. the old "
                                 "control-plane.env): keeps the Postgres/MinIO credentials the "
                                 "live volumes were created with; prints names only")
            sp.add_argument("--answers", default=None, metavar="PATH",
                            help="YAML/JSON answers file (mode 0600); overrides --from-env")
            sp.add_argument("--with-api-keys", action="store_true",
                            help="generate control-plane API keys (keyed mode); the dashboard, "
                                 "llm-proxy and emit hook do not send keys yet")
        if name == "revoke":
            sp.add_argument("host", help="host id in the fleet config")
        if name == "token":
            sp.add_argument("host", help="host id in the fleet config")
            sp.add_argument("--target", default=None,
                            help="ssh target (default: the host id); 'local' for this machine")
            sp.add_argument("--control-plane-url", default=None)
        if name == "render-compose":
            sp.add_argument("--out", required=True, help="env file to write (0600)")
        if name == "render-collab":
            sp.add_argument("--out", required=True,
                            help="file to write (0600): only control_plane.collab")
        if name == "render-llm":
            sp.add_argument("--out", required=True,
                            help="file to write (0600): only llm + control_plane.llm_proxy")
        sp.set_defaults(func=_cmd_fleet_config)
    pu = fsub.add_parser("update", help="self-update controller + every host, then "
                                        "report parity (one command)")
    pu.add_argument("--host", default=None, help="update only this host (default: all)")
    pu.add_argument("--gateway", default=None,
                    help="IGNORED for the fabric agent (warns); set fabric.url in "
                         "fleet-config.yaml or fabric.gateway_url in config.yaml")
    pu.add_argument("--no-pull", action="store_true", help="skip controller git pull")
    pu.add_argument("--no-reinstall", action="store_true",
                    help="skip controller local pip reinstall")
    pu.add_argument("--json", action="store_true")
    pu.set_defaults(func=_cmd_fleet_update)
    ps = fsub.add_parser("status", help="read-only fleet version/registration status")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=_cmd_fleet_status)
    pd = fsub.add_parser("drift", help="show tool/plugin versions that differ across hosts, with suggested fixes")
    pd.add_argument("--control-plane", default=None,
                    help="control plane URL (default: host.json control_plane_url)")
    pd.add_argument("--host", help="only this host")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=_cmd_fleet_drift)

    recovery = fsub.add_parser("recover", help="plan or apply staggered outage recovery for Pi sessions")
    recovery.add_argument("recovery_command", nargs="?", choices=("plan", "apply", "run", "status"), default="plan")
    recovery.add_argument("--pi-revision", help="opt in to this exact immutable Pi release before recovery (planning only)")
    recovery.add_argument("--id", help="recovery plan ID (required for apply/status)")
    recovery.add_argument("--inventory", help="fleet inventory path (planning only)")
    recovery.add_argument("--host", help="fleet host filter (planning only)")
    recovery.add_argument("--session", default="default", help="explicit native HerdR server session")
    recovery.set_defaults(func=_cmd_fleet_recover)

    pr = fsub.add_parser("rollout", help="durable Pi runtime update and mission recovery")
    rsub = pr.add_subparsers(dest="rollout_command", required=True)
    for action in ("plan", "apply", "run", "status"):
        rp = rsub.add_parser(action)
        rp.add_argument("--state", default=None, help="private rollout state directory")
        if action == "plan":
            rp.add_argument("--pi-revision", required=True, help="full immutable Pi commit SHA")
            rp.add_argument("--host", default=None)
            rp.add_argument("--inventory", default=None)
        else:
            rp.add_argument("--id", required=True, help="rollout ID printed by plan")
        if action == "run":
            rp.add_argument("--interval-seconds", type=int, default=30, help="initial retry interval, 1..3600 (backoff caps at max(interval, 300))")
            rp.add_argument("--max-passes", type=int, default=None, help="stop after this many passes; omit to retry offline hosts until complete")
            rp.add_argument("--max-unchanged-online-passes", type=int, default=8, help="stop for review after this many unchanged online passes per session (1..1000)")
        rp.set_defaults(func=_cmd_fleet_rollout)

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
    p2 = asub.add_parser("upload", help="upload a file to the shared control-plane artifact registry")
    p2.add_argument("data", help="path to file")
    p2.add_argument("--control-plane", default=None)
    p2.add_argument("--filename", default=None)
    p2.add_argument("--mime", default="application/octet-stream")
    p2.add_argument("--session", default=None)
    p2.add_argument("--mission", default=None)
    p2.set_defaults(func=_cmd_art_upload)
    asub.add_parser("list", help="list stored artifacts").set_defaults(func=_cmd_art_list)
    p2 = asub.add_parser("retention", help="enforce retention policy")
    p2.set_defaults(func=_cmd_art_retention)
    from .artifact_reports_client import add_report_subparser
    add_report_subparser(asub)
    # system-graph artifact (P5): deterministic sanitized snapshot renderer.
    ga = sub.add_parser("graph-artifact", help="render a graph snapshot to validated HTML (system-graph P5)")
    gas = ga.add_subparsers(dest="graph_artifact_command", required=True)
    p2 = gas.add_parser("render", help="render a graph snapshot JSON to a deterministic sanitized HTML artifact")
    p2.add_argument("graph_json", help="path to a graph snapshot JSON file")
    p2.add_argument("--out", default=None, help="output .html path (default: <graph_json>.html)")
    p2.set_defaults(func=_cmd_graph_artifact_render)

    # summary --------------------------------------------------------------
    p = sub.add_parser("summary", help="/summary skill: install, verify, remove, read session history")
    smsub = p.add_subparsers(dest="summary_command", required=True)
    for name, helptext in (("install", "install or upgrade the summary skill and /summary commands"),
                           ("uninstall", "remove only what this installer wrote"),
                           ("verify", "read-only installation check (exit 1 if missing or invalid)")):
        p2 = smsub.add_parser(name, help=helptext)
        p2.add_argument("--scope", choices=("global", "project"), default="global")
        p2.add_argument("--home", default=None, help="override $HOME (tests)")
        p2.add_argument("--project-dir", default=None, help="project root for --scope project (default: cwd)")
        if name != "verify":
            p2.add_argument("--dry-run", action="store_true")
        p2.add_argument("--json", action="store_true")
        p2.set_defaults(func=_cmd_summary_manage)
    p2 = smsub.add_parser("history", help="read-only normalized history of the current agent session")
    p2.add_argument("--agent", choices=("claude", "pi", "codex", "opencode"), default=None)
    p2.add_argument("--session", default=None, help="session id or transcript path")
    p2.add_argument("--cwd", default=None)
    p2.add_argument("--home", default=None)
    p2.add_argument("--max-events", type=int, default=400)
    p2.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON")
    p2.set_defaults(func=_cmd_summary_history)
    p2 = smsub.add_parser("report", help="run the summary skill headlessly on one recorded session (full report as JSON)")
    p2.add_argument("--agent", required=True, choices=("claude", "pi", "codex", "opencode"))
    p2.add_argument("--session", required=True, help="session id of the target session")
    p2.add_argument("--harness", choices=("claude", "codex"), default="claude", help="summarizer harness")
    p2.add_argument("--timeout", type=int, default=300)
    p2.set_defaults(func=_cmd_summary_report)
    p2 = smsub.add_parser("publish", help="publish a finished /summary report (stdin) to the dashboard and a side pane")
    p2.add_argument("--agent", required=True, choices=("claude", "pi", "codex", "opencode"))
    p2.add_argument("--session", required=True, help="session id of the summarized session")
    p2.add_argument("--no-pane", action="store_true")
    p2.add_argument("--no-dashboard", action="store_true")
    p2.set_defaults(func=_cmd_summary_publish)

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
    p2.add_argument("--machine", required=True)
    p2.add_argument("--host", required=True,
                    help="target host; 'auto' = this machine's tailnet IP")
    p2.add_argument("--port", type=int, required=True)
    p2.add_argument("--protocol", default="http")
    p2.add_argument("--session"); p2.add_argument("--repo"); p2.add_argument("--worktree")
    p2.add_argument("--process"); p2.add_argument("--label")
    p2.add_argument("--route", action="append", default=None, metavar="PREFIX=HOST:PORT",
                    help="same-origin route (repeatable): a path prefix -> backend, "
                         "e.g. /api=api:8300. The --host/--port is the catch-all. "
                         "Makes this a multi-backend same-origin app edge.")
    p2.add_argument("--preferred-port", type=int, default=None,
                    help="pin the origin's external dev port (default: match --port, "
                         "fall back to the dev range if taken)")
    p2.set_defaults(func=_cmd_dev_register)
    p2 = dsub.add_parser("enroll", help="enroll a same-origin app edge (register the "
                    "origin + optional forwarder); see the fabric-enroll skill")
    p2.add_argument("--manifest", help="JSON manifest: {origin_port, machine, repo, label, "
                    "catchall:{host,port}, routes:[{prefix,host,port}]}")
    p2.add_argument("--origin-port", type=int, help="the origin's external dev port")
    p2.add_argument("--machine", help="machine id where the backends run (default: hostname)")
    p2.add_argument("--repo"); p2.add_argument("--label"); p2.add_argument("--protocol")
    p2.add_argument("--catchall", metavar="HOST:PORT",
                    help="default backend (overrides manifest.catchall)")
    p2.add_argument("--route", action="append", metavar="PREFIX=HOST:PORT",
                    help="path-prefix route (repeatable; overrides manifest.routes)")
    p2.add_argument("--serve", action="store_true",
                    help="run the per-host forwarder and block (no gateway needed)")
    p2.add_argument("--bind", default="127.0.0.1",
                    help="forwarder bind address for --serve (default 127.0.0.1)")
    p2.add_argument("--require-gateway", action="store_true",
                    help="fail (nonzero exit) if the gateway is not reachable "
                         "instead of silently falling back to the local registry")
    p2.set_defaults(func=_cmd_dev_enroll)
    p2 = dsub.add_parser("enroll-sync", help="auto-enroll every origin manifest in a "
                    "dir (watch for new projects); see the fabric-enroll skill")
    p2.add_argument("--dir", required=True,
                    help="directory of origin.json manifests (one file per app)")
    p2.add_argument("--watch", action="store_true",
                    help="keep re-syncing so new/changed manifests auto-enroll")
    p2.add_argument("--interval", type=float, default=30.0,
                    help="watch interval in seconds (default 30)")
    p2.add_argument("--serve", action="store_true",
                    help="no gateway: run a per-host forwarder per origin")
    p2.add_argument("--bind", default="127.0.0.1",
                    help="forwarder bind address (default 127.0.0.1)")
    p2.add_argument("--git-pull", action="store_true",
                    help="if --dir is a git repo, 'git pull --ff-only' before "
                         "each sync (auto-pick-up committed origin.json files)")
    p2.add_argument("--require-gateway", action="store_true",
                    help="fail (nonzero exit) if the gateway is not reachable "
                         "instead of silently falling back to the local registry")
    p2.set_defaults(func=_cmd_dev_enroll_sync)
    p2 = dsub.add_parser("heartbeat", help="renew a lease")
    p2.add_argument("lease_id"); p2.set_defaults(func=_cmd_dev_heartbeat)
    p2 = dsub.add_parser("close", help="close a lease")
    p2.add_argument("lease_id"); p2.set_defaults(func=_cmd_dev_close)
    p2 = dsub.add_parser("gateway", help="run the fabric gateway (front door + router)")
    p2.add_argument("--bind", default=None, help="comma-separated bind IPs (default: config/auto)")
    p2.add_argument("--control-port", type=int, default=None)
    p2.add_argument("--url-base", default=None, help="stable base for dev:<port> (default: auto)")
    p2.add_argument("--reset-lock", action="store_true",
                    help="take over a stale gateway.lock (e.g. after a crash/SIGKILL; "
                         "safe when no other gateway is running)")
    p2.add_argument("--rewrite-headers", action="store_true",
                    help="opt-in: rewrite loopback URLs in Location / CORS / cookie-domain "
                         "headers of HTTP leases to the fabric address (forces "
                         "Connection: close; bodies and websockets are untouched)")
    p2.add_argument("--tls-cert", default=None, help="serve HTTPS on every leased port with this certificate "
                    "(same port as HTTP; issue one with `devfabric ca issue`)")
    p2.add_argument("--tls-key", default=None, help="private key for --tls-cert")
    p2.add_argument("--acme-domain", default=None, help="serve a publicly trusted Let's Encrypt certificate for this "
                    "name and *.name (e.g. local.metabolomics.us); also the default --url-base")
    p2.add_argument("--url-alias", action="append", default=[],
                    help="extra name the fabric answers to when resolving URLs (repeatable; dev.lan is added "
                         "automatically when a private CA exists)")
    p2.add_argument("--named-port", type=int, default=None,
                    help="port for stable https://<service>.<acme-domain> URLs (default 443; if it cannot be bound "
                         "the gateway keeps using per-port URLs)")
    p2.add_argument("--apex-upstream", default=None, metavar="HOST:PORT",
                    help="where the bare fleet names (https://dev.lan, https://<acme-domain>) go: the dashboard, e.g. "
                         "100.104.39.6:80. Needs the named listener (--acme-domain) and a certificate for the name")
    p2.add_argument("--route", action="append", default=[], metavar="HOST=UP:PORT",
                    help="send one exact host name on the named port to an upstream (repeatable), e.g. "
                         "bender.tail0c50da.ts.net=127.0.0.1:8787 to take over a `tailscale serve` rule")
    p2.add_argument("--tailscale-cert", default=None, metavar="NAME",
                    help="also serve this node's ts.net NAME with the certificate `tailscale cert` provides")
    p2.add_argument("--no-named-urls", action="store_true", help="do not serve stable service-name URLs")
    p2.add_argument("--acme-dir", default=None, help="certbot state directory (default: ~/.config/herdr-engineering/acme)")
    p2.add_argument("--acme-staging", action="store_true", help="use the Let's Encrypt staging CA (untrusted; for trials)")
    p2.add_argument("--renew-certs", action="store_true",
                    help="renew certificates from this gateway (set on exactly one node of a pair)")
    p2.add_argument("--peer", default=None, help="control URL of the other gateway: run as an active/standby pair "
                    "(the standby mirrors leases and promotes itself when the peer stops answering)")
    p2.add_argument("--node-id", default=None, help="stable name for this gateway in a pair (default: hostname)")
    p2.add_argument("--promote-after", type=int, default=3, help="missed peer checks before a standby takes over")
    p2.add_argument("--allow-user", action="append", default=[],
                    help="tailnet login allowed to open leased ports (repeatable). With --allow-user/--allow-tag "
                         "every other peer, guests and shared-in devices included, is refused")
    p2.add_argument("--allow-tag", action="append", default=[], help="tailnet ACL tag allowed (e.g. tag:dev)")
    p2.set_defaults(func=_cmd_dev_gateway)
    p2 = dsub.add_parser("cert", help="publicly trusted certificate for the fabric name (Let's Encrypt, DNS-01 on Route 53)")
    csub = p2.add_subparsers(dest="cert_cmd", required=True)
    for name, helptext in (("issue", "obtain the certificate now (no-op while a valid one exists unless --force)"),
                           ("renew", "renew only when due (safe to run from cron)"),
                           ("status", "show expiry and whether renewal is due")):
        q = csub.add_parser(name, help=helptext)
        q.add_argument("--domain", default=None, help="fleet name (default: fabric.acme_domain from config)")
        q.add_argument("--dir", default=None, help="certbot state directory (default: ~/.config/herdr-engineering/acme)")
        q.add_argument("--staging", action="store_true", help="use the Let's Encrypt staging CA")
        q.add_argument("--email", default=None, help="expiry-notice address; omitted = register without one")
        if name == "issue":
            q.add_argument("--force", action="store_true", help="force a new certificate (counts against rate limits)")
    p2.set_defaults(func=_cmd_dev_cert)
    p2 = dsub.add_parser("dns", help="publish the fabric name in Route 53")
    dnssub = p2.add_subparsers(dest="dns_cmd", required=True)
    q = dnssub.add_parser("publish", help="UPSERT A records for NAME and *.NAME pointing at the gateway tailnet IPs")
    q.add_argument("--domain", default=None, help="fleet name (default: fabric.acme_domain from config)")
    q.add_argument("--ip", action="append", default=[], help="gateway tailnet IPv4 (repeatable; default: this host's)")
    q.add_argument("--ttl", type=int, default=60)
    q.add_argument("--zone-id", default=None, help="hosted zone id (default: found by name)")
    q.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    p2.set_defaults(func=_cmd_dev_dns)
    p2 = dsub.add_parser("ca", help="private CA for the fabric's HTTPS (dev.lan)")
    casub = p2.add_subparsers(dest="ca_cmd", required=True)
    for name, helptext in (("init", "create the name-constrained root CA"),
                           ("issue", "issue the gateway's server certificate"),
                           ("export", "write the root for device enrollment (.pem and iOS .mobileconfig)"),
                           ("status", "show the CA, its fingerprint and certificate expiry")):
        q = casub.add_parser(name, help=helptext)
        q.add_argument("--dir", default=None, help="CA directory (default: ~/.config/herdr-engineering/ca)")
        if name == "init":
            q.add_argument("--domain", action="append", help="fabric domain the CA may sign (default: dev.lan)")
        if name == "issue":
            q.add_argument("--name", action="append", help="DNS name for the certificate "
                           "(default: dev.lan and *.dev.lan)")
        if name == "export":
            q.add_argument("--out", default=".", help="directory to write fabric-ca.pem / fabric-ca.mobileconfig")
        q.set_defaults(func=_cmd_dev_ca)
    p2 = dsub.add_parser(
        "agent",
        help="auto-register this host's web services with the gateway")
    p2.add_argument("--interval", type=float, default=5.0,
                    help="seconds between scan cycles (default: 5)")
    p2.add_argument("--once", action="store_true",
                    help="run a single scan cycle and exit")
    p2.add_argument("--label-prefix", default="",
                    help="prefix for auto-generated lease labels")
    p2.set_defaults(func=_cmd_dev_agent)
    p2 = dsub.add_parser("list", help="list active leases")
    p2.add_argument("--json", action="store_true", default=True)
    p2.set_defaults(func=_cmd_dev_list)
    p2 = dsub.add_parser("resolve", help="resolve a dev port to a lease")
    p2.add_argument("port", type=int); p2.set_defaults(func=_cmd_dev_resolve)
    dsub.add_parser("reconcile", help="expire stale leases").set_defaults(func=_cmd_dev_reconcile)
    dsub.add_parser("gc", help="drop expired leases past the retention window (shrink the store)"
                    ).set_defaults(func=_cmd_dev_gc)
    p2 = dsub.add_parser("serve", help="forward the leased external port to the target")
    p2.add_argument("lease_id")
    p2.add_argument("--bind", default="127.0.0.1",
                    help="bind address (loopback or private; public refused)")
    p2.add_argument("--timeout", type=float, default=None,
                    help="auto-stop after N seconds (for tests)")
    p2.set_defaults(func=_cmd_dev_serve)

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
    p2 = csub.add_parser("repos", help="list CI repositories")
    p2.set_defaults(func=_cmd_ci_repos)
    p2 = csub.add_parser("agents", help="list CI runner agents (runner/queue state)")
    p2.set_defaults(func=_cmd_ci_agents)
    p2 = csub.add_parser("pipelines", help="list pipelines for a repo (owner/name)")
    p2.add_argument("--repo", required=True); p2.set_defaults(func=_cmd_ci_pipelines)
    p2 = csub.add_parser("pipeline", help="pipeline detail incl. tasks (by number)")
    p2.add_argument("--repo", required=True); p2.add_argument("number", type=int)
    p2.set_defaults(func=_cmd_ci_pipeline)
    p2 = csub.add_parser("logs", help="raw log of one pipeline step (if the API exposes it)")
    p2.add_argument("--repo", required=True); p2.add_argument("--pipeline", type=int, required=True)
    p2.add_argument("--step", required=True); p2.set_defaults(func=_cmd_ci_logs)
    p2 = csub.add_parser("debug", help="build a bounded 'Debug with Pi' handoff for a failing pipeline")
    p2.add_argument("--repo", required=True); p2.add_argument("number", type=int)
    p2.set_defaults(func=_cmd_ci_debug)

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

    # review-gate ----------------------------------------------------------
    p = sub.add_parser("review-gate", help="PR review gate tools")
    gsub = p.add_subparsers(dest="review_gate_command", required=True)
    p2 = gsub.add_parser("worker", help="run the reviewer worker (claims jobs, calls the model)")
    p2.add_argument("--config", required=True, help="worker YAML config file")
    p2.add_argument("--once", action="store_true", help="run one claim cycle and exit")
    p2.set_defaults(func=_cmd_review_gate_worker)
    p2 = gsub.add_parser("pool", help="run a pool of reviewer workers sized by the review queue")
    p2.add_argument("--config", required=True, help="pool YAML config file")
    p2.set_defaults(func=_cmd_review_gate_pool)
    p2 = gsub.add_parser("check-skips", help="fail when a test is skipped outside the disabled-tests file")
    p2.add_argument("--junit", action="append", required=True, help="junit XML file or directory (repeatable)")
    p2.add_argument("--disabled", required=True, help="disabled-tests YAML file")
    p2.set_defaults(func=_cmd_review_gate_check_skips)

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

    from .architecture import command as _architecture_command
    _architecture_command.register(sub)

    # control-plane --------------------------------------------------------
    p = sub.add_parser("control-plane",
                       help="HerdR Dev Fabric Control Plane (spec 0190)")
    csub = p.add_subparsers(dest="control_plane_command", required=True)
    p2 = csub.add_parser("migrate", help="apply versioned SQL migrations")
    p2.add_argument("--migrations", default=None,
                    help="migrations dir (default: bundled deploy/control-plane)")
    p2.add_argument("--fleet-config", default=FLEET_CONFIG_DEFAULT,
                    help="central fleet config file; control_plane.pg_dsn names the database")
    p2.set_defaults(func=_cmd_cp_migrate)
    p2 = csub.add_parser("collab-repair", help="set aside foreign rows of the collaboration log (dry run by default; "
                         "never deletes: rows move to a quarantine file/table)")
    p2.add_argument("--log-file", default=None, help="a collab-only JSON-lines log; without it the Postgres "
                    "fabric_events table named by --fleet-config is used")
    p2.add_argument("--fleet-config", default=FLEET_CONFIG_DEFAULT)
    p2.add_argument("--apply", action="store_true", help="really move the rows (needs --confirm-quarantine)")
    p2.add_argument("--confirm-quarantine", action="store_true", help="explicit confirmation for --apply")
    p2.add_argument("--include-gap-tail", action="store_true",
                    help="also set aside every row after the first missing sequence (may drop genuine events)")
    p2.set_defaults(func=_cmd_cp_collab_repair)
    p2 = csub.add_parser("web", help="serve the control-plane UI + /api/v1 + SSE")
    p2.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1; private-by-default)")
    p2.add_argument("--port", type=int, default=0,
                    help="bind port (default 0 = ephemeral)")
    p2.add_argument("--api-key", default=None,
                    help="require X-API-Key header (Spec §104)")
    p2.add_argument("--api-keys", default=None,
                    help="comma list of key=role (viewer|operator) pairs "
                         "(Spec §104); e.g. k1=viewer,k2=operator")
    p2.add_argument("--fleet-config", default=FLEET_CONFIG_DEFAULT,
                    help="central fleet config file: the only source of configuration. "
                         "It is served to hosts, and its control_plane section supplies the "
                         "Postgres DSN, host aliases, API keys, RustFS/MinIO, CI-actions "
                         "and inspector settings")
    p2.add_argument("--pg-dsn", default=None,
                    help="optional Postgres DSN to hydrate the repo from "
                         "durable state on startup (Spec §106)")
    p2.add_argument("--system-graph", action="store_true",
                    help="opt-in: reflect mission/session/repo/trace lifecycle events "
                         "into the running lineage graph (system-graph spec P2-P4)")
    p2.add_argument("--seed-demo", action="store_true",
                    help="seed a fresh in-memory repo with demo data so the "
                         "UI is populated (useful when no Postgres is present)")
    p2.add_argument("--web-root", default=None,
                    help="directory of static UI assets (index.html, app.js, "
                         "herdr-web-*). Default: resolved relative to the "
                         "package (dev checkout); set to /app/packages in the "
                         "container image.")
    p2.add_argument("--allow-open", action="store_true",
                    help="explicitly permit an unauthenticated server on a "
                         "non-loopback bind. ONLY safe when the host port is "
                         "bound to the Tailnet interface / `tailscale serve` "
                         "(Tailnet = trust boundary). Refused without this flag.")
    p2.add_argument("--collab-users-file", default=None,
                    help="enable the collaboration network (HCN, off by default): JSON "
                         "file mapping user_id -> sha256(token) hex. Pilot authenticator "
                         "only; replace with SSO before real use (ADR-0007).")
    p2.add_argument("--collab-cognito-pool", default=None,
                    help="authenticate collaboration users with Cognito access tokens from "
                         "this user pool (e.g. us-west-2_GjtcM0PCp); needs --collab-cognito-client")
    p2.add_argument("--collab-cognito-client", action="append", default=[],
                    help="allowed Cognito app client id (repeatable)")
    p2.add_argument("--collab-cognito-web-client", default=None,
                    help="app client used by the browser page (Hosted UI, PKCE); also allowed as a token client")
    p2.add_argument("--collab-cognito-domain", default=None,
                    help="Cognito Hosted UI base URL, e.g. https://<prefix>.auth.us-west-2.amazoncognito.com")
    p2.add_argument("--collab-public-url", default=None,
                    help="the https URL users open (must equal a registered callback URL), e.g. "
                         "https://bender.tail0c50da.ts.net:9447/")
    p2.add_argument("--collab-cognito-group", default=None,
                    help="required cognito:groups membership (default: local.dev)")
    p2.add_argument("--collab-open-signup", action="store_true",
                    help="approval mode: any pool account may sign in and ask to join, and nothing works until "
                         "an approver or the operator approves it; --collab-cognito-group members are approved "
                         "on sight instead of being required")
    p2.add_argument("--collab-replay-history", default=None, choices=["full", "since_join"],
                    help="lineage replay window for members (default since_join; normally set by "
                         "control_plane.collab.replay_history in the fleet config)")
    p2.add_argument("--collab-approver-group", action="append", default=[],
                    help="Cognito group whose members may approve new people (repeatable; approval mode)")
    p2.add_argument("--collab-public-port", type=int, default=None,
                    help="also serve ONLY collaboration (API, MCP, page) on this port; point the "
                         "internet-facing proxy here, never at the main port")
    p2.add_argument("--collab-public-host", default=None,
                    help="bind address for --collab-public-port (default: --host)")
    p2.add_argument("--collab-sync-dir", default=None,
                    help="read Cognito JWKS (jwks.json) and group members (members.json) from this "
                         "directory, refreshed by a host job, instead of calling AWS: the "
                         "container then needs no credentials and no outbound network")
    p2.add_argument("--collab-operator-port", type=int, default=None,
                    help="with --collab-only: also serve the collaboration API WITHOUT sign-in on this "
                         "port, as --collab-operator-user, for the local owner's dashboard (publish it "
                         "on the tailnet interface only)")
    p2.add_argument("--collab-operator-user", default=None, help="Cognito username the operator acts as")
    p2.add_argument("--collab-operator-email", default=None, help="email shown for the operator")
    p2.add_argument("--collab-operator-host", action="append", default=[],
                    help="Host header value the operator listener accepts (repeatable; DNS-rebinding guard)")
    p2.add_argument("--collab-operator-url", default=None,
                    help="dashboard: forward /api/v1/collab/* to the operator listener of the "
                         "isolated collab service (e.g. http://100.64.0.1:8082)")
    # No flag on purpose: the T11 delegation kill switch is YAML only (control_plane.collab.delegation_enabled).
    p2.set_defaults(collab_delegation_enabled=False)
    p2.add_argument("--collab-dashboard-host", action="append", default=[],
                    help="extra DNS name the dashboard may be reached by for collaboration "
                         "(IP literals and localhost always work; repeatable; DNS-rebinding guard)")
    p2.add_argument("--collab-only", action="store_true",
                    help="serve ONLY the public collaboration surface on --collab-public-port: no "
                         "dashboard, no operator API, no database (events go to --collab-log-file); "
                         "unset --collab-cognito-* / --collab-public-url "
                         "come from control_plane.collab in --fleet-config (the render-collab slice)")
    p2.add_argument("--collab-log-file", default=None,
                    help="append-only JSON-lines collaboration event log (required with --collab-only)")
    p2.add_argument("--collab-require-https", action="store_true",
                    help="serve collaboration only for requests forwarded by a TLS proxy "
                         "(X-Forwarded-Proto: https); other listeners on this server stay "
                         "plain. Satisfies the non-loopback TLS requirement.")
    p2.add_argument("--collab-tls-terminated", action="store_true",
                    help="assert TLS is terminated in front of this server; required to "
                         "enable collaboration on a non-loopback bind")
    p2.set_defaults(func=_cmd_cp_web)
    p2 = csub.add_parser("public-gateway", help="serve the isolated approved-artifact delivery gateway")
    p2.add_argument("--host", default="127.0.0.1")
    p2.add_argument("--port", type=int, default=0)
    p2.add_argument("--fleet-config", default=FLEET_CONFIG_DEFAULT)
    p2.set_defaults(func=_cmd_cp_public_gateway)
    p2 = csub.add_parser("worker", help="run the bounded background worker")
    p2.add_argument("--interval", type=float, default=30.0,
                    help="seconds between passes (default: 30)")
    p2.add_argument("--once", action="store_true",
                    help="run a single pass and exit")
    p2.add_argument("--migrations", default=None,
                    help="migrations dir (default: bundled deploy/control-plane)")
    p2.add_argument("--fleet-config", default=FLEET_CONFIG_DEFAULT,
                    help="central fleet config file; control_plane.pg_dsn names the database")
    p2.add_argument("--host-id", default=None, help="worker host identity for artifact job placement")
    p2.set_defaults(func=_cmd_cp_worker)
    p2 = csub.add_parser("feed-pi", help="publish a read-only Pi orchestration snapshot",
                         description="Post a Pi snapshot using HERDR_CP_INGEST_KEY (ingest-role API key).")
    p2.add_argument("--snapshot", required=True, help="path to orchestration-snapshot.json")
    p2.add_argument("--url", default=None,
                    help="control-plane base URL (default: host.json control_plane_url)")
    p2.add_argument("--interval", type=float, default=0,
                    help="seconds between snapshot reads (default: 0 = once)")
    p2.set_defaults(func=_cmd_cp_feed_pi)
    p2 = csub.add_parser("mission-feed", help="publish read-only live Pi mission activity")
    p2.add_argument("--control-plane", default=None,
                    help="control plane URL (default: host.json control_plane_url)")
    p2.add_argument("--host-id", default=None, help="host id (default: host.json host_id)")
    p2.add_argument("--control-dir", default=None,
                    help="native Pi control directory (default: Pi runtime directory)")
    p2.add_argument("--interval", type=float, default=5.0)
    p2.add_argument("--once", action="store_true")
    p2.set_defaults(func=_cmd_cp_mission_feed)
    p2 = csub.add_parser("llm-proxy",
                         help="OpenAI-compatible fabric gateway: project keys, token accounting")
    p2.add_argument("--host", default="127.0.0.1")
    p2.add_argument("--port", type=int, default=8088)
    p2.add_argument("--control-plane", default="http://control-plane-web:8080",
                    help="control-plane base URL")
    p2.add_argument("--upstream", default=None,
                    help="upstream base URL (default: llm.upstream_url from the fleet config)")
    p2.add_argument("--fleet-config", default="~/.config/herdr-engineering/llm-proxy/llm-config.json",
                    help="the llm slice of the central fleet file (`fleet config render-llm`): llm.upstream_key/upstream_url and "
                         "control_plane.llm_proxy tuning (the only source of configuration)")
    p2.set_defaults(func=_cmd_cp_llm_proxy)

    return parser


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------
def _cmd_cp_feed_pi(args) -> int:
    import math
    import time
    from pathlib import Path

    from .control_plane.pi_snapshot import post_envelopes, snapshot_to_envelopes
    from .host_config import HostConfigUnavailable, resolve_control_plane

    if not os.environ.get("HERDR_CP_INGEST_KEY", "").strip():
        print("Set HERDR_CP_INGEST_KEY to an ingest-role API key", file=sys.stderr)
        return 2
    if not math.isfinite(args.interval) or args.interval < 0 or args.interval > 2 ** 31 - 1:
        print("--interval must be a finite non-negative number of seconds", file=sys.stderr)
        return 2
    try:
        url = resolve_control_plane(args.url)
    except HostConfigUnavailable:
        print("Set --url or configure host.json control_plane_url", file=sys.stderr)
        return 2
    while True:
        try:
            doc = json.loads(Path(args.snapshot).expanduser().read_text(encoding="utf-8"))
            envelopes = snapshot_to_envelopes(doc)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # File paths and malformed snapshot fields can contain secrets.
            print("Cannot read a valid Pi snapshot; check --snapshot and its schema", file=sys.stderr)
            return 2
        try:
            posted = post_envelopes(url, None, envelopes)
        except (ValueError, RuntimeError):
            print("Snapshot ingest failed; check --url and HERDR_CP_INGEST_KEY", file=sys.stderr)
            return 1
        _print_json({"ok": True, "posted": posted})
        if args.interval == 0:
            return 0
        time.sleep(args.interval)


def _cmd_cp_mission_feed(args) -> int:
    from pathlib import Path

    from .host_config import (
        ConfigRefresher,
        HostConfigUnavailable,
        exit_unavailable,
        host_config,
        host_config_or_none,
        resolve_control_plane,
    )
    from .mission_activity_feed import run_feed
    try:
        url = resolve_control_plane(args.control_plane)
        if args.control_plane and args.host_id:
            # Fully explicit: runs without host.json; only the ingest key (if
            # any) follows the config.
            live = host_config_or_none()
        else:
            live = host_config()
        host_id = args.host_id or live.host_id
        refresher = None if args.once or live is None else ConfigRefresher(live)
    except HostConfigUnavailable as exc:
        return exit_unavailable(exc)
    return run_feed(url, host_id=host_id,
                    control_dir=Path(args.control_dir) if args.control_dir else None,
                    interval=args.interval, once=args.once, refresher=refresher,
                    follow_url=not args.control_plane)


def _cmd_cp_llm_proxy(args) -> int:
    """Serve the fabric LLM gateway. Everything it needs comes from the llm slice
    of the central fleet file (`fleet config render-llm`: llm.upstream_key /
    upstream_url, control_plane.llm_proxy tuning); it refuses a file carrying
    anything else. The key is never taken from argv or the environment."""
    from pathlib import Path

    from .fleet_config import FleetConfigError, load_fleet_config
    from .fleet_config_cli import check_llm_slice
    from .llm_proxy import DEFAULT_MAX_INFLIGHT, DEFAULT_QUEUE_WAIT_SECONDS, serve
    path = Path(args.fleet_config).expanduser()
    try:
        cfg = load_fleet_config(path)
        check_llm_slice(cfg)
        llm = cfg.section("llm")
        try:
            tuning = cfg.section("control_plane").get("llm_proxy") or {}
        except FleetConfigError:
            tuning = {}
        if not isinstance(tuning, dict):
            raise FleetConfigError("control_plane.llm_proxy must be a mapping")
        max_inflight = int(tuning.get("max_inflight", DEFAULT_MAX_INFLIGHT))
        wait_seconds = float(tuning.get("queue_wait_seconds", DEFAULT_QUEUE_WAIT_SECONDS))
    except (FleetConfigError, OSError, ValueError, TypeError) as exc:
        print(f"--fleet-config {path}: {exc}", file=sys.stderr)
        return 2
    key = str(llm.get("upstream_key") or "")
    upstream = args.upstream or str(llm.get("upstream_url") or "")
    if not key or not upstream:
        print("fleet config llm.upstream_key and llm.upstream_url are required", file=sys.stderr)
        return 2
    httpd = serve(args.host, args.port, args.control_plane, upstream, key,
                  max_inflight=max_inflight, wait_seconds=wait_seconds)
    print(f"llm-proxy listening on {args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_llm_env(args) -> int:
    """Mint a project key and write a 0600 env file; the key is never printed."""
    from .session_env import LlmEnvError, mint_key, project_cache_path, write_env_file
    try:
        minted = mint_key(args.control_plane, args.project, args.label)
        out = args.out or project_cache_path(args.project)
        write_env_file(out, {"OPENAI_BASE_URL": args.gateway,
                             "OPENAI_API_KEY": minted["key"]})
    except LlmEnvError as exc:
        _print_json({"code": "llm-env", "message": str(exc)})
        return 1
    _print_json({"project": args.project, "key_id": minted["key_id"], "env_file": out})
    return 0


def _cmd_session_run(args) -> int:
    from .config import InvalidError, load_config
    from .session_env import run_session
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        _print_json({"code": "usage", "message": "no command given (use: session run --project P -- cmd)"})
        return 2
    try:
        cfg = load_config()
    except InvalidError as exc:
        _print_json({"code": "config", "message": str(exc)})
        return 1
    return run_session(cfg, project=args.project, command=command, session=args.session,
                       mission=args.mission, disable=args.no_llm_env,
                       require=args.require_llm_env, key_mode=args.key_mode)


def _cmd_doctor(args) -> int:
    from .doctor import doctor_main
    return doctor_main(args)


def _cmd_summary_manage(args) -> int:
    from . import session_summary as ss
    kw = dict(scope=args.scope, home=Path(args.home) if args.home else None,
              project=Path(args.project_dir) if args.project_dir else None)
    ok = True
    if args.summary_command == "verify":
        ok, results = ss.verify(**kw)
    else:
        fn = ss.install if args.summary_command == "install" else ss.uninstall
        results = fn(dry_run=args.dry_run, **kw)
    if args.json:
        _print_json({"ok": ok, "results": [r.to_dict() for r in results]})
    else:
        for r in results:
            print(f"{r.action:10} {r.target}" + (f"  ({r.detail})" if r.detail else ""))
            if r.action == "preserved":
                print(f"warning: kept existing {r.target}", file=sys.stderr)
    return 0 if ok else 1


def _cmd_summary_history(args) -> int:
    from . import session_history as sh
    home = Path(args.home) if args.home else None
    res = sh.resolve(agent=args.agent, session=args.session, cwd=args.cwd, home=home)
    out = sh.read_history(res, home=home, max_events=args.max_events)
    out["journal"] = sh.journal_projection(res.session_id)
    _print_json(out)
    return 0


def _cmd_summary_report(args) -> int:
    from . import summary_report as sr
    try:
        _print_json(sr.run_report(args.agent, args.session, harness=args.harness, timeout=args.timeout))
    except sr.ReportError as exc:
        _print_json({"ok": False, "error": exc.code, "detail": exc.detail})
        return 1
    return 0


def _cmd_summary_publish(args) -> int:
    from . import summary_report as sr
    try:
        out = sr.publish_report(args.agent, args.session, sys.stdin.read(),
                                pane=not args.no_pane, dashboard=not args.no_dashboard)
    except sr.ReportError as exc:
        _print_json({"ok": False, "error": exc.code, "detail": exc.detail})
        return 1
    _print_json(out)
    return 0 if out["ok"] else 1


def _cmd_agent_mine(args) -> int:
    from . import agent_mine
    args.out = os.path.expanduser(args.out)
    return agent_mine.agent_mine_main(args)


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
    target = args.target or args.snapshot
    snap = PowerpackManager().rollback(target)
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


def _cmd_fleet_join(args) -> int:
    from .fleet import DEFAULT_GATEWAY, fleet_join, warn_gateway_ignored
    warn_gateway_ignored(args.gateway)
    report = fleet_join(
        ssh_target=args.ssh_target,
        label=args.label,
        os_name=args.os,
        user=args.user,
        ansible_host=args.ansible_host or args.ssh_target,
        gateway=DEFAULT_GATEWAY,
        attach=not args.no_attach,
        provision=not args.no_provision,
        fleet_config=None if args.no_credentials else Path(args.fleet_config).expanduser(),
        roles=([r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None),
        control_plane_url=args.control_plane_url,
    )
    if args.json:
        _print_json(report)
    else:
        print(f"herdr-eng fleet join {args.ssh_target}")
        for step, detail in report.get("steps", {}).items():
            ok = detail.get("ok", detail.get("added") is not False)
            status = "ok" if ok and not detail.get("error") else "warn"
            print(f"  [{status:>4}] {step}: {detail}")
        print(f"  registered with gateway: {report.get('registered')}")
        if report.get("errors"):
            for e in report["errors"]:
                print(f"  !! {e}")
    return 0 if report.get("ok") else 1


def _cmd_fleet_config(args) -> int:
    from pathlib import Path

    from . import fleet_config_cli as fcc
    from .fleet_config import FleetConfigError, load_fleet_config
    cmd = args.fleet_config_command
    path = Path(args.path).expanduser()
    try:
        if cmd == "init":
            if args.with_api_keys:
                print(fcc.API_KEYS_WARNING, file=sys.stderr)
            answers: dict = {}
            if args.from_env:
                imported, report = fcc.import_env_file(Path(args.from_env))
                answers.update(imported)
                # names only, never values
                for label, key in (("imported", "imported"), ("not used (unknown names)", "unknown"),
                                   ("empty, ignored", "empty"),
                                   ("token files not readable", "unreadable_files")):
                    if report[key]:
                        print(f"{args.from_env}: {label}: {', '.join(report[key])}", file=sys.stderr)
                if report["bad_lines"]:
                    print(f"{args.from_env}: skipped lines (not NAME=value): "
                          f"{', '.join(map(str, report['bad_lines']))}", file=sys.stderr)
                for dotted, state in report["tokens"].items():
                    print(f"{dotted}: {state}", file=sys.stderr)
            if args.answers:
                answers.update(fcc.load_answers_file(Path(args.answers)))
            fcc.init_config(path, answers, with_api_keys=args.with_api_keys,
                            optional_secrets=("github_token",) if args.from_env else ())
            print(f"wrote {path} (0600)")
        elif cmd == "validate":
            info = fcc.validate_config(path)
            print(f"ok revision={info['revision']} hosts={info['hosts']}")
        elif cmd == "token":
            rep = fcc.issue_host_credentials(path, args.host, args.target or args.host,
                                             control_plane_url=args.control_plane_url,
                                             must_exist=True)
            print(f"token for {rep['host']} installed on {rep['target']} (token: {rep['token']})")
        elif cmd == "revoke":
            backup = fcc.revoke_host(path, args.host)
            print(f"revoked {args.host} (backup: {backup})")
        elif cmd == "render-compose":
            fcc.render_compose(load_fleet_config(path), Path(args.out).expanduser())
            print(f"wrote {args.out} (0600)")
        elif cmd == "render-collab":
            fcc.render_collab(load_fleet_config(path), Path(args.out).expanduser())
            print(f"wrote {args.out} (0600)")
        elif cmd == "render-llm":
            fcc.render_llm(load_fleet_config(path), Path(args.out).expanduser())
            print(f"wrote {args.out} (0600)")
    except FleetConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_fleet_update(args) -> int:
    from .fleet import DEFAULT_GATEWAY, fleet_update, warn_gateway_ignored
    warn_gateway_ignored(args.gateway)
    report = fleet_update(
        host=args.host,
        gateway=DEFAULT_GATEWAY,
        pull_controller=not args.no_pull,
        reinstall_local=not args.no_reinstall,
    )
    if args.json:
        _print_json(report)
    else:
        print(f"herdr-eng fleet update ({report['target']})")
        for step, detail in report.get("steps", {}).items():
            ok = detail.get("ok", detail.get("rc") == 0)
            print(f"  [{'ok' if ok else 'warn':>4}] {step}: rc={detail.get('rc')}")
        if report.get("error"):
            print(f"  !! {report["error"]}")
        print("  --- fleet parity ---")
        for h in report.get("status", {}).get("hosts", []):
            print(f"  {h['host']:12} herdr-eng={h['herdr_eng']} build-gate={h['build_gate']} "
                  f"agent={h['agent']} revision={h.get('revision', 'unknown')} "
                  f"cleanup={h.get('cleanup', 'unknown')} summary={h.get('summary', 'unknown')}")
        fs = report.get("steps", {}).get("fleet_sync", {})
        if fs.get("rc") != 0 and fs.get("stderr"):
            print(f"  !! {fs['stderr']}")
    return 0 if report.get("ok") else 1


def _cmd_fleet_recover(args) -> int:
    from pathlib import Path

    from .fleet import repo_root
    from .fleet_rollout import inventory_targets
    from .pi_recovery import RecoveryStore, apply_recovery, create_plan

    store = RecoveryStore()
    try:
        if args.recovery_command in ("plan", "run"):
            inventory_value = args.inventory or os.environ.get("HERDR_PI_RECOVERY_INVENTORY")
            inventory = Path(inventory_value).expanduser() if inventory_value else repo_root() / "ansible/inventory/hosts.yml"
            targets = [{**target, "session": args.session} for target in inventory_targets(inventory, args.host)]
            report = create_plan(store, targets, progress=lambda event: print(event, file=sys.stderr, flush=True), revision=args.pi_revision or os.environ.get("HERDR_PI_RECOVERY_REVISION"), inventory=inventory)
            if args.recovery_command == "run":
                print("Recovery plan: " + report["id"], file=sys.stderr, flush=True)
                report = apply_recovery(store, report["id"], progress=lambda event: print(event, file=sys.stderr, flush=True))
        elif args.host or args.inventory or args.pi_revision or args.session != "default":
            raise ValueError("apply/status use the frozen plan targets; planning flags are not allowed")
        elif not args.id:
            raise ValueError("--id is required for apply/status")
        elif args.recovery_command == "apply":
            report = apply_recovery(store, args.id, progress=lambda event: print(event, file=sys.stderr, flush=True))
        else:
            from .pi_recovery_verification import verify_recovery
            report = verify_recovery(store, args.id, progress=lambda event: print(event, file=sys.stderr, flush=True))
    except (OSError, ValueError, RuntimeError) as exc:
        _print_json({"error": str(exc)})
        return 1
    _print_json(report)
    return 2 if report.get("status") == "needs_verification" else 0


def _cmd_fleet_rollout(args) -> int:
    from pathlib import Path

    from .fleet import repo_root
    from .fleet_rollout import (
        RolloutStore,
        apply_rollout,
        create_rollout,
        inventory_targets,
        run_rollout,
    )

    store = RolloutStore(Path(args.state) if args.state else None)
    try:
        if args.rollout_command == "plan":
            inventory = Path(args.inventory) if args.inventory else repo_root() / "ansible/inventory/hosts.yml"
            report = create_rollout(store, args.pi_revision, inventory_targets(inventory, args.host), inventory=inventory)
        elif args.rollout_command == "apply":
            report = apply_rollout(store, args.id)
        elif args.rollout_command == "run":
            report = run_rollout(store, args.id, interval_seconds=args.interval_seconds, max_passes=args.max_passes, max_unchanged_online_passes=args.max_unchanged_online_passes)
        else:
            report = store.read(args.id)
    except (OSError, ValueError, RuntimeError) as exc:
        _print_json({"error": str(exc)})
        return 1
    _print_json(report)
    if report.get("runner", {}).get("status") == "interrupted" and args.rollout_command == "run":
        return 130
    return 0 if args.rollout_command not in ("apply", "run") or report["status"] == "complete" else 2


def _cmd_fleet_drift(args) -> int:
    import urllib.request

    from .fleet import plan_version_sync
    from .host_config import HostConfigUnavailable, exit_unavailable, resolve_control_plane
    try:
        base = resolve_control_plane(args.control_plane)
    except HostConfigUnavailable as exc:
        return exit_unavailable(exc)
    try:
        with urllib.request.urlopen(f"{base}/api/v1/fleet/inventory", timeout=10) as response:
            matrix = json.loads(response.read(4 * 1024 * 1024))
    except (OSError, ValueError) as exc:
        print(f"could not read fleet inventory from {base}: {exc}", file=sys.stderr)
        return 1
    plan = [e for e in plan_version_sync(matrix) if not args.host or e["host"] == args.host]
    if args.json:
        _print_json({"hosts": [h["host"] for h in matrix.get("hosts", [])], "plan": plan})
        return 0
    if not plan:
        print("all reporting hosts match the fleet reference")
        return 0
    for host in sorted({e["host"] for e in plan}):
        print(f"{host}")
        for e in (x for x in plan if x["host"] == host):
            have = e["current"] or "not installed"
            print(f"  {e['component']:28} {have:>14} -> {e['target']}")
            if e.get("command"):
                print(f"      suggested: {e['command']}")
            elif e.get("note"):
                print(f"      note: {e['note']}")
    print("\nSuggestions only; nothing was run.")
    return 0


def _cmd_fleet_status(args) -> int:
    from .fleet import fleet_status
    status = fleet_status()
    if args.json:
        _print_json(status)
    else:
        for h in status.get("hosts", []):
            print(f"{h['host']:12} herdr-eng={h['herdr_eng']} build-gate={h['build_gate']} "
                  f"agent={h['agent']} revision={h.get('revision', 'unknown')} "
                  f"cleanup={h.get('cleanup', 'unknown')} summary={h.get('summary', 'unknown')}")
        if not status.get("hosts"):
            print(f"no hosts reported (playbook rc={status.get("rc")})")
    return 0 if status.get("rc") == 0 else 1


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


def _cmd_art_upload(args) -> int:
    import urllib.request
    from pathlib import Path
    from urllib.parse import urlencode

    from .host_config import HostConfigUnavailable, exit_unavailable, resolve_control_plane
    try:
        base = resolve_control_plane(args.control_plane)
    except HostConfigUnavailable as exc:
        return exit_unavailable(exc)
    path = Path(args.data)
    query = {"filename": args.filename or path.name, "mime": args.mime}
    if args.session:
        query["session_id"] = args.session
    if args.mission:
        query["mission_id"] = args.mission
    req = urllib.request.Request(f"{base}/api/v1/artifacts?{urlencode(query)}",
                                 data=path.read_bytes(), method="POST",
                                 headers={"Content-Type": args.mime})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.loads(response.read(8 * 1024 * 1024))
    except (OSError, ValueError) as exc:
        print(f"artifact upload failed: {exc}", file=sys.stderr)
        return 1
    _print_json(payload)
    return 0


def _cmd_graph_artifact_render(args) -> int:
    """Render a graph snapshot JSON file (GraphProjection.snapshot() shape) to
    a deterministic sanitized HTML artifact (system-graph P5).
    """
    import json as _json
    from pathlib import Path

    from .control_plane.graph_artifact import GraphArtifactRenderer

    data = _json.loads(Path(args.graph_json).read_text())
    result = GraphArtifactRenderer(
        title=data.get("title", "System graph snapshot")).render(data)
    out = args.out or f"{args.graph_json}.html"
    Path(out).write_text(result.html)
    _print_json({"ok": True, "out": out, "digest": result.digest,
                 "nodes": result.node_count, "edges": result.edge_count})
    return 0


def _cmd_art_upload(args) -> int:
    import urllib.request
    from pathlib import Path
    from urllib.parse import urlencode

    from .host_config import HostConfigUnavailable, exit_unavailable, resolve_control_plane

    try:
        base = resolve_control_plane(args.control_plane)
    except HostConfigUnavailable as exc:
        return exit_unavailable(exc)
    path = Path(args.data)
    query = {"filename": args.filename or path.name, "mime": args.mime}
    if args.session:
        query["session_id"] = args.session
    if args.mission:
        query["mission_id"] = args.mission
    req = urllib.request.Request(
        f"{base}/api/v1/artifacts?{urlencode(query)}", data=path.read_bytes(),
        method="POST", headers={"Content-Type": args.mime})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.loads(response.read(8 * 1024 * 1024))
    except (OSError, ValueError) as exc:
        print(f"artifact upload failed: {exc}", file=sys.stderr)
        return 1
    _print_json(payload)
    return 0


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


def _local_fabric_client():
    """Return a FabricClient when a gateway is configured AND reachable,
    else None (local/degraded mode)."""
    from .config import load_config
    from .errors import InvalidError
    from .fabric import FabricClient, gateway_url_from_config
    try:
        url = gateway_url_from_config(load_config())
    except InvalidError:
        return None
    if not url:
        return None
    try:
        FabricClient(url).healthz()
        return FabricClient(url)
    except Exception:
        return None  # gateway down: degrade to local mode, never break registration


def _gateway_configured_or_reachable() -> bool:
    """True when a gateway URL is configured (fabric.url in the central host
    config or fabric.gateway_url in config.yaml) OR a gateway is currently reachable.
    Used to warn about silent local-registry degradation (issue #77b)."""
    from .config import load_config
    from .errors import InvalidError
    from .fabric import FabricClient, gateway_url_from_config
    try:
        url = gateway_url_from_config(load_config())
    except InvalidError:
        url = None
    if url:
        return True  # configured (even if currently unreachable)
    # Not configured: check if one is somehow reachable (e.g. via env override)
    try:
        FabricClient("http://127.0.0.1:29999").healthz()
        return True
    except Exception:
        return False


def _fabric_url_resolver():
    """Resolver the dashboard uses for canonical fabric URLs; None when no gateway is configured."""
    from .config import load_config
    from .fabric import FabricClient, gateway_url_from_config
    url = gateway_url_from_config(load_config())
    if not url:
        return None
    client = FabricClient(url)
    return client.resolve_urls


def _fabric_service_lister():
    from .config import load_config
    from .fabric import FabricClient, gateway_url_from_config
    url = gateway_url_from_config(load_config())
    return FabricClient(url).list if url else None


def _resolve_target_host(host: str, port: int) -> str:
    """Resolve 'auto'/'local' to this machine's tailnet IP (so the gateway can
    route back to us). Verifies the local target is listening. Explicit hosts
    are passed through unchanged."""
    if host not in ("auto", "local"):
        return host
    from .errors import ConflictError
    from .fabric import tailnet_ipv4
    ts_ip = tailnet_ipv4()
    if ts_ip is None:
        raise ConflictError("--host auto requires tailscale (no tailnet IPv4 found)")
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except OSError as exc:
        raise ConflictError(f"target 127.0.0.1:{port} is not listening: {exc}")
    return ts_ip


def _parse_routes(specs):
    """Parse --route PREFIX=HOST:PORT specs into the wire format (list of dicts).

    Returns None when there are no routes (plain 1:1 service). The lease's own
    --host/--port is the catch-all; each route is a path prefix -> backend.
    """
    if not specs:
        return None
    routes = []
    for spec in specs:
        prefix, eq, hp = spec.partition("=")
        if not eq:
            raise ValueError(f"bad --route {spec!r} (want PREFIX=HOST:PORT)")
        host, colon, port = hp.rpartition(":")
        if not colon or not host:
            raise ValueError(f"bad --route backend {hp!r} (want HOST:PORT)")
        routes.append({"prefix": prefix, "host": host, "port": int(port)})
    return routes


def _cmd_dev_register(args) -> int:
    from .devfabric import DevServiceRegistry
    host = _resolve_target_host(args.host, args.port)
    try:
        routes = _parse_routes(args.route)
    except ValueError as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1
    client = _local_fabric_client()
    if client is not None:
        try:
            result = client.register(
                target_machine_id=args.machine, target_host=host,
                target_port=args.port, protocol=args.protocol,
                owner_herdr_session_id=args.session, owner_repository=args.repo,
                owner_worktree_id=args.worktree, owner_process_id=args.process,
                label=args.label, routes=routes,
                preferred_port=args.preferred_port)
            _print_json(result)
            return 0
        except Exception as exc:
            _print_json({"ok": False, "gateway": str(exc),
                         "note": "gateway unreachable — fell back to local registry",
                         "warning": "lease is NOT reachable via dev:<port> until the gateway is up"})
    lease = DevServiceRegistry().register(
        target_machine_id=args.machine, target_host=host, target_port=args.port,
        protocol=args.protocol, owner_herdr_session_id=args.session,
        owner_repository=args.repo, owner_worktree_id=args.worktree,
        owner_process_id=args.process, label=args.label, routes=routes,
        preferred_port=args.preferred_port)
    _print_json(lease.to_dict())
    return 0


def _load_origin_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("manifest must be a JSON object")
    return d


def _stable_origin_lease_id(machine: str, host: str, port: int, repo: str | None) -> str:
    """A deterministic lease id for an origin (stable across (re)enrolls and
    process restarts), derived from its identity. register() renews a lease by
    id, so re-enrolling the same origin RENEWS the same lease/port instead of
    creating a new one (the gateway's register is otherwise not target-deduped).
    This is what makes auto-enrollment durable + churn-free."""
    import hashlib
    key = f"{machine}:{host}:{int(port)}:{repo or ''}"
    return "lease_" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _gateway_bound_ports(client):
    """Set of external ports the gateway is currently SERVING, from healthz's
    bound_ports — the data plane's authoritative view. More reliable than /list
    on older gateway builds (whose /list can omit a lease that is still bound +
    serving). None if it can't be read."""
    if client is None:
        return None
    try:
        return set(client.healthz().get("bound_ports", []))
    except Exception:
        return None


def _local_lease_alive(reg, lease_id):
    """Whether the local registry still holds the lease (local-mode liveness)."""
    if not lease_id:
        return False
    try:
        return any(lease.id == lease_id for lease in reg.active_leases())
    except Exception:
        return True


def _routes_equal(a, b) -> bool:
    """Compare two route lists (Route objects or None), order-insensitive."""
    def norm(rs):
        return sorted((getattr(r, "prefix", None), getattr(r, "host", None),
                       getattr(r, "port", None)) for r in (rs or []))
    return norm(a) == norm(b)


def _spec_from_manifest(m: dict) -> dict:
    """A manifest dict -> an origin spec (no CLI-flag overrides)."""
    return {
        "origin_port": m.get("origin_port"),
        "machine": m.get("machine") or socket.gethostname(),
        "repo": m.get("repo"),
        "label": m.get("label"),
        "protocol": m.get("protocol", "http"),
        "catchall": dict(m.get("catchall") or {}),
        "routes": list(m.get("routes") or []),
    }


def heartbeat_local_leases(reg, state: dict) -> None:
    """Renew every locally-held lease (the --serve renewal loop, #89).

    Loud about the two ways a renewal can silently lose the origin: the
    lease is MISSING from the registry (clobbered or pruned by another
    process — the next sync re-enrolls it), and the lease LAPSED between
    cycles (expired while the forwarder still held the port — fleet-wide
    unreachable until this renewal). The lapsed lease is still renewed.
    """
    from .devfabric import _now_iso, _parse_time
    now = _parse_time(_now_iso())
    for key, st in state.items():
        lid = st["lease_id"]
        lease = reg.get(lid)
        lapsed = lease is not None and _parse_time(lease.expires_at) < now
        renewed = reg.heartbeat(lid)
        if renewed is None:
            logging.getLogger("herdr.cli").warning(
                "lease %s missing from the registry while the forwarder "
                "still serves it (clobbered or pruned); the next sync will "
                "re-enroll it", lid)
        elif lapsed:
            logging.getLogger("herdr.cli").warning(
                "lease %s lapsed (expired while the forwarder held the "
                "port); renewed now", lid)


def _ensure_origin_forwarder(fwd: dict, key: str, lease, bind: str):
    """Keep the per-origin forwarder running (local --serve mode). Returns an
    error string when (re)starting failed, else None.

    Restarts only when the lease identity or its 1:1 target moved — a
    routes-only drift needs no restart (OriginEdge reads lease.routes per
    connection). A failed start leaves the slot empty (None) so the NEXT
    sync retries instead of remembering the failure for the life of the
    process (#90).
    """
    from .devfabric import forwarder_for
    cur = fwd.get(key)
    if cur is not None:
        same = (cur.lease.id == lease.id
                and cur.lease.target_host == lease.target_host
                and cur.lease.target_port == lease.target_port
                and cur.lease.external_port == lease.external_port)
        if same:
            return None  # routes-only drift: the edge picks it up per-connection
        try:
            cur.stop()
        except Exception:
            pass
        fwd.pop(key, None)
    try:
        fwd[key] = forwarder_for(lease, bind_host=bind).start()
        return None
    except Exception as exc:  # noqa: BLE001 - report; next sync retries
        fwd[key] = None  # slot stays retryable
        return str(exc)


def _enroll_origin(spec: dict, client, reg, bind: str = "127.0.0.1"):
    """Register one origin (spec): gateway if reachable (it forwards fleet-wide),
    else the local registry. Idempotent via a STABLE lease_id (re-register
    renews the same lease/port, no new lease). Returns (out_dict, lease).
    Raises on registration failure."""
    from .contracts import Route
    c_host, c_port = spec["catchall"]["host"], int(spec["catchall"]["port"])
    origin_port = spec["origin_port"]
    stable_id = _stable_origin_lease_id(spec["machine"], c_host, c_port, spec["repo"])
    routes = [Route(r.get("prefix", "/"), r.get("host"), int(r.get("port")))
              if isinstance(r, dict) else r for r in spec["routes"]]
    routes_wire = [{"prefix": r.prefix, "host": r.host, "port": r.port}
                   for r in routes] or None
    if client is not None:
        # Idempotent: the stable lease_id makes the gateway's register RENEW the
        # existing lease (same port) instead of creating a new one per (re)enroll.
        result = client.register(
            target_machine_id=spec["machine"], target_host=c_host,
            target_port=c_port, protocol=spec["protocol"],
            owner_repository=spec["repo"], label=spec["label"],
            routes=routes_wire, preferred_port=origin_port, lease_id=stable_id)
        lease = result.get("lease", {})
        out = {"ok": True, "via": "gateway", "changed": True,
               "lease_id": lease.get("id"),
               "origin": result.get("url")
               or f"http://dev:{lease.get('external_port')}",
               "catchall": f"{c_host}:{c_port}",
               "routes": [f"{r['prefix']} -> {r['host']}:{r['port']}"
                          for r in (routes_wire or [])],
               "note": "the gateway forwards the origin automatically"}
        return out, lease
    # Local: register with the stable lease_id (idempotent — renews the same
    # lease/port). Route drift updates the lease IN PLACE (same id, same
    # external port) instead of close+re-register — the churn path re-
    # allocated the port (landing elsewhere when the old forwarder still
    # OS-binds it) and left the CLI with a stale origin; a running edge
    # picks up the new routes per connection (#90).
    existing = reg.get(stable_id)
    if existing is not None and _routes_equal(existing.routes, routes):
        lease = existing
        changed = False
    elif existing is not None:
        lease = reg.update_routes(stable_id, routes)
        if lease.state != "active":
            lease = reg.heartbeat(stable_id) or lease
        changed = True
    else:
        lease = reg.register(
            target_machine_id=spec["machine"], target_host=c_host,
            target_port=c_port, protocol=spec["protocol"],
            owner_repository=spec["repo"], label=spec["label"],
            routes=routes, preferred_port=origin_port, lease_id=stable_id)
        changed = True
    out = {"ok": True, "via": "local", "changed": changed, "lease_id": lease.id,
           "origin": f"http://dev:{lease.external_port}",
           "local_origin": f"http://{bind}:{lease.external_port}",
           "catchall": f"{c_host}:{c_port}",
           "routes": [f"{r.prefix} -> {r.host}:{r.port}"
                      for r in (lease.routes or [])]}
    if lease.external_port != origin_port:
        out["warning"] = (f"origin port {origin_port} in use; "
                          f"allocated {lease.external_port}")
    return out, lease


def _maybe_git_pull(directory: str) -> None:
    """Best-effort 'git pull --ff-only' of a fleet-origins repo. No-op if the
    dir isn't a git repo or the pull fails (the local dir is always the source
    of truth). Lets a new committed origin.json auto-enroll without a manual pull."""
    import subprocess
    try:
        subprocess.run(["git", "-C", directory, "pull", "--ff-only", "--quiet"],
                       check=False, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _load_origin_manifests(directory: str) -> dict:
    """All *.json origin manifests in directory -> {file_stem: spec}."""
    import pathlib
    d = pathlib.Path(directory)
    if not d.is_dir():
        raise ValueError(f"not a directory: {directory}")
    out = {}
    for p in sorted(d.glob("*.json")):
        try:
            spec = _spec_from_manifest(_load_origin_manifest(str(p)))
        except (OSError, ValueError) as exc:
            print(f"  skip {p.name}: {exc}", file=sys.stderr)
            continue
        if (not spec["origin_port"] or not spec["catchall"].get("host")
                or not spec["catchall"].get("port")):
            print(f"  skip {p.name}: missing origin_port or catchall", file=sys.stderr)
            continue
        out[p.stem] = spec
    return out


def _origin_signature(spec: dict) -> str:
    """A stable fingerprint of a spec, to detect changes across syncs."""
    routes = [sorted(r.items()) if isinstance(r, dict) else r
              for r in spec["routes"]]
    return json.dumps({"o": spec["origin_port"], "m": spec["machine"],
                       "c": spec["catchall"], "r": routes}, sort_keys=True)


def _cmd_dev_enroll_sync(args) -> int:
    """Auto-enroll every origin manifest in --dir (idempotent). With a reachable
    gateway this registers them all — one registration serves every machine on
    dev.lan (the gateway owns the ports and auto-restarts). Without a gateway it
    registers locally and, with --serve, runs a per-host forwarder per origin.
    --watch re-syncs every --interval seconds so NEW/CHANGED manifests auto-enroll
    and removed ones are reaped — the 'any new project auto-enrolled' engine (see
    the fabric-enroll skill, Automatic enrollment)."""
    import signal
    import time

    from .devfabric import DevServiceRegistry
    reg = DevServiceRegistry()
    client = _local_fabric_client()
    if client is None:
        # --require-gateway: fail hard instead of silently degrading (issue #77b).
        if getattr(args, "require_gateway", False):
            _print_json({"ok": False, "error": "--require-gateway set but the "
                        "gateway is not reachable; refusing to fall back to "
                        "the local registry"})
            return 1
        if _gateway_configured_or_reachable():
            import sys as _sys
            print(
                "WARNING: enrolling via the LOCAL registry while a gateway "
                "URL is configured or reachable. Origins will NOT be visible "
                "to other hosts on the dev.lan fabric. Start the gateway or "
                "set --require-gateway to make this a hard error.",
                file=_sys.stderr,
            )
    try:
        _load_origin_manifests(args.dir)  # validate early
    except ValueError as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1
    state: dict = {}    # key -> {"sig": str, "lease_id": str}
    fwd: dict = {}      # key -> forwarder            [local serve mode]
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))

    def _stop_fwd(key):
        cur = fwd.pop(key, None)
        if cur is not None:
            try:
                cur.stop()
            except Exception:
                pass

    def _heartbeat_local():
        if client is None:
            try:
                heartbeat_local_leases(reg, state)
            except Exception:
                pass

    def sync_once() -> list:
        if args.git_pull:
            _maybe_git_pull(args.dir)
        specs = _load_origin_manifests(args.dir)
        bound = _gateway_bound_ports(client)  # None = can't verify
        results = []
        for key, spec in specs.items():
            sig = _origin_signature(spec)
            prev = state.get(key)
            # Self-healing: skip only if the manifest is unchanged AND its origin
            # port is still bound + serving by the gateway (the data plane's
            # authoritative view). A dropped lease (e.g. after a gateway restart)
            # is re-enrolled even though the manifest didn't change — so the
            # origin always recovers, not just on a manifest edit.
            if client is not None:
                prev_port = prev.get("port") if prev else None
                lease_alive = (bound is None) or (prev_port is not None and prev_port in bound)
            else:
                lease_alive = _local_lease_alive(
                    reg, prev.get("lease_id") if prev else None)
            if prev is not None and prev.get("sig") == sig and lease_alive:
                continue  # unchanged + still serving -> idempotent
            try:
                out, lease = _enroll_origin(spec, client, reg, bind=args.bind)
            except Exception as exc:  # noqa: BLE001 - report, keep syncing
                results.append({"manifest": key, "ok": False, "error": str(exc)})
                continue
            out["manifest"] = key
            results.append(out)
            lid = lease.get("id") if isinstance(lease, dict) else lease.id
            port = lease.get("external_port") if isinstance(lease, dict) \
                else lease.external_port
            state[key] = {"sig": sig, "lease_id": lid, "port": port}
            if args.serve and client is None:
                serve_err = _ensure_origin_forwarder(fwd, key, lease, args.bind)
                if serve_err is not None:
                    out["serve_error"] = serve_err
        for key in list(state):  # reap removed manifests
            if key not in specs:
                _stop_fwd(key)
                if client is not None:  # close the gateway lease, too
                    try:
                        client.close(state[key]["lease_id"])
                    except Exception:
                        pass
                state.pop(key, None)
                results.append({"manifest": key, "ok": True, "removed": True})
        return results

    results = sync_once()
    _heartbeat_local()
    _print_json({
        "ok": True, "dir": args.dir, "watch": args.watch,
        "gateway": client is not None, "serve": args.serve,
        "enrolled": [r["manifest"] for r in results
                     if r.get("ok") and r.get("changed") and not r.get("removed")],
        "unchanged": [r["manifest"] for r in results
                      if r.get("ok") and not r.get("changed") and not r.get("removed")],
        "results": results,
    })
    if not args.watch:
        if args.serve and client is None:
            _print_json({"ok": True, "serving": len(fwd), "note": "forwarding (Ctrl-C to stop)"})
            while not stop["flag"]:
                time.sleep(0.3)
                _heartbeat_local()
        return 0
    while not stop["flag"]:
        end = time.monotonic() + args.interval
        while not stop["flag"] and time.monotonic() < end:
            time.sleep(0.5)
            _heartbeat_local()
        if stop["flag"]:
            break
        sync_once()
        _heartbeat_local()
    for key in list(fwd):
        _stop_fwd(key)
    return 0


def _cmd_dev_enroll(args) -> int:
    """Enroll a same-origin app edge: register the origin lease (catch-all +
    path routes) in the fabric and, with --serve, run the per-host forwarder.

    This is the 'sets everything up' entry point (see the fabric-enroll skill).
    With a reachable gateway the origin is forwarded automatically; otherwise
    --serve runs the forwarder locally.
    """
    from .contracts import Route
    from .devfabric import DevServiceRegistry, forwarder_for
    from .errors import ConflictError

    m = {}
    if args.manifest:
        try:
            m = _load_origin_manifest(args.manifest)
        except (OSError, ValueError) as exc:
            _print_json({"ok": False, "error": f"manifest: {exc}"})
            return 1

    origin_port = args.origin_port or m.get("origin_port")
    machine = args.machine or m.get("machine") or socket.gethostname()
    repo = args.repo or m.get("repo")
    label = args.label or m.get("label")
    protocol = args.protocol or m.get("protocol", "http")
    catchall = m.get("catchall") or {}
    routes_in = m.get("routes") or []
    if args.catchall:
        ch, _, cp = args.catchall.rpartition(":")
        catchall = {"host": ch, "port": int(cp)}
    if args.route:
        routes_in = _parse_routes(args.route) or []

    if not origin_port:
        _print_json({"ok": False, "error": "missing origin port (--origin-port or manifest.origin_port)"})
        return 1
    if not catchall.get("host") or not catchall.get("port"):
        _print_json({"ok": False, "error": "missing catch-all backend (--catchall HOST:PORT or manifest.catchall)"})
        return 1
    c_host, c_port = catchall["host"], int(catchall["port"])
    routes = [Route(r.get("prefix", "/"), r.get("host"), int(r.get("port")))
              if isinstance(r, dict) else r for r in routes_in]
    routes_wire = [{"prefix": r.prefix, "host": r.host, "port": r.port} for r in routes] or None

    client = _local_fabric_client()
    if client is not None:
        stable_id = _stable_origin_lease_id(machine, c_host, c_port, repo)
        try:
            result = client.register(
                target_machine_id=machine, target_host=c_host, target_port=c_port,
                protocol=protocol, owner_repository=repo, label=label,
                routes=routes_wire, preferred_port=origin_port, lease_id=stable_id)
        except Exception as exc:
            _print_json({"ok": False, "error": f"gateway register failed: {exc}"})
            return 1
        lease = result.get("lease", {})
        _print_json({
            "ok": True, "via": "gateway", "lease_id": lease.get("id"),
            "origin": result.get("url")
            or f"http://dev:{lease.get('external_port')}",
            "catchall": f"{c_host}:{c_port}",
            "routes": [f"{r['prefix']} -> {r['host']}:{r['port']}" for r in (routes_wire or [])],
            "note": "the gateway forwards the origin automatically",
        })
        return 0

    # --require-gateway: the caller explicitly wants the central gateway; do not
    # silently degrade to the local registry (issue #77b).
    if getattr(args, "require_gateway", False):
        _print_json({"ok": False, "error": "--require-gateway set but the gateway "
                    "is not reachable; refusing to fall back to the local registry"})
        return 1

    # Local mode: apply (re)routes by closing any stale lease for the target,
    # then register fresh (preferred_port keeps the origin port stable).
    reg = DevServiceRegistry()

    # Issue #77b: a gateway URL is configured/reachable but we fell back to the
    # local registry — this is a footgun (the origin won't be visible to the
    # fleet). Print a loud warning so the operator notices.
    if _gateway_configured_or_reachable():
        import sys as _sys
        print(
            "WARNING: enrolling via the LOCAL registry while a gateway URL is "
            "configured or reachable. The origin will NOT be visible to other "
            "hosts on the dev.lan fabric. Start the gateway or set "
            "--require-gateway to make this a hard error.",
            file=_sys.stderr,
        )
    stale = next((cand for cand in reg.active_leases()
                  if cand.target_machine_id == machine and cand.target_host == c_host
                  and cand.target_port == c_port), None)
    if stale is not None:
        reg.close(stale.id)
    lease = reg.register(
        target_machine_id=machine, target_host=c_host, target_port=c_port,
        protocol=protocol, owner_repository=repo, label=label,
        routes=routes, preferred_port=origin_port)
    origin = f"http://dev:{lease.external_port}"
    out = {
        "ok": True, "via": "local", "lease_id": lease.id, "origin": origin,
        "local_origin": f"http://{args.bind}:{lease.external_port}",
        "catchall": f"{c_host}:{c_port}",
        "routes": [f"{r.prefix} -> {r.host}:{r.port}" for r in (lease.routes or [])],
    }
    if lease.external_port != origin_port:
        out["warning"] = (f"origin port {origin_port} was in use; allocated "
                          f"{lease.external_port} instead")
    if not args.serve:
        out["next"] = f"herdr-eng devfabric serve --lease {lease.id}"
    _print_json(out)
    if not args.serve:
        return 0

    try:
        forwarder_for(lease, bind_host=args.bind).start()
    except ConflictError as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1
    _print_json({"ok": True, "serving": f"http://{args.bind}:{lease.external_port}",
                 "note": "forwarding the origin (Ctrl-C to stop)"})
    import signal
    import time
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    next_hb = time.monotonic()
    while not stop["flag"]:
        time.sleep(0.2)
        # Keep the lease active (default TTL ~90s) so the origin stays a live
        # fabric member (visible in `devfabric list`, kept by a central gateway).
        if time.monotonic() >= next_hb:
            try:
                reg.heartbeat(lease.id)
            except Exception:
                pass
            next_hb = time.monotonic() + 30
    return 0


def _cmd_dev_heartbeat(args) -> int:
    from .devfabric import DevServiceRegistry
    client = _local_fabric_client()
    if client is not None:
        _print_json(client.heartbeat(args.lease_id))
        return 0
    lease = DevServiceRegistry().heartbeat(args.lease_id)
    _print_json({"ok": lease is not None,
                 "lease": lease.to_dict() if lease else None})
    return 0


def _cmd_dev_close(args) -> int:
    from .devfabric import DevServiceRegistry
    client = _local_fabric_client()
    if client is not None:
        _print_json(client.close(args.lease_id))
        return 0
    ok = DevServiceRegistry().close(args.lease_id)
    _print_json({"ok": ok})
    return 0


def _cmd_dev_list(args) -> int:
    from .devfabric import DevServiceRegistry
    client = _local_fabric_client()
    if client is not None:
        _print_json(client.list())
        return 0
    _print_json([lease.to_dict() for lease in DevServiceRegistry().active_leases()])
    return 0


def _ca_dir(args):
    from pathlib import Path
    return Path(args.dir).expanduser() if args.dir else Path.home() / ".config" / "herdr-engineering" / "ca"


def _cmd_dev_ca(args) -> int:
    from .fabric_tls import CAError, PrivateCA, fingerprint_sha256
    ca = PrivateCA(_ca_dir(args))
    try:
        if args.ca_cmd == "init":
            path = ca.init(args.domain or ["dev.lan"])
            _print_json({"ok": True, "ca": str(path), "domains": ca.domains(),
                         "sha256": fingerprint_sha256(path),
                         "next": "devfabric ca issue, then devfabric ca export and enroll each device"})
        elif args.ca_cmd == "issue":
            names = args.name or [d for dom in ca.domains() for d in (dom, f"*.{dom}")]
            cert, key = ca.issue(names)
            _print_json({"ok": True, "cert": str(cert), "key": str(key), "names": names,
                         "expires": ca.expires(cert).isoformat(),
                         "next": f"run the gateway with --tls-cert {cert} --tls-key {key}"})
        elif args.ca_cmd == "export":
            from pathlib import Path
            out = Path(args.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            pem, prof = out / "fabric-ca.pem", out / "fabric-ca.mobileconfig"
            pem.write_text(ca.cert_path.read_text())
            prof.write_bytes(ca.mobileconfig())
            _print_json({"ok": True, "pem": str(pem), "mobileconfig": str(prof),
                         "sha256": fingerprint_sha256(ca.cert_path),
                         "note": "public certificate only; the CA key never leaves its directory"})
        else:
            gw = ca.dir / "gateway.pem"
            _print_json({"ca": ca.exists(), "domains": ca.domains(),
                         "sha256": fingerprint_sha256(ca.cert_path) if ca.exists() else None,
                         "gateway_cert": str(gw) if gw.exists() else None,
                         "gateway_expires": ca.expires(gw).isoformat() if gw.exists() else None,
                         "renewal_due": ca.needs_renewal(gw) if gw.exists() else None})
    except CAError as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1
    return 0


def _fabric_cfg() -> dict:
    from .config import InvalidError, load_config
    try:
        return load_config().get("fabric") or {}
    except InvalidError:
        return {}


def _acme_for(args, cfg: dict):
    from pathlib import Path

    from .fabric_certs import AcmeRoute53
    domain = getattr(args, "domain", None) or getattr(args, "acme_domain", None) or cfg.get("acme_domain")
    if not domain:
        raise SystemExit("no domain: pass --domain/--acme-domain or set fabric.acme_domain")
    directory = getattr(args, "dir", None) or getattr(args, "acme_dir", None) or cfg.get("acme_dir") \
        or Path.home() / ".config" / "herdr-engineering" / "acme"
    return AcmeRoute53(domain, Path(directory), staging=bool(getattr(args, "staging", False) or getattr(args, "acme_staging", False) or cfg.get("acme_staging")),
                       email=getattr(args, "email", None) or cfg.get("acme_email") or None)


def _route(spec) -> tuple[str, tuple[str, int]]:
    host, sep, upstream = str(spec).partition("=")
    target = _host_port(upstream)
    if not sep or not host or target is None:
        raise SystemExit(f"--route must be HOST=UPSTREAM:PORT, got {spec!r}")
    return host.strip().lower(), target


def _host_port(value) -> tuple[str, int] | None:
    if not value:
        return None
    host, _, port = str(value).rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise SystemExit(f"--apex-upstream must be HOST:PORT, got {value!r}")
    return host, int(port)


def _private_ca_domains(cfg: dict) -> list[str]:
    from pathlib import Path

    from .fabric_tls import PrivateCA
    ca = PrivateCA(Path(cfg.get("ca_dir") or Path.home() / ".config" / "herdr-engineering" / "ca"))
    return ca.domains() if ca.exists() else []


def _fabric_names_getter():
    from .config import load_config
    from .fabric import FabricClient, gateway_url_from_config
    url = gateway_url_from_config(load_config())
    return FabricClient(url).names if url else None


def _cmd_dev_cert(args) -> int:
    from .fabric_certs import CertError, CertManager
    acme = _acme_for(args, _fabric_cfg())
    try:
        if args.cert_cmd == "status":
            _print_json(CertManager(public=acme).status() if acme.fullchain.exists()
                        else {"public": None, "note": f"no certificate yet: run `devfabric cert issue --domain {acme.domain}`"})
        elif args.cert_cmd == "issue":
            changed = acme.obtain(force=args.force)
            _print_json({"ok": True, "changed": changed, "fullchain": str(acme.fullchain), "names": list(acme.names),
                         "staging": acme.staging, **CertManager(public=acme).status().get("public", {})})
        else:
            if not acme.due():
                _print_json({"ok": True, "changed": False, "note": "not due"})
            else:
                _print_json({"ok": True, "changed": acme.obtain()})
    except CertError as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1
    return 0


def _cmd_dev_dns(args) -> int:
    from .fabric import tailnet_ipv4
    from .fabric_dns import DnsError, publish, route53_client
    cfg = _fabric_cfg()
    domain = args.domain or cfg.get("acme_domain")
    if not domain:
        raise SystemExit("no domain: pass --domain or set fabric.acme_domain")
    ips = args.ip or ([tailnet_ipv4()] if tailnet_ipv4() else [])
    try:
        _print_json({"ok": True, **publish(route53_client(), domain, ips, ttl=args.ttl, dry_run=args.dry_run, zone_id=args.zone_id)})
    except DnsError as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1
    return 0


def _cmd_dev_gateway(args) -> int:
    import signal
    import time

    from .config import InvalidError, load_config
    from .devfabric import DevServiceRegistry
    from .fabric import FabricGateway, GatewayRouter, tailnet_ipv4
    try:
        cfg = load_config().get("fabric") or {}
    except InvalidError:
        cfg = {}
    control_port = args.control_port or cfg.get("control_port", 29999)
    if args.bind:
        bind_hosts = [h.strip() for h in args.bind.split(",") if h.strip()]
    elif cfg.get("bind") == "auto":
        bind_hosts = ["127.0.0.1", tailnet_ipv4() or "127.0.0.1"]
    else:
        bind_hosts = ["127.0.0.1"]
    bind_hosts = [h for h in dict.fromkeys(bind_hosts) if h]
    # Control API must be reachable from other tailnet hosts (they register
    # through it): prefer the tailnet IP; loopback-only when no tailnet.
    control_host = next((h for h in bind_hosts if h != "127.0.0.1"), "127.0.0.1")
    acme_domain = getattr(args, "acme_domain", None) or cfg.get("acme_domain") or None
    registry = DevServiceRegistry()
    router = GatewayRouter(bind_hosts)
    gateway = FabricGateway(
        registry, router, control_host=control_host, control_port=control_port,
        url_base=args.url_base or (cfg.get("url_base") or None) or acme_domain,
        probe_interval=float(cfg.get("probe_interval_seconds", 15)),
        reset_lock=bool(getattr(args, "reset_lock", False)),
        rewrite_headers=bool(getattr(args, "rewrite_headers", False)),
        peer_url=getattr(args, "peer", None) or cfg.get("peer_url") or None,
        node_id=getattr(args, "node_id", None) or cfg.get("node_id") or None,
        promote_after=int(getattr(args, "promote_after", 3)),
        named_domain=None if getattr(args, "no_named_urls", False) else acme_domain,
        named_port=int(getattr(args, "named_port", None) or cfg.get("named_port") or 443),
        host_routes=dict(_route(r) for r in [*getattr(args, "route", []), *(cfg.get("routes") or [])]),
        apex_upstream=_host_port(getattr(args, "apex_upstream", None) or cfg.get("apex_upstream")),
        url_aliases=tuple(dict.fromkeys([*getattr(args, "url_alias", []), *(cfg.get("url_aliases") or []),
                                         *_private_ca_domains(cfg)])))
    tls_cert = getattr(args, "tls_cert", None) or cfg.get("tls_cert")
    tls_key = getattr(args, "tls_key", None) or cfg.get("tls_key")
    if bool(tls_cert) != bool(tls_key):
        raise SystemExit("--tls-cert and --tls-key must be given together")
    from pathlib import Path
    cert_stop = None
    if acme_domain:
        import threading

        from .fabric_certs import CertError, CertManager, TailscaleCert
        from .fabric_tls import PrivateCA
        ca = PrivateCA(Path(cfg.get("ca_dir") or Path.home() / ".config" / "herdr-engineering" / "ca"))
        mgr = CertManager(public=_acme_for(args, cfg), private_ca=ca if (ca.dir / "gateway.pem").exists() else None,
                          private_names=tuple(ca.domains() or ["dev.lan"]),
                          tailnet=TailscaleCert(ts_name, Path(cfg.get("tailnet_cert_dir") or Path.home() / ".config" / "herdr-engineering" / "tailnet-cert"))
                          if (ts_name := getattr(args, "tailscale_cert", None) or cfg.get("tailscale_cert")) else None,
                          renew=bool(getattr(args, "renew_certs", False) or cfg.get("renew_certs")),
                          on_change=gateway.router.set_tls)
        try:
            if mgr.tailnet and not mgr.tailnet.fullchain.exists():
                mgr.tailnet.obtain()
            gateway.enable_tls(mgr.load())
        except CertError as exc:
            raise SystemExit(f"{exc}\n(or drop --acme-domain to serve plain HTTP)") from exc
        tls_cert = tls_cert or "acme"
        cert_stop = threading.Event()
        threading.Thread(target=mgr.run_forever, args=(cert_stop,), daemon=True, name="cert-renewal").start()
    elif tls_cert:
        import ssl as _ssl
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        ctx.set_alpn_protocols(["http/1.1"])
        gateway.enable_tls(ctx)
    acl = cfg.get("access") or {}
    users = [*args.allow_user, *(acl.get("users") or [])]
    tags = [*args.allow_tag, *(acl.get("tags") or [])]
    if users or tags:
        from .fabric_access import AccessPolicy
        router.access = AccessPolicy(users=frozenset(users), tags=frozenset(tags))
    gateway.start()
    _print_json({
        "ok": True, "gateway": True, "https": bool(tls_cert), "role": gateway.role, "peer": gateway.peer_url, "access_enforced": bool(users or tags),
        "control_url": f"http://{gateway.control_host}:{control_port}",
        "url_base": gateway.url_base,
        "named_urls": (f"https://<service>.{gateway.named_domain}" + ("" if gateway.named_port == 443 else f":{gateway.named_port}"))
                      if gateway.named_front else None,
        "bind_hosts": bind_hosts,
        "probe_interval_seconds": gateway.probe_interval,
        "note": f"dev:<port> resolves to {gateway.public_scheme}://{gateway.url_base}:<port>"
                f"{' (plain http also accepted on the same port)' if tls_cert else ''}",
    })
    stop = {"flag": False}

    def _on_sigint(_signum, _frame) -> None:
        stop["flag"] = True
    signal.signal(signal.SIGINT, _on_sigint)
    # Docker/compose stop sends SIGTERM; handle it so the gateway releases its
    # lock (gateway.stop() -> _release_lock) and exits cleanly instead of
    # leaving a stale lock that blocks the next container start.
    signal.signal(signal.SIGTERM, _on_sigint)
    try:
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        if cert_stop:
            cert_stop.set()
        gateway.stop()
        _print_json({"stopped": True})
    return 0


def _cmd_build_coordinate(args) -> int:
    """Run a build command under the per-host single-flight build gate.

    Bounds the number of concurrent ``cargo``/``tsc``/build jobs across all
    autonomous agents on a host, so N agents can't each launch a full-workspace
    build at once and CPU-starve the machine (the "build storm").
    """
    from .build_coord import BuildGate, _default_slots, run_gated

    slots = args.slots if args.slots is not None else _default_slots()
    if getattr(args, "status", False):
        gate = BuildGate(slots=slots)
        if args.json:
            _print_json({"slots": slots, "occupancy": gate.status()})
        else:
            for entry in gate.status():
                if entry["held"]:
                    h = entry["holder"]
                    print(f"  slot {entry['slot']}: held by pid {h.get('pid')} "
                          f"({h.get('host')}) since {h.get('start')} "
                          f"cmd={h.get('cmd')}")
                else:
                    print(f"  slot {entry['slot']}: free")
        return 0

    cmd = args.command
    if cmd and cmd[0] == "--":  # argparse REMAINDER keeps a leading `--`
        cmd = cmd[1:]
    return run_gated(cmd, slots=slots, max_load=args.max_load,
                     wait_seconds=args.wait, json_out=args.json,
                     bypass_on_timeout=args.bypass_on_timeout)


def _cmd_build_cleanup(args) -> int:
    from .build_maintenance import command
    return command(args)


def _cmd_dev_agent(args) -> int:
    """Run the per-host auto-registration agent (spec 0110)."""
    import logging
    import os
    from pathlib import Path

    from .config import InvalidError, load_config
    from .fabric import FabricClient, gateway_url_from_config, tailnet_ipv4
    from .fabric_agent import AgentConfig, FabricAgent, parse_agent_binaries
    from .host_config import ConfigRefresher, HostConfigUnavailable, exit_unavailable, host_config

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        live = host_config()
    except HostConfigUnavailable as exc:
        return exit_unavailable(exc)
    try:
        cfg = load_config()
    except InvalidError as exc:
        _print_json({"code": "config", "message": str(exc)})
        return 1
    base = gateway_url_from_config(cfg)
    if not base:
        _print_json({
            "code": "config",
            "message": "no gateway configured: set fabric.url in the central "
                       "fleet-config.yaml, or fabric.gateway_url in config.yaml",
        })
        return 1
    tailnet_ip = tailnet_ipv4()
    if not tailnet_ip:
        _print_json({
            "code": "config",
            "message": "no tailnet IPv4 found (is tailscale up?)",
        })
        return 1
    fabric_cfg = cfg.get("fabric") or {}
    web_port = int((cfg.get("web") or {}).get("port", 8787))
    control_port = int(fabric_cfg.get("control_port", 29999))
    state_dir = Path(os.environ.get(
        "HERDR_ENGINEERING_STATE",
        str(Path.home() / ".local/share/herdr-engineering/state")))
    # Noise filter knobs (all optional, sensible defaults in AgentConfig):
    # extra excluded ports + the dev-port window the agent will register.
    extra_exclude = frozenset(int(p) for p in fabric_cfg.get(
        "agent_exclude_ports", []))
    try:
        agent_binaries = parse_agent_binaries(fabric_cfg.get("agent_binaries"))
    except ValueError as exc:
        _print_json({"code": "config", "message": str(exc)})
        return 1
    def control_plane_for(host_cfg):
        return fabric_cfg.get("control_plane_url") or host_cfg.control_plane_url

    agent = FabricAgent(
        client=FabricClient(base),
        config=AgentConfig(
            tailnet_ip=tailnet_ip,
            store_path=state_dir / "fabric-agent.json",
            exclude_ports=frozenset({web_port, control_port}) | extra_exclude,
            label_prefix=args.label_prefix,
            interval=args.interval,
            web_port_min=int(fabric_cfg.get("agent_web_port_min", 1024)),
            ephemeral_min=int(fabric_cfg.get("agent_ephemeral_min", 32768)),
            control_plane_url=control_plane_for(live),
            fabric_domains=tuple(dict.fromkeys([d for d in (fabric_cfg.get("acme_domain"), fabric_cfg.get("url_base"),
                                                            *(fabric_cfg.get("url_aliases") or [])) if d])),
            host_report_interval=float(fabric_cfg.get("host_report_interval", 30.0)),
            host_slow_interval=float(fabric_cfg.get("host_slow_interval", 3600.0)),
            agent_binaries=agent_binaries,
        ))
    if args.once:
        summary = agent.scan_once()
        agent.stop()
        _print_json({"ok": True, **summary})
        return 0
    refresher = ConfigRefresher(live)
    seen = {"fabric": (live.get("fabric.url"), live.get("fabric.token")), "cp": control_plane_for(live)}

    def follow_config() -> None:
        """Rebuild the gateway client / control-plane URL when a fresh fetch changed them."""
        fresh = refresher.current()  # raises HostConfigUnavailable(revoked=True)
        fab = (fresh.get("fabric.url"), fresh.get("fabric.token"))
        cp = control_plane_for(fresh)
        if fab == seen["fabric"] and cp == seen["cp"]:
            return
        new_base = gateway_url_from_config(cfg, fabric_url=fab[0] or "") or base
        agent.retarget(FabricClient(new_base, fab[1] or None), cp)
        seen["fabric"], seen["cp"] = fab, cp

    try:
        agent.run(after_cycle=follow_config)
    except HostConfigUnavailable as exc:
        return exit_unavailable(exc)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
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


def _cmd_dev_gc(args) -> int:
    from .devfabric import DevServiceRegistry
    _print_json(DevServiceRegistry().gc())
    return 0


def _cmd_dev_serve(args) -> int:
    import signal
    import time

    from .devfabric import DevServiceRegistry, forwarder_for
    from .errors import ConflictError
    lease = DevServiceRegistry().get(args.lease_id)
    if lease is None:
        _print_json({"ok": False, "error": f"unknown lease {args.lease_id}"})
        return 1
    try:
        fwd = forwarder_for(lease, bind_host=args.bind).start()
    except ConflictError as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 1
    out = {
        "ok": True,
        "lease_id": lease.id,
        "dev_address": f"http://dev:{lease.external_port}",
        "local_address": f"http://{args.bind}:{lease.external_port}",
        "target": f"{lease.target_host}:{lease.target_port}",
        "protocol": lease.protocol,
    }
    if lease.routes:
        out["origin"] = True
        out["routes"] = [f"{r.prefix} -> {r.host}:{r.port}" for r in lease.routes]
    _print_json(out)
    stop = {"flag": False}

    def _on_sigint(_signum, _frame) -> None:
        stop["flag"] = True
    signal.signal(signal.SIGINT, _on_sigint)
    try:
        deadline = time.monotonic() + args.timeout if args.timeout else None
        while not stop["flag"]:
            if deadline is not None and time.monotonic() > deadline:
                break
            time.sleep(0.2)
    finally:
        fwd.stop()
        _print_json({"stopped": True, "lease_id": lease.id})
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


def _ci_provider():
    from .ci import WoodpeckerProvider
    from .config import load_config
    try:
        base = (load_config().get("ci") or {}).get("base_url")
    except Exception:
        base = None
    return WoodpeckerProvider(base_url=base)


def _ci_offline() -> int:
    _print_json({"available": False, "repos": [], "pipelines": [],
                 "note": "CI provider offline — ci.token is not in the central "
                         "host config (see `herdr-eng fleet join`). Stale view, "
                         "never guessed success."})
    return 0


def _cmd_ci_repos(args) -> int:
    provider = _ci_provider()
    if not provider.available:
        return _ci_offline()
    try:
        _print_json({"available": True, "base_url": provider.base_url,
                     "repos": provider.repos()})
    except Exception as exc:
        _print_json({"available": False, "error": str(exc)})
        return 1
    return 0


def _cmd_ci_agents(args) -> int:
    provider = _ci_provider()
    if not provider.available:
        return _ci_offline()
    try:
        _print_json({"available": True, "base_url": provider.base_url,
                     "agents": provider.agents()})
    except Exception as exc:
        _print_json({"available": False, "error": str(exc)})
        return 1
    return 0


def _cmd_ci_pipelines(args) -> int:
    provider = _ci_provider()
    if not provider.available:
        return _ci_offline()
    try:
        views = provider.pipelines(args.repo)
    except Exception as exc:
        _print_json({"available": False, "error": str(exc)})
        return 1
    _print_json({"available": True, "pipelines": [v.to_dict() for v in views]})
    return 0


def _cmd_ci_pipeline(args) -> int:
    provider = _ci_provider()
    if not provider.available:
        return _ci_offline()
    try:
        detail = provider.pipeline(args.repo, args.number)
    except Exception as exc:
        _print_json({"available": False, "error": str(exc)})
        return 1
    if detail is None:
        _print_json({"available": False,
                     "note": f"pipeline {args.number} not found for {args.repo}"})
        return 1
    _print_json({"available": True, "pipeline": detail})
    return 0


def _cmd_ci_logs(args) -> int:
    provider = _ci_provider()
    if not provider.available:
        return _ci_offline()
    try:
        log = provider.step_log(args.repo, args.pipeline, args.step)
    except Exception as exc:
        _print_json({"available": False, "error": str(exc)})
        return 1
    if log is not None:
        print(log)
        return 0
    # Honest degraded view: this Woodpecker 3.x build streams logs over
    # WebSocket (no REST log endpoint), and the OAuth2 proxy only forwards
    # an interactive browser session — not a bearer token — on the upgrade.
    _print_json({
        "available": True, "log": None,
        "note": "step log not available via the API on this instance. "
                "Woodpecker 3.x streams logs over WebSocket, which the "
                "OAuth2 proxy only accepts for an interactive browser session "
                "(a bearer token is not forwarded on the upgrade).",
        "view_in_web_ui": f"{provider.base_url.rstrip('/')}/{args.repo}/pipelines/{args.pipeline}",
    })
    return 0


def _cmd_ci_debug(args) -> int:
    provider = _ci_provider()
    if not provider.available:
        return _ci_offline()
    try:
        _print_json(provider.debug_with_pi(args.repo, args.number))
    except Exception as exc:
        _print_json({"available": False, "error": str(exc)})
        return 1
    return 0


def _cmd_browser(args) -> int:
    from .artifacts import FilesystemArtifactWorkspace
    from .browser import NoopBrowserAdapter, PlaywrightBrowserAdapter
    artifacts = FilesystemArtifactWorkspace()
    adapter = PlaywrightBrowserAdapter(artifacts=artifacts)
    if not adapter.available:
        adapter = NoopBrowserAdapter(artifacts=artifacts)
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


def _cmd_review_gate_worker(args) -> int:
    """Exit 0 on a clean stop, 1 when the reviewer key is refused, 2 on a config error.

    Messages are fixed text (config errors carry no key material); nothing from a
    response, URL or key file is printed.
    """
    from .review_gate import worker as rgw

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = rgw.load_worker_config(args.config)
        reviewer = rgw.ReviewWorker(config)
        if args.once:
            print("handled" if reviewer.run_once() else "idle")
            return 0
        stop = threading.Event()

        def _request_stop(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        reviewer.run(stop)
        return 0
    except rgw.WorkerAuthError:
        print("review-gate worker: the control plane refused the reviewer key", file=sys.stderr)
        return 1
    except rgw.WorkerConfigError as exc:
        print(f"review-gate worker: config error: {exc}", file=sys.stderr)
        return 2


def _cmd_review_gate_pool(args) -> int:
    """Same exit codes and fixed-text messages as ``review-gate worker``."""
    from .review_gate import pool as rgp
    from .review_gate import worker as rgw

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        pool = rgp.ReviewPool(rgp.load_pool_config(args.config))
        stop = threading.Event()

        def _request_stop(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        pool.run(stop)
        return 0
    except rgw.WorkerAuthError:
        print("review-gate pool: the control plane refused the reviewer key", file=sys.stderr)
        return 1
    except rgw.WorkerConfigError as exc:
        print(f"review-gate pool: config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - restartable: not a refused key or bad config
        print(f"review-gate pool: internal error: {type(exc).__name__}", file=sys.stderr)
        return 3


def _cmd_review_gate_check_skips(args) -> int:
    """Exit 0 clean, 1 when a skipped test is not in the disabled file, 2 on bad input."""
    from .review_gate import skips

    try:
        report = skips.check_junit(args.junit, skips.load_disabled(args.disabled))
    except skips.SkipsError as exc:
        print(f"review-gate check-skips: {exc}", file=sys.stderr)
        return 2
    for name in report.unused_disabled:
        print(f"warning: listed as disabled but not skipped: {name}", file=sys.stderr)
    if report.skipped_unlisted:
        print(f"{len(report.skipped_unlisted)} test(s) skipped outside the disabled-tests file:")
        for name in report.skipped_unlisted:
            print(f"- {name}")
        return 1
    print(f"ok: {report.total} testcases, no unlisted skips")
    return 0


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


def _cp_migrations_dir() -> str:
    from pathlib import Path
    pkg = Path(__file__).resolve().parent          # .../herdr_engineering
    return str(pkg.parent / "deploy" / "control-plane" / "migrations")


def _cp_config(fleet_config_path=None) -> dict:
    """Control-plane settings: config.yaml defaults, with the Postgres DSN taken
    from the central fleet file's control_plane section when that file exists."""
    from pathlib import Path

    from .config import load_config
    from .control_plane.db import control_plane_config
    from .fleet_config import FleetConfigError, load_fleet_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    out = control_plane_config(cfg)
    if fleet_config_path:
        path = Path(fleet_config_path).expanduser()
        if path.exists():
            try:
                dsn = load_fleet_config(path).section("control_plane").get("pg_dsn")
            except FleetConfigError as exc:
                raise SystemExit(f"--fleet-config: {exc}") from None
            if dsn:
                out["postgres_dsn"] = str(dsn)
    return out


async def _cp_connect(dsn: str):
    import asyncpg
    return await asyncpg.connect(dsn)


def _cmd_cp_migrate(args) -> int:
    import asyncio

    from .control_plane.db import Migrator
    migrations = args.migrations or _cp_migrations_dir()

    async def _run():
        conn = await _cp_connect(_cp_config(getattr(args, "fleet_config", None))["postgres_dsn"])
        try:
            return await Migrator(conn).apply(migrations)
        finally:
            await conn.close()

    applied = asyncio.run(_run())
    _print_json({"ok": True, "applied": applied, "migrations_dir": migrations})
    return 0


def _cmd_cp_collab_repair(args) -> int:
    import asyncio

    from .control_plane import collab_repair
    try:
        if args.log_file:
            report = collab_repair.repair_file(args.log_file, apply=args.apply, confirmed=args.confirm_quarantine,
                                               cut_tail=args.include_gap_tail)
        else:
            async def _run():
                conn = await _cp_connect(_cp_config(getattr(args, "fleet_config", None))["postgres_dsn"])
                try:
                    return await collab_repair.repair_postgres(conn, apply=args.apply, confirmed=args.confirm_quarantine,
                                                               cut_tail=args.include_gap_tail)
                finally:
                    await conn.close()
            report = asyncio.run(_run())
    except (PermissionError, collab_repair.LogInUseError, collab_repair.SchemaTooOldError) as exc:
        raise SystemExit(str(exc)) from None
    _print_json({"ok": True, **report})
    return 0


def _parse_service_port(endpoint: str | None, default: int) -> int:
    """Best-effort port extraction from a URL/DSN (``http://rustfs:9000``)."""
    if not endpoint:
        return default
    m = re.search(r":(\d+)(?:/|$)", endpoint)
    return int(m.group(1)) if m else default


def _self_report_services(repo, *, host_id, web_port, dsn=None,
                          rustfs_endpoint=None):
    """Register the control-plane's own stack services (idempotent).

    The Services page is otherwise dead in production: the herdr plugin emits
    only host/mission/session events and nothing reports services. Self-
    reporting the stack makes the page show real data, and ``upsert_service``
    keys by (host, name) so restarting the web never duplicates a row.
    """
    repo.upsert_service(host_id, "herdr-control-plane", int(web_port),
                        status="healthy",
                        route=f"http://{host_id}:{web_port}")
    repo.upsert_service(host_id, "postgres", _parse_service_port(dsn, 5432),
                        status="healthy")
    repo.upsert_service(host_id, "rustfs",
                        _parse_service_port(rustfs_endpoint, 9000),
                        status="healthy")
    repo.upsert_service(host_id, "worker", 0, status="healthy")


def _host_aliases_from(cp: dict) -> dict[str, str]:
    """control_plane.host_aliases: a mapping, or the ``alias=canonical,...`` string."""
    raw = cp.get("host_aliases") or {}
    if isinstance(raw, dict):
        return {str(a): str(c) for a, c in raw.items() if a and c}
    return _parse_host_aliases(str(raw))


def _parse_host_aliases(raw: str) -> dict[str, str]:
    """``alias=canonical,alias2=canonical2`` -> mapping; malformed pairs are ignored."""
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {a.strip(): c.strip() for a, c in pairs if a.strip() and c.strip()}


def _require_durable_review_gate(cp: dict, dsn) -> None:
    """An enabled review gate needs Postgres (``control_plane.pg_dsn``).

    Gate state (PR states, review queue, author pins) is event-sourced and
    written with ``require_durable=True``. Without a DSN the repo has no sink,
    those writes would live only in memory and a restart would forget every
    approval and escalation, so the real server refuses to start. The check
    lives here, where the DSN is known: a ``ControlPlaneServer`` built directly
    on an in-memory ``ControlPlaneRepo()`` (tests, embedding) is not refused.
    """
    gate = cp.get("review_gate")
    if not dsn and isinstance(gate, dict) and gate.get("enabled") is True:
        raise SystemExit("control_plane.review_gate needs durable storage: set "
                         "control_plane.pg_dsn, or disable the review gate")


def _cmd_cp_web(args) -> int:
    """Serve the control-plane UI + structured /api/v1 + SSE (private-by-default).

    The web UI is a client of this structured API. Runs the in-memory
    ControlPlaneRepo so the whole surface is usable standalone; the durable
    Postgres/RustFS path (Phase 8) plugs in behind the same repo interface.
    """
    from .control_plane.api import ControlPlaneServer
    from .control_plane.repo import ControlPlaneRepo
    from .control_plane.sink import DurableEventSink

    fleet_store, cp = _load_cp_fleet_config(getattr(args, "fleet_config", None))
    switch_store = fleet_store
    replay_flag = getattr(args, "collab_replay_history", None)      # an explicit --collab-replay-history wins over the YAML
    api_keys = _parse_api_keys(args.api_key, args.api_keys)
    lineage_key_scopes = {}
    federation_source = None
    if args.collab_only:
        # The public collab container is given only the collab slice of the central
        # file (`fleet config render-collab`): no fleet endpoint, no API keys, no DSN.
        if args.pg_dsn or set(cp) - {"collab"}:
            raise SystemExit("--collab-only must not be given database credentials or other "
                             "control-plane settings (mount the render-collab slice, not the "
                             "central file)")
        _apply_collab_config(args, cp)
        if not (args.collab_log_file and args.collab_public_port and args.collab_cognito_pool):
            raise SystemExit("--collab-only needs --collab-log-file, --collab-public-port and a "
                             "Cognito pool (control_plane.collab.cognito_pool or --collab-cognito-pool)")
        switch_store, fleet_store = fleet_store, None     # no fleet endpoint here, but the switches still reload
    elif fleet_store is not None:
        _install_fleet_host_config(fleet_store)
        names = (cp.get("collab") or {}).get("dashboard_hosts") if isinstance(cp.get("collab"), dict) else None
        if names is not None:
            if not isinstance(names, list) or not all(isinstance(v, str) and v for v in names):
                raise SystemExit("fleet config control_plane.collab.dashboard_hosts must be a list of strings")
            args.collab_dashboard_host = list(args.collab_dashboard_host) + [n for n in names if n not in args.collab_dashboard_host]
        file_keys = cp.get("api_keys") or {}
        if not isinstance(file_keys, dict):
            raise SystemExit("fleet config control_plane.api_keys must be a mapping of key -> role")
        merged = {str(k): str(v) for k, v in file_keys.items()}
        merged.update(api_keys or {})
        api_keys = merged or None
        from .control_plane.federation import PEER_KINDS, FederationConfigError, parse_federation
        if any(str(v).strip() in PEER_KINDS for v in (api_keys or {}).values()):
            raise SystemExit("api_keys: role 'peer'/'peer-user' is not an API key role; a federated peer is configured "
                             "under control_plane.federation.peers (ADR-0008), never as a key")
        try:
            parse_federation(cp.get("federation"))        # startup: refuse a bad section; hot reads fail closed
        except FederationConfigError as exc:
            raise SystemExit(f"fleet config {exc}") from None

        def federation_source(_store=fleet_store):
            return _store.secure_section("control_plane").get("federation")     # raises while the file is unusable
        raw_scopes = cp.get("lineage_key_scopes") or {}
        if not isinstance(raw_scopes, dict):
            raise SystemExit("fleet config control_plane.lineage_key_scopes must map an api key to {rooms: [...], missions: [...]}")
        for key, allow in raw_scopes.items():
            if (str(key) not in (api_keys or {}) or not isinstance(allow, dict) or set(allow) - {"rooms", "missions"}
                    or not all(isinstance(allow.get(n, []), list) and all(isinstance(v, str) and v for v in allow.get(n, []))
                               for n in ("rooms", "missions"))):
                raise SystemExit("fleet config control_plane.lineage_key_scopes: each entry must name a configured api key "
                                 "and hold only rooms/missions lists of non-empty strings")
        if any((api_keys or {}).get(str(k)) == "operator" for k in raw_scopes):
            raise SystemExit("fleet config control_plane.lineage_key_scopes: an entry names an operator-role key; operator keys "
                             "are not restricted, so the entry would silently do nothing. Remove it or use a non-operator key")
        lineage_key_scopes = {str(k): v for k, v in raw_scopes.items()}
    # Opt-in: reflect mission/session/repo/trace lifecycle events into the running
    # lineage graph (system-graph spec P2-P4). Off by default.
    system_graph = bool(cp.get("system_graph")) or bool(getattr(args, "system_graph", False))
    if args.collab_cognito_group is None:
        args.collab_cognito_group = "local.dev"
    # The Postgres DSN comes from the fleet file's control_plane section (the
    # mounted central file), so the password never appears in process args or env.
    dsn = None if args.collab_only else (args.pg_dsn or cp.get("pg_dsn"))
    inspector_limits = cp.get("inspector_limits") or {}
    if not isinstance(inspector_limits, dict):
        raise SystemExit("fleet config control_plane.inspector_limits must be a mapping")
    from .control_plane.retention import usage_retention_config
    try:
        usage_retention = usage_retention_config(cp.get("llm_proxy"))
    except ValueError as exc:
        raise SystemExit(f"fleet config {exc}") from None
    from .control_plane.attention_inbox import attention_config
    try:
        attention = attention_config(cp.get("attention"))
    except ValueError as exc:
        raise SystemExit(f"fleet config {exc}") from None
    from .control_plane.mission_outcome import missions_config
    try:
        missions = missions_config(cp.get("missions"))
    except ValueError as exc:
        raise SystemExit(f"fleet config {exc}") from None
    from .control_plane.ci_fix import ci_fix_config
    try:
        ci_fix = ci_fix_config(cp.get("ci_fix"))
    except ValueError as exc:
        raise SystemExit(f"fleet config {exc}") from None
    _require_durable_review_gate(cp, dsn)
    source = _maybe_pg_source(dsn)
    # When durable storage is wired, attach a write-through sink so every
    # ingested event persists to Postgres instead of dying with the in-memory
    # repo on restart (Phase 1a). Best-effort; never blocks or breaks serving.
    sink = DurableEventSink(dsn=dsn) if dsn else None
    if args.collab_only:
        from .control_plane.collab import FileEventLog
        sink = FileEventLog(args.collab_log_file)
    repo = ControlPlaneRepo(sink=sink, host_aliases=_host_aliases_from(cp),
                            inspector_limits=inspector_limits)
    if args.seed_demo:
        _seed_demo(repo)
    rustfs = _maybe_rustfs_client(cp)
    artifact_adapter = None
    if dsn and rustfs is not None:
        from .control_plane.artifact_adapter import SyncArtifactAdapter
        from .control_plane.artifacts import ArtifactStore

        async def create_artifact_store():
            import asyncpg
            conn = await asyncpg.connect(dsn)
            return ArtifactStore(conn, rustfs)

        artifact_adapter = SyncArtifactAdapter(create_artifact_store)
    from .control_plane.artifact_acl import ArtifactAcl
    from .control_plane.artifact_jobs import ArtifactJobManager
    from .control_plane.artifact_reviews import ArtifactReviewManager
    from .control_plane.publication import PrivateOnlyExposureProvider, PublicationController
    from .exposure_cloudflare import CloudflareTunnelProvider
    from .exposure_tailscale import TailscaleFunnelProvider
    persist = (lambda action, payload: artifact_adapter("record_job_event", action, payload)) \
        if artifact_adapter is not None else None
    persist_review = (lambda action, payload: artifact_adapter("record_review_event", action, payload)) \
        if artifact_adapter is not None else None
    persist_acl = (lambda action, payload: artifact_adapter("record_acl_event", action, payload)) \
        if artifact_adapter is not None else None
    persist_publication = (lambda action, payload: artifact_adapter("record_publication_event", action, payload)) \
        if artifact_adapter is not None else None
    artifact_jobs = ArtifactJobManager(on_change=persist)
    artifact_reviews = ArtifactReviewManager(on_change=persist_review)
    artifact_acl = ArtifactAcl(on_change=persist_acl)
    provider_name = cp.get("public_exposure_provider", "private_only")
    if provider_name == "tailscale_funnel":
        publication_provider = TailscaleFunnelProvider(enabled=True)
    elif provider_name == "cloudflare_tunnel":
        publication_provider = CloudflareTunnelProvider(enabled=True)
    else:
        publication_provider = PrivateOnlyExposureProvider()
    publication_controller = PublicationController(
        provider=publication_provider, on_change=persist_publication,
        default_local_target=cp.get("public_gateway_target", "http://127.0.0.1:8085"))
    if artifact_adapter is not None:
        artifact_acl.load(artifact_adapter("load_acl"))
        artifact_reviews.load(artifact_adapter("load_reviews"))
        artifact_jobs.load(artifact_adapter("load_jobs"))
    collab = None
    if args.collab_cognito_pool:
        if args.collab_users_file:
            raise SystemExit("use either --collab-cognito-pool or --collab-users-file, not both")
        from .control_plane.collab import CollabService
        from .control_plane.collab_cognito import CognitoAuthenticator, boto_group_lookup
        clients = list(args.collab_cognito_client)
        public = {}
        if args.collab_cognito_web_client:
            if not (args.collab_cognito_domain and args.collab_public_url
                    and args.collab_public_url.startswith("https://")):
                raise SystemExit("--collab-cognito-web-client needs --collab-cognito-domain and an "
                                 "https:// --collab-public-url")
            clients.append(args.collab_cognito_web_client)
            public = {"client_id": args.collab_cognito_web_client,
                      "hosted_ui_domain": args.collab_cognito_domain.rstrip("/"),
                      "public_url": args.collab_public_url}
        try:
            auth = CognitoAuthenticator(pool_id=args.collab_cognito_pool,
                                        client_ids=clients,
                                        required_group=args.collab_cognito_group,
                                        open_signup=args.collab_open_signup)
            if args.collab_sync_dir:
                from .control_plane.collab_cognito import SyncedDirectory
                directory = SyncedDirectory(args.collab_sync_dir, args.collab_cognito_group)
                auth._fetch = directory.jwks
                if not args.collab_open_signup:        # approval mode keeps its own state, not a group list
                    auth._lookup = directory.groups
                    auth._recheck = 15.0
            elif not args.collab_open_signup:
                auth._lookup = boto_group_lookup(auth.pool_id, auth.region)
        except ValueError as exc:
            raise SystemExit(f"--collab-cognito-*: {exc}") from exc
        if sink is not None and source is None and not args.collab_only:   # the file log is hydrated below
            raise SystemExit("collaboration with a durable sink requires the source to load "
                             "existing state; refusing to start empty over an existing log")
        approval = None
        if args.collab_open_signup:
            from .control_plane.collab import ApprovalPolicy
            approval = ApprovalPolicy(auto_groups=[args.collab_cognito_group],
                                      approver_groups=args.collab_approver_group)
        collab = CollabService(auth, sink=sink, public_config=public, approval=approval,
                               delegation_enabled=_delegation_switch(args, cp),
                               summary_fix=getattr(args, "collab_summary_fix", None))
        if args.collab_only:
            collab.hydrate_or_degrade(sink.load)       # T16 H1: any replay failure disables collaboration only
    elif args.collab_users_file:
        from pathlib import Path

        from .control_plane.collab import CollabService, StaticTokenAuthenticator
        users = json.loads(Path(args.collab_users_file).read_text(encoding="utf-8"))
        if (not isinstance(users, dict) or not users or not all(
                isinstance(u, str) and isinstance(d, str) and len(d) == 64
                for u, d in users.items())):
            raise SystemExit("--collab-users-file must map user_id -> sha256 hex digest")
        if sink is not None and source is None:
            raise SystemExit("collaboration with a durable sink requires the source to load "
                             "existing state; refusing to start empty over an existing log")
        collab = CollabService(StaticTokenAuthenticator(users), sink=sink,
                               delegation_enabled=_delegation_switch(args, cp))
    if collab is not None:
        history = getattr(args, "collab_replay_history", None) or "since_join"
        if history not in ("full", "since_join"):
            raise SystemExit("control_plane.collab.replay_history must be 'full' or 'since_join'")
        collab.replay_history = history
        from .control_plane.collab_attention import normalize_insights
        from .control_plane.collab_context import normalize_contextual
        from .control_plane.collab_workbench import normalize_steering
        collab.steering = normalize_steering(getattr(args, "collab_steering", None))
        collab.contextual = normalize_contextual(getattr(args, "collab_contextual", None))
        collab.insights = normalize_insights(getattr(args, "collab_insights", None))
        _wire_collab_switches(collab, switch_store, pinned=frozenset({"replay_history"}) if replay_flag else frozenset())
    svr = ControlPlaneServer(repo, api_keys=api_keys, lineage_key_scopes=lineage_key_scopes,
                             federation_source=federation_source,
                             system_graph=system_graph,
                             host=args.host, port=args.port, source=source,
                             web_root=args.web_root,
                             # collab-only has no operator listener, so nothing is left open
                             allow_open=args.allow_open or args.collab_only,
                             rustfs=rustfs, artifact_store=artifact_adapter,
                             artifact_job_manager=artifact_jobs,
                             artifact_review_manager=artifact_reviews,
                             artifact_acl=artifact_acl,
                             publication_controller=publication_controller, collab=collab,
                             collab_tls_terminated=args.collab_tls_terminated,
                             collab_require_https=args.collab_require_https,
                             fleet_config=fleet_store, ci_actions=cp.get("ci_actions") is True,
                             fabric_resolver=_fabric_url_resolver(), fabric_services=_fabric_service_lister(), fabric_names=_fabric_names_getter(),
                             scheduler_admission=cp.get("scheduler_admission") is True,
                             scheduler_limits=cp.get("scheduler_limits") or {},
                             summary_config=cp.get("agent_summaries") or {},
                             summary_report_config=cp.get("summary_report") or {},
                             assistant_config={"personal_assistant": cp["personal_assistant"]}
                                 if isinstance(cp.get("personal_assistant"), dict) else None,
                             review_quorum=_review_quorum_config(cp),
                             usage_retention=usage_retention, attention=attention,
                             ci_fix=ci_fix, missions=missions, github_pg_dsn=dsn,
                             public_port=args.collab_public_port,
                             public_host=args.collab_public_host,
                             collab_only=args.collab_only,
                             operator_port=args.collab_operator_port,
                             operator_user=args.collab_operator_user,
                             operator_email=args.collab_operator_email,
                             operator_hosts=args.collab_operator_host,
                             collab_operator_url=args.collab_operator_url,
                             collab_dashboard_hosts=args.collab_dashboard_host)
    # Self-report the control-plane stack so the Services page has real data.
    try:
        if not args.collab_only:   # collab-only has no stack to report
            _self_report_services(
                repo,
                host_id=cp.get("self_host") or socket.gethostname(),
                web_port=args.port or cp.get("web_port") or 8080,
                dsn=dsn,
                rustfs_endpoint=cp.get("rustfs_endpoint"),
            )
    except Exception:
        logging.exception("self-report of stack services failed")
    _print_json({"ok": True, "serving": "control plane UI + /api/v1 + SSE",
                 "host": args.host, "port": args.port,
                 "url": f"http://{args.host}:{svr.port if not args.port else args.port}"})
    try:
        svr.serve()
    except KeyboardInterrupt:
        pass
    finally:
        svr.shutdown()
        if artifact_adapter is not None:
            artifact_adapter.close()
    return 0


def _cmd_cp_public_gateway(args) -> int:
    """Serve only approved immutable artifact delivery; no dashboard/API routes."""
    from .control_plane.artifact_adapter import SyncArtifactAdapter
    from .durable_public_gateway import DurablePublicGateway
    from .public_gateway_server import PublicGatewayServer

    _, cp = _load_cp_fleet_config(args.fleet_config)
    dsn = cp.get("pg_dsn")
    endpoint = cp.get("rustfs_endpoint")
    if not dsn or not endpoint:
        raise SystemExit("public-gateway requires control_plane.pg_dsn and rustfs_endpoint")
    rustfs = _maybe_rustfs_client(cp)
    if rustfs is None:
        raise SystemExit("public-gateway requires configured RustFS-compatible storage")
    bucket = cp.get("rustfs_bucket", "herdr-artifacts")

    async def create_delivery_store():
        import asyncpg
        conn = await asyncpg.connect(dsn)
        return DurablePublicGateway(conn, rustfs, bucket=bucket)

    adapter = SyncArtifactAdapter(create_delivery_store)
    gateway = DurablePublicGateway.from_sync(adapter)
    server = PublicGatewayServer(gateway, host=args.host, port=args.port)
    _print_json({"ok": True, "serving": "approved artifact delivery only",
                 "host": args.host, "port": server.server_port})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        adapter.close()
    return 0


def _install_fleet_host_config(store) -> None:
    """The web process owns the fleet file, so its own ci/github/fabric/llm reads
    (providers built via host_config) come straight from it, live on reload."""
    from .fleet_config import FleetConfigError
    from .host_config import HostConfig, install_source

    def source():
        cfg = store.current()
        values = {}
        for name in ("ingest", "agent", "github", "ci", "llm", "fabric", "inspector"):
            try:
                values[name] = cfg.section(name)
            except FleetConfigError:
                continue
        return HostConfig("", socket.gethostname(), cfg.revision, values)
    install_source(source)


# control_plane.collab keys -> (argparse dest, kind). Read only by `web --collab-only`;
# the dashboard never enables collaboration from the file.
COLLAB_CONFIG_KEYS = {
    "cognito_pool": ("collab_cognito_pool", str),
    "cognito_clients": ("collab_cognito_client", list),
    "cognito_group": ("collab_cognito_group", str),
    "cognito_web_client": ("collab_cognito_web_client", str),
    "cognito_domain": ("collab_cognito_domain", str),
    "public_url": ("collab_public_url", str),
    "operator_user": ("collab_operator_user", str),
    "operator_email": ("collab_operator_email", str),
    "open_signup": ("collab_open_signup", bool),
    "approver_groups": ("collab_approver_group", list),
    "dashboard_hosts": ("collab_dashboard_host", list),   # names the private dashboard may be opened by
    # T11 kill switch: bounded task delegation + artifact proposals. Default false = the feature does not exist.
    "delegation_enabled": ("collab_delegation_enabled", bool),
    "replay_history": ("collab_replay_history", str),     # lineage replay: since_join (default) | full
    "steering": ("collab_steering", dict),                # T12 comments + steering; YAML only, off by default
    "contextual": ("collab_contextual", dict),            # T14 context refs, consultation, cross links; YAML only, off by default
    "insights": ("collab_insights", dict),                # T15 attention inbox, progress, analytics; YAML only, off by default
    "skills": ("collab_skills", dict),                    # public collaboration skill catalog/config
    # {room_id, requester_agent_id}: the operator's standing summary-fix room (YAML only, no flag)
    "summary_fix": ("collab_summary_fix", dict),
}


def _apply_collab_config(args, cp) -> None:
    """Fill unset --collab-* flags from control_plane.collab (flags win; booleans OR)."""
    collab = cp.get("collab") or {}
    if not isinstance(collab, dict):
        raise SystemExit("fleet config control_plane.collab must be a mapping")
    unknown = sorted(set(collab) - set(COLLAB_CONFIG_KEYS))
    if unknown:
        raise SystemExit("fleet config control_plane.collab: unknown keys: " + ", ".join(unknown))
    for key, (dest, kind) in COLLAB_CONFIG_KEYS.items():
        if key not in collab:
            continue
        value = collab[key]
        if kind is dict and key == "summary_fix":
            from .control_plane.collab import summary_fix_config
            try:
                setattr(args, dest, summary_fix_config(value))
            except ValueError as exc:
                raise SystemExit(f"fleet config {exc}") from None
            continue
        if kind is list:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise SystemExit(f"fleet config control_plane.collab.{key} must be a list of strings")
            if not getattr(args, dest):
                setattr(args, dest, list(value))
        elif kind is dict:
            from .control_plane.collab_attention import normalize_insights
            from .control_plane.collab_context import normalize_contextual
            from .control_plane.collab_workbench import normalize_steering
            try:
                {"contextual": normalize_contextual, "insights": normalize_insights}.get(key, normalize_steering)(value)
            except ValueError as exc:
                raise SystemExit(f"fleet config {exc}") from None
            setattr(args, dest, dict(value))
        elif kind is bool:
            if not isinstance(value, bool):
                raise SystemExit(f"fleet config control_plane.collab.{key} must be true or false")
            setattr(args, dest, bool(getattr(args, dest)) or value)
        else:
            if not isinstance(value, str) or not value:
                raise SystemExit(f"fleet config control_plane.collab.{key} must be a non-empty string")
            if getattr(args, dest) is None:
                setattr(args, dest, value)


# T16 M3/M15: EVERY collab feature switch is read per request through FleetConfigStore.secure_section, so a
# missing, unreadable (unparseable, group/other-accessible) or invalid fleet file turns it OFF (or, for
# replay_history, to the stricter value) identically. Adding a switch to COLLAB_CONFIG_KEYS without
# registering it here fails tests/control_plane/test_t16_security.py.
HOT_COLLAB_SWITCHES = {
    "steering": ("steering_source", "dict"),
    "contextual": ("contextual_source", "dict"),
    "insights": ("insights_source", "dict"),
    "delegation_enabled": ("delegation_source", "bool"),
    "replay_history": ("replay_history_source", "str"),
}
# collab keys that are not feature switches (identity, listener and URL settings read once at startup)
STARTUP_ONLY_COLLAB_KEYS = frozenset(COLLAB_CONFIG_KEYS) - frozenset(HOT_COLLAB_SWITCHES)


# control_plane keys read ONCE at startup: editing them needs a restart (unlike HOT_COLLAB_SWITCHES)
RESTART_REQUIRED_KEYS = ("review_quorum", "lineage_key_scopes", "api_keys", "ci_actions", "pg_dsn", "architecture")


def _wire_collab_switches(collab, store, pinned=frozenset()) -> None:
    """Attach the hot, fail-closed sources of every collab switch (no-op without a fleet file). ``pinned`` names
    switches given on the command line: the flag wins and the file is not consulted for them."""
    if store is None:
        return

    def make(key):
        def source(_store=store):
            section = _store.secure_section("control_plane")          # raises while the file is unusable
            collab_section = section.get("collab")
            return collab_section.get(key) if isinstance(collab_section, dict) else None
        return source
    from .control_plane.collab_attention import normalize_insights
    from .control_plane.collab_context import normalize_contextual
    from .control_plane.collab_workbench import normalize_steering
    normalizers = {"steering": normalize_steering, "contextual": normalize_contextual, "insights": normalize_insights}

    def check(key, value):
        return normalizers[key](value) if key in normalizers else value
    for key, (attr, kind) in HOT_COLLAB_SWITCHES.items():
        base = make(key)
        if kind == "bool":
            def source(_base=base):
                value = _base()
                if value is None:
                    return False
                if not isinstance(value, bool):
                    raise ValueError("delegation_enabled must be true or false")
                return value
        elif kind == "str":
            def source(_base=base):
                value = _base()
                if value is None:
                    return "since_join"
                if value not in ("full", "since_join"):
                    raise ValueError("replay_history must be 'full' or 'since_join'")
                return value
        else:
            source = base
        if key in pinned:
            continue
        setattr(collab, attr, source)
        try:                       # an invalid block is not a startup error (the switch is simply OFF) but it is loud
            check(key, source())
        except Exception as exc:   # noqa: BLE001
            logging.getLogger(__name__).error(
                "control_plane.collab.%s is unusable (%s): the feature is OFF until the file is fixed", key, exc)


def _delegation_switch(args, cp) -> bool:
    """The T11 kill switch from control_plane.collab.delegation_enabled (YAML only, default off). The
    collab-only path has already folded it into ``args`` via _apply_collab_config; the combined path reads it here."""
    if getattr(args, "collab_delegation_enabled", False):
        return True
    collab = cp.get("collab") if isinstance(cp, dict) else None
    value = collab.get("delegation_enabled", False) if isinstance(collab, dict) else False
    if not isinstance(value, bool):
        raise SystemExit("fleet config control_plane.collab.delegation_enabled must be true or false")
    return value


def _review_quorum_config(cp):
    """Parse control_plane.review_quorum (None = off); a bad block stops startup."""
    from .control_plane.review_quorum import parse_config
    try:
        return parse_config(cp.get("review_quorum"))
    except ValueError as exc:
        raise SystemExit(f"--fleet-config: {exc}") from None


def _load_cp_fleet_config(path_arg):
    """Return (FleetConfigStore, control_plane section) or (None, {}) when no file exists."""
    from pathlib import Path

    from .fleet_config import FleetConfigError, FleetConfigStore
    if not path_arg:
        return None, {}
    path = Path(path_arg).expanduser()
    if not path.exists():
        return None, {}
    try:
        store = FleetConfigStore(path)
        cp = store.current().section("control_plane")
    except FleetConfigError as exc:
        raise SystemExit(f"--fleet-config: {exc}") from None
    return store, cp


def _maybe_rustfs_client(cp=None):
    """Build an S3/RustFS client for artifact payloads, or None if not wired.

    Configured by the fleet file's control_plane section: rustfs_endpoint,
    rustfs_bucket (default herdr-artifacts), rustfs_region (default us-east-1),
    minio_user and minio_password (default the fixed dev creds). Returns None
    when there is no endpoint so the web server still starts with the
    in-memory stand-in.
    """
    from .control_plane.rustfs import RustFSClient
    cp = cp or {}
    endpoint = cp.get("rustfs_endpoint")
    bucket = cp.get("rustfs_bucket", "herdr-artifacts")
    region = cp.get("rustfs_region", "us-east-1")
    access_key = cp.get("minio_user", "herdr")
    secret_key = cp.get("minio_password", "herdr")
    if not endpoint:
        return None
    client = RustFSClient(endpoint=endpoint, bucket=bucket, region=region,
                          access_key=access_key, secret_key=secret_key)
    try:
        client.ensure_bucket()
    except Exception:
        # Bucket creation is best-effort; MinIO may already have it. Don't
        # crash web startup over object storage.
        pass
    return client


def _maybe_pg_source(dsn):
    """Build a PostgresSource from a DSN, or None if not provided."""
    if not dsn:
        return None
    from .control_plane.bridge import PostgresSource

    # Lazy: PostgresSource connects on its own persistent event loop when the
    # server first hydrates from Postgres, so connect+fetch share one loop.
    return PostgresSource(dsn=dsn)


def _parse_api_keys(single_key, pairs_str):
    """Build the {key: role} auth map from --api-key / --api-keys."""
    keys = {}
    if single_key:
        keys[single_key] = "operator"
    if pairs_str:
        for pair in pairs_str.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" in pair:
                key, role = pair.split("=", 1)
                keys[key.strip()] = role.strip() or "operator"
            else:
                keys[pair] = "operator"
    return keys or None


def _seed_demo(repo):
    """Populate a fresh repo with demo missions/sessions/hosts/services."""
    hid1 = repo.register_host("bender", "100.104.39.6")
    hid2 = repo.register_host("beast", "100.96.156.65")
    repo.record_telemetry(hid1, {"cpu_pct": 12, "mem_pct": 38, "disk_pct": 54})
    repo.record_telemetry(hid2, {"cpu_pct": 3, "mem_pct": 21, "disk_pct": 30})

    mid = repo.create_mission("HerdR control-plane program",
                              purpose="Spec 0190 — dev fabric control plane",
                              stage="implementation")
    sid = repo.create_session(mid, agent_role="orchestrator", model="gpt-4")
    repo.send_message(sid, "steer: focus on the durable Postgres bridge")
    repo.record_tool_call(sid, "edit", "bridge.py")
    repo.set_mission_stage(mid, "review")

    mid2 = repo.create_mission("Fleet telemetry rollout",
                               purpose="Host health sampling across the fabric")
    sid2 = repo.create_session(mid2, agent_role="implementer", model="claude")
    repo.send_message(sid2, "add CPU/RAM/disk sampling agent")
    repo.set_mission_stage(mid2, "testing")

    repo.register_service("herdr-control-plane", hid1, 8080,
                          session_id=sid, status="healthy")
    repo.register_service("fabric-gateway", hid1, 53, status="healthy")
    repo.register_service("analytics-api", hid2, 9001, status="healthy")


def _cmd_cp_worker(args) -> int:
    import asyncio
    import logging

    from .control_plane.artifact_render_runner import ArtifactRenderRunner
    from .control_plane.artifact_renderers import RendererRegistry
    from .control_plane.artifacts import ArtifactStore
    from .control_plane.db import Migrator
    from .control_plane.worker import run_one_pass, run_worker
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    async def _run():
        cp = _cp_config(getattr(args, "fleet_config", None))
        conn = await _cp_connect(cp["postgres_dsn"])
        try:
            migrations = args.migrations or _cp_migrations_dir()
            await Migrator(conn).apply(migrations)
            rustfs = _maybe_rustfs_client(cp)
            render_runner = (ArtifactRenderRunner(ArtifactStore(conn, rustfs),
                                                   RendererRegistry())
                             if rustfs is not None else None)
            runner = render_runner if render_runner is not None else None
            worker_id = args.host_id or socket.gethostname()
            if args.once:
                return await run_one_pass(conn, worker_id=worker_id,
                                          artifact_job_runner=runner)
            await run_worker(conn, interval=args.interval, worker_id=worker_id,
                             artifact_job_runner=runner)
            return None
        finally:
            await conn.close()

    try:
        result = asyncio.run(_run())
    except KeyboardInterrupt:
        return 130
    if result is not None:
        _print_json({"ok": True, "pass": result})
    return 0


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
