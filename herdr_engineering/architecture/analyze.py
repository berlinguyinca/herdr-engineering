"""Compose git source + extractor + identity into a publishable snapshot (runs in the worker).

Incremental mode reuses cached per-file *facts* (parse results) keyed by analyzer version, config hash,
toolchain hash, path and git blob sha. Resolution across files always runs on the full fact set, so an
incremental run cannot differ from a full one; a stale or changed input is simply a cache miss.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import tempfile
from datetime import datetime, timezone

from . import extract, gitsource, identity


def toolchain() -> dict:
    return {"python": f"{sys.version_info[0]}.{sys.version_info[1]}", "impl": platform.python_implementation(),
            "parser": "ast+js-import-regex"}


class FactsCache:
    """On-disk facts cache owned by the worker host. Never inside the analysed repository."""

    def __init__(self, root: str | None, scope: str):
        self.root, self.scope = root, scope
        if root:
            os.makedirs(root, mode=0o700, exist_ok=True)

    def _file(self, path: str, blob: str) -> str:
        key = hashlib.sha256(f"{self.scope}\0{path}\0{blob}".encode()).hexdigest()
        return os.path.join(self.root, key[:2], key + ".json")

    def get(self, path: str, blob: str):
        if not self.root:
            return None
        try:
            with open(self._file(path, blob), encoding="utf-8") as fh:
                facts = json.load(fh)
            return facts if isinstance(facts, dict) and facts.get("path") == path else None
        except (OSError, ValueError):
            return None

    def put(self, path: str, blob: str, facts: dict) -> None:
        if not self.root:
            return
        target = self._file(path, blob)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(facts, fh, separators=(",", ":"))
        os.replace(tmp, target)


def analyze_commit(repo_path: str, rev: str, host_id: str, repository_id: str,
                   max_file_bytes: int = 1_000_000, max_files: int = 20_000,
                   max_nodes: int = 200_000, worktree_id: str | None = None,
                   cache_dir: str | None = None) -> dict:
    cfg = extract.Config(max_file_bytes=max_file_bytes, max_files=max_files, max_nodes=max_nodes)
    commit = gitsource.resolve_commit(repo_path, rev)
    wid = worktree_id or gitsource.worktree_identity(repo_path)["worktree_id"]
    tree = gitsource.list_tree(repo_path, commit)
    config_hash, tool_hash = identity.hash_obj(cfg.as_dict()), identity.hash_obj(toolchain())
    cache = FactsCache(cache_dir, f"{identity.ANALYZER_VERSION}\0{config_hash}\0{tool_hash}")
    facts: list[dict] = []
    other: dict[str, int] = {}
    oversize = 0
    need: list = []
    reused = 0
    for e in tree.entries:
        is_src = e.path.endswith(".py") or extract.is_js(e.path)
        if is_src:
            if e.size > max_file_bytes:
                oversize += 1
                continue
            hit = cache.get(e.path, e.sha)
            if hit is not None:
                facts.append(hit)
                reused += 1
            else:
                need.append(e)
        elif "." in e.path.rsplit("/", 1)[-1]:
            lang = extract.UNSUPPORTED_SUFFIXES.get("." + e.path.rsplit(".", 1)[-1])
            if lang:
                other[lang] = other.get(lang, 0) + 1
    blobs = gitsource.read_blobs(repo_path, sorted({e.sha for e in need}), max_bytes=max_file_bytes)
    for e in need:
        raw = blobs.get(e.sha)
        if raw is None:
            oversize += 1
            continue
        f = extract.extract_file_facts(e.path, raw)
        cache.put(e.path, e.sha, f)
        facts.append(f)
    skipped = {"skipped_symlinks": tree.skipped_symlinks, "skipped_submodules": tree.skipped_submodules,
               "skipped_oversize": oversize}
    graph = extract.assemble(facts, repository_id, cfg, skipped, other)
    completeness = graph.pop("completeness")
    tree_hash = graph.pop("source_tree_hash")
    counts = graph.pop("counts")
    key = {"host_id": host_id, "repository_id": repository_id, "commit_sha": commit, "worktree_id": wid,
           "analyzer_version": identity.ANALYZER_VERSION, "config_hash": config_hash, "toolchain_hash": tool_hash}
    snapshot = {**key, "snapshot_id": identity.snapshot_id(key), "status": "published",
                "completeness": completeness, "graph_hash": identity.graph_hash(graph),
                "source_tree_hash": tree_hash, "counts": counts, "oversize_skipped": oversize,
                "reuse": {"files": len(facts), "reused": reused, "parsed": len(facts) - reused},
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return {"snapshot": snapshot, "graph": graph}
