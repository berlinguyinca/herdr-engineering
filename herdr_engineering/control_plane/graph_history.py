"""P6 history/scale: diff and impact analysis over graph snapshots.

Provides snapshot/diff/impact primitives on top of the existing lineage
``GraphProjection.snapshot()`` so operators can:

  * diff two snapshots (before/after a rollout or incident),
  * run impact analysis (what nodes/edges a changed node can reach),
  * reason about SLO/scale targets.

All operations are pure functions over ``snapshot()`` dicts. The graph remains
a read model (no mutation of authoritative state).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

__all__ = ["diff", "impact", "Diff"]


@dataclass(frozen=True)
class Diff:
    added_nodes: tuple[str, ...]
    removed_nodes: tuple[str, ...]
    added_edges: tuple[tuple[str, str, str], ...]   # (kind, from, to)
    removed_edges: tuple[tuple[str, str, str], ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added_nodes or self.removed_nodes
                    or self.added_edges or self.removed_edges)

    def summary(self) -> dict[str, int]:
        return {"added_nodes": len(self.added_nodes),
                "removed_nodes": len(self.removed_nodes),
                "added_edges": len(self.added_edges),
                "removed_edges": len(self.removed_edges)}


def _edges_triples(edges: dict) -> set[tuple]:
    out: set[tuple] = set()
    for e in edges.values():
        out.add((e.get("edge_type"), e.get("from_id"), e.get("to_id")))
    return out


def diff(before: dict, after: dict) -> Diff:
    """Diff two ``snapshot()`` dicts (before -> after)."""
    bn = set((before.get("nodes") or {}).keys())
    an = set((after.get("nodes") or {}).keys())
    be = _edges_triples(before.get("edges") or {})
    ae = _edges_triples(after.get("edges") or {})
    return Diff(
        added_nodes=tuple(sorted(an - bn)),
        removed_nodes=tuple(sorted(bn - an)),
        added_edges=tuple(sorted(ae - be)),
        removed_edges=tuple(sorted(be - ae)),
    )


def impact(snapshot: dict, node_id: str, *, max_depth: int = 5) -> list[str]:
    """Return node ids reachable from ``node_id`` following edges (impact
    closure). Uses the snapshot's own node keys so results are real ids."""
    edges = snapshot.get("edges") or {}
    nodes = snapshot.get("nodes") or {}
    # adjacency: from_id -> set of to_ids
    adj: dict[str, set[str]] = {}
    for e in edges.values():
        f, t = e.get("from_id"), e.get("to_id")
        if f and t:
            adj.setdefault(f, set()).add(t)
    seen: set[str] = set()
    frontier = {node_id}
    for _ in range(max_depth):
        nxt: set[str] = set()
        for f in frontier:
            if f in seen:
                continue
            seen.add(f)
            nxt |= adj.get(f, set()) - seen
        frontier = nxt
        if not frontier:
            break
    return sorted(seen)
