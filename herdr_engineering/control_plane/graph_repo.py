"""P3 repository slice: emit repository/dependency lineage events.

EMITTER over the existing lineage ``GraphProjection`` (no own store). Maps
repositories and their cross-repo dependencies onto the shared node/edge kinds
(``repository`` node, ``dependency`` edge) and produces lineage events applied
via ``GraphProjection.apply``. Static manifest dependency parsing is included
as a pure helper (no network, no filesystem).

Mapping:
  repository -> node kind ``repository``  (label = repo name)
  depends_on -> edge kind ``dependency``  (repository -> repository)
  rename     -> attrs ``supersedes`` on the new repo node + edge ``supersession``
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .graph import from_stored

__all__ = ["parse_dependencies", "repo_lineage_events", "RepositoryGraphEmitter"]

# <name>[extras]... [op version]; markers are tolerated but only the name is kept.
# Handles PEP 508 extras (`requests[socks]>=2`), version specifiers and env markers.
_DEP_RE = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*"     # package name
    r"(?:\[[^]]*\])?\s*"              # optional extras [socks]
    r"(?:[<>=~!]{1,2}\s*\S+)?\s*"      # optional version specifier
    r"(?:;[^#\n]*)?$"                  # optional environment marker
)


def _parse_pyproject_entry(entry: Any) -> list[str]:
    """Expand one pyproject dependency entry (str or PEP 508 dict) to names."""
    if isinstance(entry, dict):
        entry = entry.get("name") or ""
    m = _DEP_RE.match(str(entry))
    return [m.group(1)] if m else []


def parse_dependencies(manifest: dict, manifest_type: str) -> list[str]:
    """Extract dependency names from a parsed manifest dict.

    ``manifest_type`` in {"pyproject", "package.json", "Cargo.toml"}.
    Returns plain dependency names (version specifiers stripped). Pure helper.
    """
    deps: set[str] = set()

    if manifest_type == "pyproject":
        project = manifest.get("project", {}) or {}
        for entry in project.get("dependencies") or []:
            deps.update(_parse_pyproject_entry(entry))
        # [project.optional-dependencies] is a MAPPING of extra -> list of deps
        for entries in (project.get("optional-dependencies") or {}).values():
            for entry in entries or []:
                deps.update(_parse_pyproject_entry(entry))
        # poetry style: [tool.poetry.dependencies] is a MAPPING name -> spec
        for name in (manifest.get("tool", {}).get("poetry", {}).get("dependencies") or {}):
            if isinstance(name, str):
                deps.add(name)

    elif manifest_type == "package.json":
        for table in ("dependencies", "devDependencies", "peerDependencies",
                      "optionalDependencies"):
            for name in (manifest.get(table) or {}):
                deps.add(str(name))

    elif manifest_type == "Cargo.toml":
        for name in (manifest.get("dependencies") or {}):
            deps.add(str(name))

    return sorted(deps)


class RepositoryGraphEmitter:
    """Emit repository + dependency lineage events."""

    def __init__(self, *, source: str = "git", actor: str = "key:system-graph",
                 scope: str = "org", seq: int = 0) -> None:
        self.source = source
        self.actor = actor
        self.scope = scope
        self.seq = seq

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def _stored(self, etype: str, payload: dict, mission_id: str | None = None) -> dict:
        payload = dict(payload)
        payload["_envelope"] = {"lineage_mode": "normal"}
        ev = {
            "event_type": etype, "payload": payload, "visibility_scope": self.scope,
            "source": self.source, "source_sequence": self._next(),
            "actor": {"id": self.actor, "kind": "agent", "authenticated_by": "api_key"},
            "event_id": f"{self.source}-{self.seq}", "occurred_at": "2026-10-22T10:00:00Z",
        }
        if mission_id:
            ev["mission_id"] = mission_id
        return from_stored(ev)

    @staticmethod
    def _repo_id(name: str) -> str:
        """Canonical repo identity is the forge name; never a bare short name."""
        return f"repository:{name}"

    def repo(self, name: str, *, depends_on: Iterable[str] = (),
             supersedes: str | None = None) -> list[dict]:
        """Emit a repository node (+ dependency edges, + optional supersession)."""
        node_id = self._repo_id(name)
        attrs: dict = {}
        if supersedes:
            attrs["supersedes"] = supersedes
        payload: dict = {"node_type": "repository", "node_id": node_id, "label": name}
        if attrs:
            payload["attrs"] = attrs
        events = [self._stored("node.upsert", payload)]
        for dep in depends_on:
            events.append(self._stored(
                "edge.add", {"edge_type": "dependency", "from_id": node_id,
                             "to_id": self._repo_id(dep)}))
        if supersedes:
            events.append(self._stored(
                "edge.add", {"edge_type": "supersession", "from_id": node_id,
                             "to_id": self._repo_id(supersedes)}))
        return events

    @staticmethod
    def apply_all(graph: Any, events: Iterable[dict]) -> None:
        for ev in events:
            graph.apply(ev)


def repo_lineage_events(emitter: RepositoryGraphEmitter, records: Iterable[dict]) -> list[dict]:
    """Expand repo records into lineage events.

    Each record: {"name": str, "depends_on": [..], "supersedes": str|None}.
    """
    events: list[dict] = []
    for rec in records:
        events += emitter.repo(rec["name"], depends_on=rec.get("depends_on", ()),
                               supersedes=rec.get("supersedes"))
    return events
