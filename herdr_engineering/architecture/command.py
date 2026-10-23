"""``herdr-eng architecture analyze``: analyse a commit on THIS host and publish the snapshot.

Source never leaves the host; only the derived graph is POSTed. Credentials come
from a key file, not the environment.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..control_plane.project_links import canonical_repository
from . import gitsource, runner


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
    a.set_defaults(func=cmd_analyze)


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
                                     limits=runner.Limits(wall_seconds=args.timeout))
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
