"""Automatic placement of specialist assistants on fabric hosts and Slurm clusters.

Pure functions over data the runner attested or the operator configured; nothing here reads a clock, a network or a
file. Capabilities and resources CONSTRAIN where an agent may run. They never grant a tool, network or display: the
sandbox profile alone decides what a runtime can reach.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

CAPABILITIES = frozenset({
    "display", "desktop", "internet", "vm", "gpu",
    "os:linux", "os:macos", "arch:x86_64", "arch:arm64",
})
IMPLIES = {"desktop": {"display"}}
RESOURCE_KEYS = ("cpus", "memory_mb", "gpus")
MAX_RESOURCE = {"cpus": 512, "memory_mb": 4_194_304, "gpus": 16}


class PlacementError(ValueError):
    pass


def normalize_capabilities(value: Any, *, what: str = "capabilities") -> list[str]:
    """Closed vocabulary: an unknown name is an error, so a typo can never match nothing or everything."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)) or len(value) > 32:
        raise PlacementError(f"{what} must be a list")
    out: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in CAPABILITIES:
            raise PlacementError(f"unknown capability in {what}")
        out.add(item)
        out |= IMPLIES.get(item, set())
    return sorted(out)


def normalize_resources(value: Any, *, what: str = "resources") -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(RESOURCE_KEYS):
        raise PlacementError(f"{what} may only set {', '.join(RESOURCE_KEYS)}")
    out: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= MAX_RESOURCE[key]:
            raise PlacementError(f"invalid {what} value for {key}")
        out[key] = raw
    return out


def requirements(type_requires: Iterable[str], extra: Iterable[str] | None = None) -> list[str]:
    """A caller may add to a type's minimum, never relax it."""
    return normalize_capabilities(list(type_requires) + list(extra or []))


# --------------------------------------------------------------------------------------------- candidates
# A candidate is one place a runtime could run. ``kind`` is "host" (an attested fabric host) or "slurm" (a configured
# cluster partition). Both carry the same fields so one resolver ranks them.
#   id, kind, label, status ("ready"|"stale"|"unavailable"), capabilities, free{cpus,memory_mb,gpus},
#   attested_at (epoch seconds), profiles[], models[], role_bindings[], queue_wait_s (slurm only, estimate)
#   nodes[] (optional): per-node free {cpus, memory_mb, gpus}; when present one node must cover the whole request

def _fits(free: Mapping[str, int], need: Mapping[str, int]) -> list[str]:
    return [key for key in RESOURCE_KEYS if need.get(key, 0) > int(free.get(key, 0))]


def _shortfall(candidate: Mapping[str, Any], need: Mapping[str, int]) -> list[str]:
    """What stops the candidate carrying the request. A candidate that lists per-node capacity (a cluster partition)
    fits only when ONE node covers every resource at once, since a runtime lives on a single node; the others are
    judged on their aggregate free resources."""
    nodes = candidate.get("nodes")
    if nodes is None:
        return _fits(candidate.get("free", {}), need)
    if not any(not _fits(node, need) for node in nodes):
        best = max(nodes, key=lambda n: (int(n.get("cpus", 0)), int(n.get("memory_mb", 0))), default={})
        return _fits(best, need) or list(RESOURCE_KEYS[:1])      # no single node has it all: report the closest
    return []


def evaluate(candidate: Mapping[str, Any], *, needs: list[str], resources: Mapping[str, int],
             profile: str | None = None, role: str | None = None) -> list[str]:
    """Why this candidate cannot carry the request; an empty list means it can."""
    why: list[str] = []
    if candidate.get("status") != "ready":
        why.append(f"not ready ({candidate.get('status', 'unknown')})")
    have = set(candidate.get("capabilities", ()))
    missing = [cap for cap in needs if cap not in have]
    if missing:
        why.append("missing " + ", ".join(missing))
    short = _shortfall(candidate, resources)
    if short:
        why.append("not enough " + ", ".join(short))
    if profile and profile not in candidate.get("profiles", ()):
        why.append(f"no sandbox profile {profile}")
    if role and role not in candidate.get("role_bindings", ()):
        why.append("role not bound")
    return why


def place(candidates: Iterable[Mapping[str, Any]], *, needs: list[str], resources: Mapping[str, int],
          profile: str | None = None, role: str | None = None) -> dict[str, Any]:
    """Deterministic choice. Fabric hosts that can start now beat a cluster queue; then the freshest attestation,
    then the most free capacity, then the id. Returns the chosen candidate id (or None) and every rejection reason."""
    chosen: list[Mapping[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for cand in candidates:
        why = evaluate(cand, needs=needs, resources=resources, profile=profile, role=role)
        if why:
            rejected.append({"id": cand.get("id"), "kind": cand.get("kind"), "reasons": why})
        else:
            chosen.append(cand)

    def rank(cand: Mapping[str, Any]):
        free = cand.get("free", {})
        return (cand.get("kind") == "slurm",                         # start now before queueing
                int(cand.get("queue_wait_s", 0)),
                -float(cand.get("attested_at", 0)),
                -(int(free.get("cpus", 0)) + int(free.get("memory_mb", 0)) // 1024),
                str(cand.get("id")))
    chosen.sort(key=rank)
    return {"chosen": chosen[0]["id"] if chosen else None,
            "candidates": [{"id": c["id"], "kind": c.get("kind"), "label": c.get("label", c["id"])} for c in chosen],
            "rejected": sorted(rejected, key=lambda r: str(r["id"])),
            "needs": needs, "resources": dict(resources)}
