"""Compose git source + extractor + identity into a publishable snapshot (runs in the worker)."""
from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone

from . import extract, gitsource, identity


def toolchain() -> dict:
    return {"python": f"{sys.version_info[0]}.{sys.version_info[1]}", "impl": platform.python_implementation(),
            "parser": "ast"}


def analyze_commit(repo_path: str, rev: str, host_id: str, repository_id: str,
                   max_file_bytes: int = 1_000_000, max_files: int = 20_000,
                   max_nodes: int = 200_000, worktree_id: str | None = None) -> dict:
    cfg = extract.Config(max_file_bytes=max_file_bytes, max_files=max_files, max_nodes=max_nodes)
    commit = gitsource.resolve_commit(repo_path, rev)
    wid = worktree_id or gitsource.worktree_identity(repo_path)["worktree_id"]
    tree = gitsource.list_tree(repo_path, commit)
    wanted = [e for e in tree.entries if e.path.endswith(".py") or "." in e.path.rsplit("/", 1)[-1]]
    oversize = sum(1 for e in wanted if e.size > max_file_bytes)
    py = [e for e in tree.entries if e.path.endswith(".py") and e.size <= max_file_bytes]
    blobs = gitsource.read_blobs(repo_path, sorted({e.sha for e in py}), max_bytes=max_file_bytes)
    files = {e.path: blobs[e.sha] for e in py if blobs.get(e.sha) is not None}
    # non-Python paths participate only so unsupported languages are reported
    for e in tree.entries:
        if not e.path.endswith(".py"):
            files.setdefault(e.path, b"")
    skipped = {"skipped_symlinks": tree.skipped_symlinks, "skipped_submodules": tree.skipped_submodules,
               "skipped_oversize": sum(1 for e in tree.entries if e.path.endswith(".py") and e.size > max_file_bytes)}
    graph = extract.build_graph(files, repository_id, commit, cfg, skipped)
    completeness = graph.pop("completeness")
    tree_hash = graph.pop("source_tree_hash")
    counts = graph.pop("counts")
    key = {"host_id": host_id, "repository_id": repository_id, "commit_sha": commit, "worktree_id": wid,
           "analyzer_version": identity.ANALYZER_VERSION, "config_hash": identity.hash_obj(cfg.as_dict()),
           "toolchain_hash": identity.hash_obj(toolchain())}
    snapshot = {**key, "snapshot_id": identity.snapshot_id(key), "status": "published",
                "completeness": completeness, "graph_hash": identity.graph_hash(graph),
                "source_tree_hash": tree_hash, "counts": counts, "oversize_skipped": oversize,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {"snapshot": snapshot, "graph": graph}
