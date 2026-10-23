"""``herdr-eng architecture analyze``: analyse a commit on THIS host and publish the snapshot.

Source never leaves the host; only the derived graph is POSTed. Credentials come
from a key file, not the environment.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..control_plane.project_links import canonical_repository
from . import gitsource, runner


DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "herdr-engineering", "architecture")


def register(sub) -> None:
    p = sub.add_parser("architecture", help="architecture explorer (host-local analysis)")
    asub = p.add_subparsers(dest="architecture_command", required=True)
    a = asub.add_parser("analyze", help="analyse a commit of a local checkout and publish its snapshot")
    a.add_argument("--repo", default=".", help="local checkout or worktree (default .)")
    a.add_argument("--rev", default="HEAD", help="commit-ish to analyse (default HEAD)")
    a.add_argument("--repository-id", default=None,
                   help="canonical repository identity (default: canonicalised origin URL)")
    a.add_argument("--host-id", default=None, help="owning host id (default: this hostname)")
    a.add_argument("--url", default=None, help="control-plane base URL to publish to")
    a.add_argument("--api-key-file", default=None, help="file holding an operator/agent API key")
    a.add_argument("--output", default=None, help="write the snapshot JSON here instead of publishing")
    a.add_argument("--timeout", type=float, default=300.0, help="wall-clock limit in seconds")
    a.add_argument("--cache-dir", default=DEFAULT_CACHE, help="facts cache for incremental runs (outside the repo)")
    a.add_argument("--no-cache", action="store_true", help="full baseline: ignore and do not write the facts cache")
    a.set_defaults(func=cmd_analyze)
    sv = asub.add_parser("serve", help="claim analysis requests for this host from the control plane and run them")
    sv.add_argument("--url", required=True)
    sv.add_argument("--api-key-file", required=True)
    sv.add_argument("--host-id", default=None)
    sv.add_argument("--checkout", action="append", default=[], metavar="REPOSITORY_ID=PATH",
                    help="local checkout for a canonical repository id (repeatable); only these are ever analysed")
    sv.add_argument("--poll", type=float, default=5.0)
    sv.add_argument("--once", action="store_true", help="process at most one request, then exit")
    sv.add_argument("--timeout", type=float, default=300.0)
    sv.add_argument("--cache-dir", default=DEFAULT_CACHE)
    sv.set_defaults(func=cmd_serve)


def _get(url: str, key: str | None) -> dict:
    req = urllib.request.Request(url, headers={"X-API-Key": key} if key else {})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - operator-supplied control plane
        return json.loads(resp.read())


def _post(url: str, body: dict, key: str | None) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - operator-supplied control plane
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"control plane refused ({exc.code}): {exc.read()[:300].decode('utf-8', 'replace')}") from exc


def cmd_analyze(args) -> int:
    repo = str(Path(args.repo).resolve())
    repository_id = args.repository_id or canonical_repository(gitsource.origin_url(repo) or "")
    if not repository_id:
        raise SystemExit("cannot determine the repository identity: pass --repository-id")
    host_id = args.host_id or socket.gethostname()
    key = Path(args.api_key_file).read_text().strip() if args.api_key_file else None
    base = args.url.rstrip("/") if args.url else None
    analysis_id = "an-" + uuid.uuid4().hex[:16]
    ident = {"analysis_id": analysis_id, "repository_id": repository_id, "host_id": host_id}

    def event(name, **extra):
        if base:
            try:
                _post(f"{base}/api/v1/architecture/analyses", {**ident, "event": name, **extra}, key)
            except SystemExit as exc:
                print(f"warning: could not report {name}: {exc}", file=sys.stderr)

    commit = gitsource.resolve_commit(repo, args.rev)
    event("started", commit_sha=commit)
    try:
        result = runner.run_isolated(repo, commit, host_id, repository_id,
                                     limits=runner.Limits(wall_seconds=args.timeout),
                                     config=None if args.no_cache else {"cache_dir": args.cache_dir})
    except runner.AnalysisCancelled:
        event("cancelled", commit_sha=commit)
        raise
    except runner.AnalysisError as exc:
        event("failed", commit_sha=commit, message=str(exc)[:400])
        raise SystemExit(f"analysis failed: {exc}") from exc
    snap = result["snapshot"]
    if args.output:
        Path(args.output).write_text(json.dumps({"snapshot": snap, "graph": result["graph"]}))
    if base:
        out = _post(f"{base}/api/v1/architecture/snapshots", {"snapshot": snap, "graph": result["graph"]}, key)
        event("completed", commit_sha=commit, snapshot_id=snap["snapshot_id"])
        snap = out["snapshot"]
    print(json.dumps({"snapshot_id": snap["snapshot_id"], "commit_sha": snap["commit_sha"],
                      "completeness": snap["completeness"], "counts": snap["counts"],
                      "graph_hash": snap["graph_hash"], "network_isolated": result["sandbox"]["network_isolated"]},
                     indent=2))
    return 0


def _run_job(base, key, job, checkouts, host_id, timeout, cache_dir) -> None:
    """Run one claimed job; always ends it with completed / failed / cancelled."""
    aid, rid = job["analysis_id"], job["repository_id"]
    ident = {"analysis_id": aid, "repository_id": rid, "host_id": host_id}
    path = checkouts.get(rid)
    if path is None:
        _post(f"{base}/api/v1/architecture/analyses", {**ident, "event": "failed",
              "message": "no checkout is registered on this host for that repository"}, key)
        return
    cancel, done = threading.Event(), threading.Event()

    def watch():                                   # heartbeat extends the lease; a cancel request stops the child
        while not done.wait(2.0):
            try:
                st = _get(f"{base}/api/v1/architecture/analyses/{aid}", key)["analysis"]["state"]
                if st == "cancelled":
                    cancel.set()
                else:
                    _post(f"{base}/api/v1/architecture/analyses", {**ident, "event": "progress"}, key)
            except Exception:  # noqa: BLE001 - best effort; the lease simply lapses
                pass
    threading.Thread(target=watch, daemon=True).start()
    try:
        commit = gitsource.resolve_commit(path, job["rev"])
        result = runner.run_isolated(path, commit, host_id, rid, cancel=cancel,
                                     limits=runner.Limits(wall_seconds=timeout), config={"cache_dir": cache_dir})
        snap = result["snapshot"]
        _post(f"{base}/api/v1/architecture/snapshots", {"snapshot": snap, "graph": result["graph"]}, key)
        done.set()
        _post(f"{base}/api/v1/architecture/analyses", {**ident, "event": "completed", "commit_sha": commit,
                                                       "snapshot_id": snap["snapshot_id"]}, key)
    except runner.AnalysisCancelled:
        done.set()
    except (runner.AnalysisError, gitsource.GitSourceError, SystemExit) as exc:
        done.set()
        _post(f"{base}/api/v1/architecture/analyses", {**ident, "event": "failed", "message": str(exc)[:400]}, key)
    finally:
        done.set()


def cmd_serve(args) -> int:
    base, key = args.url.rstrip("/"), Path(args.api_key_file).read_text().strip()
    host_id = args.host_id or socket.gethostname()
    checkouts = {}
    for item in args.checkout:
        rid, _, path = item.partition("=")
        if canonical_repository(rid) != rid or not Path(path).is_dir():
            raise SystemExit(f"invalid --checkout {item!r}: need a canonical repository id and an existing directory")
        checkouts[rid] = str(Path(path).resolve())
    while True:
        got = _post(f"{base}/api/v1/architecture/analyses/claim", {"host_id": host_id}, key)
        if got.get("job"):
            _run_job(base, key, got["job"], checkouts, host_id, args.timeout, args.cache_dir)
            if args.once:
                return 0
            continue
        if args.once:
            return 0
        time.sleep(args.poll)
