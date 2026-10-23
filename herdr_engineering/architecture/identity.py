"""Snapshot identity and canonical serialization.

A snapshot is keyed by host, repository, commit SHA, worktree, analyzer version,
config hash and toolchain hash. A branch name is never part of the key. The
graph itself carries none of host/worktree, so the same commit analysed with the
same config yields the same ``graph_hash`` everywhere.
"""
from __future__ import annotations

import hashlib
import json
import re

ANALYZER_VERSION = "herdr-arch-python-ast/1"
GRAPH_SCHEMA = "herdr.architecture.graph.v1"
SNAPSHOT_KEY_FIELDS = ("host_id", "repository_id", "commit_sha", "worktree_id",
                       "analyzer_version", "config_hash", "toolchain_hash")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def canonical_bytes(obj) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8", errors="surrogatepass")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj) -> str:
    return sha256_hex(canonical_bytes(obj))


def is_full_sha(value) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def snapshot_id(key: dict) -> str:
    missing = [f for f in SNAPSHOT_KEY_FIELDS if not isinstance(key.get(f), str) or not key.get(f)]
    if missing:
        raise ValueError(f"snapshot key is missing: {', '.join(missing)}")
    return "arch_" + hash_obj({f: key[f] for f in SNAPSHOT_KEY_FIELDS})[:32]


def graph_hash(graph: dict) -> str:
    return hash_obj(graph)
