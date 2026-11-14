"""Coalesced rollout planning (D4, D5). Pure: no I/O."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

#: Fixed inventory order (D5): canary first.
HOST_ORDER = ("beast", "bender", "macbook-pro")
OPTIONAL_HOSTS = frozenset({"macbook-pro"})


def rollout_id(targets: Mapping[str, str]) -> str:
    """sha256 of the sorted `repo:sha` lines, first 16 hex chars; same heads -> same id."""
    text = "\n".join(f"{repo}:{sha}" for repo, sha in sorted(targets.items()))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def plan_rollout(verified: Mapping[str, str], converged: Mapping[str, str],
                 rollouts: Mapping[str, Mapping[str, object]]) -> dict | None:
    """One rollout over every verified head that differs from the last converged SHA.

    Returns None when there is nothing to do, or when this exact target set already succeeded or was abandoned
    (idempotent across restarts and ETag misses; a newer head is a different target set and plans normally).
    """
    targets = {repo: sha for repo, sha in verified.items() if converged.get(repo) != sha}
    if not targets:
        return None
    rid = rollout_id(targets)
    if rollouts.get(rid, {}).get("status") in ("succeeded", "abandoned"):    # abandoned: the operator said no to these heads
        return None
    return {
        "rollout_id": rid,
        "status": "planned",
        "targets": dict(sorted(targets.items())),
        "before": {repo: converged.get(repo) for repo in sorted(targets)},
        "hosts": [{"host": h, "optional": h in OPTIONAL_HOSTS} for h in HOST_ORDER],
    }
