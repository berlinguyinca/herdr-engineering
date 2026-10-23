"""Control-plane side: validate, store and query immutable snapshots.

Reuses the repo's artifact store (graph bytes) and event log (metadata). A
snapshot is never updated: republishing the same key with the same graph hash
is an idempotent no-op, with a different hash a conflict.
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict

from ..control_plane.project_links import canonical_repository
from . import identity
from .extract import EDGE_KINDS, NODE_KINDS

ENTITY = "architecture_snapshot"
JOB_ENTITY = "architecture_job"
MAX_NODES = 250_000
MAX_EDGES = 1_000_000
MAX_LIMIT = 500
EVENTS = {"requested", "started", "progress", "completed", "failed", "cancelled"}
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}")
_REL = re.compile(r"(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\x00\\]{1,1024}")


class ArchitectureError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _validate(snapshot: dict, graph: dict) -> None:
    if not isinstance(snapshot, dict) or not isinstance(graph, dict):
        raise ArchitectureError("snapshot and graph must be objects")
    for f in identity.SNAPSHOT_KEY_FIELDS:
        if not isinstance(snapshot.get(f), str) or not snapshot[f]:
            raise ArchitectureError(f"snapshot.{f} is required")
    if not identity.is_full_sha(snapshot["commit_sha"]):
        raise ArchitectureError("commit_sha must be a full 40 or 64 character hexadecimal commit")
    if canonical_repository(snapshot["repository_id"]) != snapshot["repository_id"]:
        raise ArchitectureError("repository_id must be a canonical repository identity")
    if graph.get("schema") != identity.GRAPH_SCHEMA:
        raise ArchitectureError("unsupported graph schema")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ArchitectureError("graph.nodes and graph.edges must be arrays")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise ArchitectureError("graph exceeds the size limit", 413)
    if snapshot.get("snapshot_id") != identity.snapshot_id(snapshot):
        raise ArchitectureError("snapshot_id does not match its key fields")
    if snapshot.get("graph_hash") != identity.graph_hash(graph):
        raise ArchitectureError("graph_hash does not match the graph")
    ids = set()
    for n in nodes:
        if not isinstance(n, dict) or n.get("kind") not in NODE_KINDS or not isinstance(n.get("node_id"), str):
            raise ArchitectureError("invalid node")
        if n["node_id"] in ids:
            raise ArchitectureError("duplicate node_id")
        ids.add(n["node_id"])
        p = n.get("relative_path")
        if p is not None and not (isinstance(p, str) and _REL.fullmatch(p)):
            raise ArchitectureError("node path escapes the repository")
    for e in edges:
        if not isinstance(e, dict) or e.get("kind") not in EDGE_KINDS:
            raise ArchitectureError("invalid edge")
        if e.get("from_id") not in ids or e.get("to_id") not in ids:
            raise ArchitectureError("edge references an unknown node")
        if not isinstance(e.get("inferred"), bool):
            raise ArchitectureError("edge.inferred must be a boolean")
        if not e.get("extractor") or not isinstance(e.get("source_span"), dict):
            raise ArchitectureError("edge needs an extractor and a source span")


class _Loaded:
    def __init__(self, graph):
        self.nodes = {n["node_id"]: n for n in graph["nodes"]}
        self.children = defaultdict(list)
        for n in graph["nodes"]:
            self.children[n["parent_id"]].append(n["node_id"])
        self.out = defaultdict(list)
        self.inn = defaultdict(list)
        for e in graph["edges"]:
            self.out[e["from_id"]].append(e)
            self.inn[e["to_id"]].append(e)
        self.diagnostics = graph.get("diagnostics", [])
        self.graph = graph

    def ancestors(self, nid):
        while nid is not None:
            yield nid
            nid = self.nodes[nid]["parent_id"]


def _page(items, cursor, limit):
    try:
        start = max(0, int(cursor or 0))
        limit = max(1, min(int(limit or 100), MAX_LIMIT))
    except (TypeError, ValueError):
        raise ArchitectureError("invalid cursor or limit") from None
    page = items[start:start + limit]
    more = start + limit < len(items)
    return page, (str(start + limit) if more else None), len(items)


class ArchitectureService:
    def __init__(self, repo, enabled=False):
        self.repo = repo
        self.enabled = bool(enabled)
        self._meta: dict[str, dict] = {}
        self._graphs: dict[str, _Loaded] = {}
        self._lock = threading.Lock()
        for (etype, eid), stream in list(getattr(repo, "_events", {}).items()):
            if etype == ENTITY and stream:
                ev = stream[0]
                if ev.get("event_type") == "architecture.snapshot.published":
                    self._meta[eid] = ev["payload"]["snapshot"]

    # ------------------------------------------------------------ publish
    def publish(self, body: dict) -> dict:
        snapshot, graph = (body or {}).get("snapshot"), (body or {}).get("graph")
        _validate(snapshot, graph)
        sid = snapshot["snapshot_id"]
        with self._lock:
            have = self._meta.get(sid)
            if have:
                if have["graph_hash"] != snapshot["graph_hash"]:
                    raise ArchitectureError("snapshot exists with a different graph; snapshots are immutable", 409)
                return {"snapshot": have, "duplicate": True}
            data = identity.canonical_bytes(graph)
            aid = self.repo.register_artifact(original_filename=f"{sid}.graph.json", mime_type="application/json",
                                             data=data)
            art = self.repo.get_artifact(aid)
            meta = {k: snapshot[k] for k in (*identity.SNAPSHOT_KEY_FIELDS, "snapshot_id", "graph_hash", "completeness",
                                            "source_tree_hash", "counts", "created_at") if k in snapshot}
            meta["status"] = "published"
            meta["artifact_ref"] = {"artifact_id": aid, "sha256": art["sha256"], "size_bytes": art["size_bytes"],
                                    "repository": snapshot["repository_id"], "revision": snapshot["commit_sha"],
                                    "worktree_id": snapshot["worktree_id"], "machine_id": snapshot["host_id"]}
            meta["diagnostic_codes"] = sorted({d.get("code") for d in graph.get("diagnostics", []) if d.get("code")})
            self.repo.record_event(ENTITY, sid, "architecture.snapshot.published", {"snapshot": meta},
                                   require_durable=True)
            self._meta[sid] = meta
            self._graphs[sid] = _Loaded(graph)
            return {"snapshot": meta, "duplicate": False}

    def _load(self, sid) -> _Loaded:
        meta = self._meta.get(sid)
        if meta is None:
            raise KeyError(sid)
        g = self._graphs.get(sid)
        if g is None:
            data = self.repo.get_artifact_data(meta["artifact_ref"]["artifact_id"])
            if data is None:
                raise ArchitectureError("snapshot graph artifact is unavailable", 503)
            import hashlib
            import json
            if hashlib.sha256(data).hexdigest() != meta["artifact_ref"]["sha256"]:
                raise ArchitectureError("snapshot graph artifact failed its integrity check", 503)
            g = self._graphs[sid] = _Loaded(json.loads(data))
        return g

    # -------------------------------------------------------------- query
    def list_snapshots(self, repository_id=None, commit_sha=None, host_id=None, cursor=None, limit=50):
        rows = [m for m in self._meta.values()
                if (not repository_id or m["repository_id"] == repository_id)
                and (not commit_sha or m["commit_sha"].startswith(commit_sha))
                and (not host_id or m["host_id"] == host_id)]
        rows.sort(key=lambda m: (m["created_at"], m["snapshot_id"]), reverse=True)
        page, nxt, total = _page(rows, cursor, limit)
        return {"snapshots": page, "next_cursor": nxt, "total": total}

    def get_snapshot(self, sid):
        meta = self._meta.get(sid)
        if meta is None:
            raise KeyError(sid)
        g = self._load(sid)
        return {"snapshot": meta, "diagnostics": g.diagnostics, "root_id": (g.children.get(None) or [None])[0]}

    def children(self, sid, parent=None, cursor=None, limit=100):
        g = self._load(sid)
        pid = parent or (g.children.get(None) or [None])[0]
        if pid not in g.nodes:
            raise KeyError(pid)
        kids = sorted(g.children.get(pid, []), key=lambda i: (g.nodes[i]["kind"] not in ("package",), i))
        page, nxt, total = _page(kids, cursor, limit)
        return {"parent": g.nodes[pid], "nodes": [self._row(g, i) for i in page], "next_cursor": nxt, "total": total,
                "incomplete": nxt is not None, "edges": self._aggregate(g, page)}

    def _row(self, g, nid):
        n = dict(g.nodes[nid])
        n["child_count"] = len(g.children.get(nid, []))
        return n

    def _aggregate(self, g: _Loaded, visible: list[str]) -> list[dict]:
        """imports edges between the visible nodes, rolled up from their descendants."""
        vis = set(visible)
        agg: dict[tuple, dict] = {}

        def owner(nid):
            for a in g.ancestors(nid):
                if a in vis:
                    return a
            return None

        stack = list(visible)
        while stack:
            cur = stack.pop()
            stack.extend(g.children.get(cur, []))
            for e in g.out.get(cur, []):
                if e["kind"] == "contains":
                    continue
                a, b = owner(e["from_id"]), owner(e["to_id"])
                if a and b and a != b:
                    row = agg.setdefault((a, b, e["kind"]), {"from_id": a, "to_id": b, "kind": e["kind"], "count": 0,
                                                              "inferred": False, "sample": e["source_span"],
                                                              "evidence_ref": e["evidence_ref"]})
                    row["count"] += 1
                    row["inferred"] = row["inferred"] or e["inferred"]
        return sorted(agg.values(), key=lambda r: (-r["count"], r["from_id"], r["to_id"]))[:2000]

    def search(self, sid, q="", kind=None, cursor=None, limit=50):
        g = self._load(sid)
        ql = (q or "").lower().strip()
        rows = [n for n in g.nodes.values()
                if (not kind or n["kind"] == kind) and (not ql or ql in n["qualified_name"].lower()
                                                       or ql in (n["relative_path"] or "").lower())]
        rows.sort(key=lambda n: (len(n["qualified_name"]), n["node_id"]))
        page, nxt, total = _page(rows, cursor, limit)
        return {"nodes": page, "next_cursor": nxt, "total": total}

    def node(self, sid, nid):
        g = self._load(sid)
        if nid not in g.nodes:
            raise KeyError(nid)
        return {"node": self._row(g, nid), "ancestors": [g.nodes[a] for a in list(g.ancestors(nid))[1:]][::-1]}

    def expand(self, sid, nid, direction="both", kind=None, cursor=None, limit=100):
        g = self._load(sid)
        if nid not in g.nodes:
            raise KeyError(nid)
        edges = []
        if direction in ("out", "both"):
            edges += g.out.get(nid, [])
        if direction in ("in", "both"):
            edges += g.inn.get(nid, [])
        edges = [e for e in edges if e["kind"] != "contains" and (not kind or e["kind"] == kind)]
        edges.sort(key=lambda e: (e["kind"], e["from_id"], e["to_id"], e["edge_id"]))
        page, nxt, total = _page(edges, cursor, limit)
        ends = {i for e in page for i in (e["from_id"], e["to_id"])}
        return {"edges": page, "nodes": [g.nodes[i] for i in sorted(ends)], "next_cursor": nxt, "total": total,
                "incomplete": nxt is not None}

    def diff(self, base, head, cursor=None, limit=200):
        mb, mh = self._meta.get(base), self._meta.get(head)
        if mb is None or mh is None:
            raise KeyError(base if mb is None else head)
        if mb["repository_id"] != mh["repository_id"]:
            raise ArchitectureError("snapshots belong to different repositories")
        gb, gh = self._load(base), self._load(head)
        ek = lambda g: {(e["kind"], e["from_id"], e["to_id"]) for e in g.graph["edges"] if e["kind"] != "contains"}  # noqa: E731
        added = sorted(set(gh.nodes) - set(gb.nodes))
        removed = sorted(set(gb.nodes) - set(gh.nodes))
        changed = sorted(i for i in set(gb.nodes) & set(gh.nodes)
                         if gb.nodes[i]["metadata"].get("content_hash") != gh.nodes[i]["metadata"].get("content_hash"))
        eb, eh = ek(gb), ek(gh)
        items = ([{"change": "added", "type": "node", "id": i} for i in added]
                 + [{"change": "removed", "type": "node", "id": i} for i in removed]
                 + [{"change": "modified", "type": "node", "id": i} for i in changed]
                 + [{"change": "added", "type": "edge", "kind": k, "from_id": a, "to_id": b} for k, a, b in sorted(eh - eb)]
                 + [{"change": "removed", "type": "edge", "kind": k, "from_id": a, "to_id": b} for k, a, b in sorted(eb - eh)])
        page, nxt, total = _page(items, cursor, limit)
        warnings = ["a moved or renamed symbol appears as removed + added; rename detection is not implemented"]
        for m in (mb, mh):
            if m["completeness"] != "complete":
                warnings.append(f"snapshot {m['snapshot_id']} is {m['completeness']}: unsupported languages or edge kinds "
                                "are absent from both sides")
        return {"base": base, "head": head, "base_commit": mb["commit_sha"], "head_commit": mh["commit_sha"],
                "summary": {"nodes_added": len(added), "nodes_removed": len(removed), "nodes_modified": len(changed),
                            "edges_added": len(eh - eb), "edges_removed": len(eb - eh)},
                "items": page, "next_cursor": nxt, "total": total, "incomplete": nxt is not None, "warnings": warnings}

    # ----------------------------------------------------------- analyses
    def record_analysis(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ArchitectureError("body must be an object")
        aid, event = body.get("analysis_id"), body.get("event")
        if not isinstance(aid, str) or not _JOB_ID.fullmatch(aid):
            raise ArchitectureError("analysis_id is required")
        if event not in EVENTS:
            raise ArchitectureError("event must be one of " + ", ".join(sorted(EVENTS)))
        payload = {k: body[k] for k in ("repository_id", "commit_sha", "host_id", "worktree_id", "snapshot_id",
                                       "progress", "message") if k in body}
        for k in ("repository_id", "commit_sha", "host_id", "worktree_id", "snapshot_id", "message"):
            if k in payload and not (isinstance(payload[k], str) and len(payload[k]) <= 500):
                raise ArchitectureError(f"invalid {k}")
        if "commit_sha" in payload and not identity.is_full_sha(payload["commit_sha"]):
            raise ArchitectureError("commit_sha must be a full hexadecimal commit")
        if "progress" in payload and not (isinstance(payload["progress"], (int, float)) and 0 <= payload["progress"] <= 1):
            raise ArchitectureError("progress must be between 0 and 1")
        return self.repo.record_event(JOB_ENTITY, aid, f"architecture.analysis.{event}", payload,
                                      require_durable=event in {"completed", "failed", "cancelled"})

    def analyses(self, limit=50):
        out = []
        for (etype, eid), stream in list(getattr(self.repo, "_events", {}).items()):
            if etype != JOB_ENTITY or not stream:
                continue
            state = {"analysis_id": eid}
            for ev in stream:
                state.update(ev["payload"])
                state["state"] = ev["event_type"].rsplit(".", 1)[-1]
                state["updated_at"] = ev["ingest_timestamp"]
            out.append(state)
        out.sort(key=lambda s: s["updated_at"], reverse=True)
        return {"analyses": out[:max(1, min(int(limit), MAX_LIMIT))]}
