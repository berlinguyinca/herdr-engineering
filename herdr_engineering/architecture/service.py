"""Control-plane side: validate, store and query immutable snapshots.

Reuses the repo's artifact store (graph bytes) and event log (metadata). A
snapshot is never updated: republishing the same key with the same graph hash
is an idempotent no-op, with a different hash a conflict.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import defaultdict

from ..control_plane.project_links import canonical_repository
from . import crossrepo, explain as ex, identity, overlays as ov
from .extract import EDGE_KINDS, NODE_KINDS

ENTITY = "architecture_snapshot"
JOB_ENTITY = "architecture_job"
LINK_ENTITY = "architecture_link"
MAX_LINKS_PER_ROOM = 200
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
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
        self.gate_store_fn = lambda: None          # set by the server when the review gate is running
        self.explain_config = None                 # set from control_plane.architecture.explain (off by default)
        self.explain_key = None
        self._meta: dict[str, dict] = {}
        self._graphs: dict[str, _Loaded] = {}
        self._jobs: dict[str, dict] = {}
        self._links: dict[str, list[dict]] = defaultdict(list)       # room_id -> links (annotations)
        self._lock = threading.RLock()
        for (etype, eid), stream in list(getattr(repo, "_events", {}).items()):
            if etype == ENTITY and stream:
                ev = stream[0]
                if ev.get("event_type") == "architecture.snapshot.published":
                    self._meta[eid] = ev["payload"]["snapshot"]
            elif etype == JOB_ENTITY:
                for ev in stream:
                    self._apply(ev)
            elif etype == LINK_ENTITY:
                self._links[eid] = [ev["payload"] for ev in stream if ev["event_type"] == "architecture.link.created"]

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
    def _apply(self, ev: dict) -> dict:
        p, kind = ev["payload"], ev["event_type"].rsplit(".", 1)[-1]
        job = self._jobs.setdefault(ev["entity_id"], {"analysis_id": ev["entity_id"], "created_at": ev["ingest_timestamp"]})
        job.update(p)
        job["state"], job["updated_at"] = kind, ev["ingest_timestamp"]
        if kind == "requested":
            job["created_at"] = ev["ingest_timestamp"]
        return job

    def _open(self, job) -> bool:
        if job["state"] == "requested":
            return True
        return job["state"] in ("started", "progress") and job.get("lease_expires_at", 0) > time.time()

    @staticmethod
    def _request_key(repository_id, host_id, rev) -> str:
        return identity.hash_obj([repository_id, host_id, rev])[:24]

    def request_analysis(self, body: dict) -> dict:
        """Ask a host to analyse a revision. Identical open requests collapse; known snapshots are returned."""
        if not isinstance(body, dict):
            raise ArchitectureError("body must be an object")
        repo_id, host_id, rev = body.get("repository_id"), body.get("host_id"), body.get("rev", "HEAD")
        if canonical_repository(repo_id) != repo_id:
            raise ArchitectureError("repository_id must be a canonical repository identity")
        if not (isinstance(host_id, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", host_id)):
            raise ArchitectureError("host_id is required")
        if not (isinstance(rev, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", rev)):
            raise ArchitectureError("rev must be a branch, tag or commit name")
        prio = body.get("priority", "interactive")
        if prio not in ("interactive", "background"):
            raise ArchitectureError("priority must be interactive or background")
        key = self._request_key(repo_id, host_id, rev)
        with self._lock:
            if identity.is_full_sha(rev):
                known = [m for m in self._meta.values() if m["repository_id"] == repo_id and m["host_id"] == host_id
                         and m["commit_sha"] == rev]
                if known:
                    return {"analysis_id": None, "deduplicated": True, "snapshot_id": known[0]["snapshot_id"]}
            for job in self._jobs.values():
                if job.get("key") == key and self._open(job):
                    return {"analysis_id": job["analysis_id"], "deduplicated": True, "state": job["state"]}
            aid = "an-" + uuid.uuid4().hex[:16]
            ev = self.repo.record_event(JOB_ENTITY, aid, "architecture.analysis.requested",
                                        {"repository_id": repo_id, "host_id": host_id, "rev": rev, "priority": prio,
                                         "key": key})
            self._apply(ev)
            return {"analysis_id": aid, "deduplicated": False, "state": "requested"}

    def claim_analysis(self, body: dict) -> dict:
        """A host takes the oldest requested job for itself (interactive first) under a lease."""
        host_id = (body or {}).get("host_id")
        if not (isinstance(host_id, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", host_id)):
            raise ArchitectureError("host_id is required")
        lease = max(30, min(int((body or {}).get("lease_seconds", 600)), 3600))
        with self._lock:
            now = time.time()
            ready = [j for j in self._jobs.values() if j.get("host_id") == host_id and (
                j["state"] == "requested" or (j["state"] in ("started", "progress") and j.get("lease_expires_at", 0) <= now))]
            if not ready:
                return {"job": None}
            ready.sort(key=lambda j: (j.get("priority") != "interactive", j["created_at"], j["analysis_id"]))
            job = ready[0]
            ev = self.repo.record_event(JOB_ENTITY, job["analysis_id"], "architecture.analysis.started",
                                        {"host_id": host_id, "lease_expires_at": now + lease})
            self._apply(ev)
            return {"job": {k: job[k] for k in ("analysis_id", "repository_id", "host_id", "rev", "priority") if k in job},
                    "lease_expires_at": now + lease}

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
        with self._lock:
            cur = self._jobs.get(aid)
            if cur and cur["state"] in ("completed", "failed", "cancelled"):
                raise ArchitectureError("analysis already finished", 409)
            if event in ("started", "progress") and "lease_expires_at" not in payload:
                payload["lease_expires_at"] = time.time() + 600     # a heartbeat extends the lease
            ev = self.repo.record_event(JOB_ENTITY, aid, f"architecture.analysis.{event}", payload,
                                        require_durable=event in {"completed", "failed", "cancelled"})
            self._apply(ev)
        return ev

    def analyses(self, limit=50, analysis_id=None):
        if analysis_id:
            job = self._jobs.get(analysis_id)
            if job is None:
                raise KeyError(analysis_id)
            return {"analysis": dict(job)}
        rows = sorted(self._jobs.values(), key=lambda s: s["updated_at"], reverse=True)
        return {"analyses": [dict(j) for j in rows[:max(1, min(int(limit), MAX_LIMIT))]]}

    # ----------------------------------------------------------- overlays / correlation
    def overlays(self, sid):
        meta = self._meta.get(sid)
        if meta is None:
            raise KeyError(sid)
        return ov.overlays(self.repo, self.gate_store_fn(), meta)

    def _previous(self, meta):
        """The newest earlier snapshot of the same repository on the same host (the comparison base for 'what changed')."""
        older = [m for m in self._meta.values() if m["repository_id"] == meta["repository_id"] and m["host_id"] == meta["host_id"]
                 and m["snapshot_id"] != meta["snapshot_id"] and m["commit_sha"] != meta["commit_sha"]
                 and m["created_at"] <= meta["created_at"]]
        return max(older, key=lambda m: (m["created_at"], m["snapshot_id"]), default=None)

    def _changed_nodes(self, meta) -> tuple[dict, dict | None]:
        prev = self._previous(meta)
        if prev is None:
            return {}, None
        gb, gh = self._load(prev["snapshot_id"]), self._load(meta["snapshot_id"])
        out = {i: "added" for i in set(gh.nodes) - set(gb.nodes)}
        out.update({i: "modified" for i in set(gb.nodes) & set(gh.nodes)
                    if gb.nodes[i]["metadata"].get("content_hash") != gh.nodes[i]["metadata"].get("content_hash")})
        return out, prev

    def correlations(self, sid, node=None, cursor=None, limit=100):
        meta = self._meta.get(sid)
        if meta is None:
            raise KeyError(sid)
        o = ov.overlays(self.repo, self.gate_store_fn(), meta)
        prs = [p["pr_ref"] for p in o["pull_requests"]]
        by_id = {s["session_id"]: s for s in self.repo.list_sessions()}
        rows, notes = [], []
        change = None
        if node:
            changed, prev = self._changed_nodes(meta)
            if node not in self._load(sid).nodes:
                raise KeyError(node)
            change = changed.get(node, "unchanged" if prev else "unknown")
            if prev is None:
                notes.append("no earlier snapshot of this repository on this host: changes cannot be attributed")
        for s in o["sessions"]:
            rec = ov.correlation(meta, by_id.get(s["session_id"]), node=node, pr_refs=prs)
            if node:
                rec["change"] = change
            rows.append(rec)
        if node and change in ("unchanged", "unknown"):
            rows = []            # an agent at this commit is not related to a symbol it did not change
        if rows:
            notes.append("attribution is commit-level: every agent recorded at this commit shares it; per-agent symbol "
                         "authorship is not recorded")
        page, nxt, total = _page(rows, cursor, limit)
        return {"correlations": page, "next_cursor": nxt, "total": total, "notes": notes, "change": change}

    def session_changes(self, session_id, limit=200):
        s = next((x for x in self.repo.list_sessions() if x["session_id"] == session_id), None)
        if s is None:
            raise KeyError(session_id)
        repo_id, commit = ov.pl._identity(s), ov.pl._commit({"commit_sha": s.get("commit_sha")})
        snaps = [m for m in self._meta.values() if m["repository_id"] == repo_id and m["commit_sha"] == commit]
        if not snaps:
            return {"session_id": session_id, "snapshot": None, "changes": [],
                    "notes": ["no snapshot exists for this session's repository and commit"]}
        meta = sorted(snaps, key=lambda m: (m["created_at"], m["snapshot_id"]))[-1]
        changed, prev = self._changed_nodes(meta)
        g = self._load(meta["snapshot_id"])
        items = [{"node_id": i, "change": c, "qualified_name": g.nodes[i]["qualified_name"], "kind": g.nodes[i]["kind"],
                  "relative_path": g.nodes[i]["relative_path"], "line_start": g.nodes[i]["line_start"]}
                 for i, c in sorted(changed.items()) if g.nodes[i]["kind"] in ("class", "function", "module")]
        return {"session_id": session_id, "snapshot_id": meta["snapshot_id"], "base_snapshot_id": prev and prev["snapshot_id"],
                "changes": items[:max(1, min(int(limit), MAX_LIMIT))], "total": len(items), "incomplete": len(items) > limit,
                "notes": ([] if prev else ["no earlier snapshot to compare against"])
                + ["attribution is commit-level, not per-agent authorship"]}

    # ----------------------------------------------------------- room annotations
    def add_room_link(self, room_id: str, body: dict, author: str) -> dict:
        """Called by CollabService AFTER it authorised the caller for this room. Links, never copies, graph data."""
        bad = [k for k in (body or {}) if k not in ("snapshot_id", "node_id", "note", "idempotency_key")]
        if bad:
            raise ArchitectureError("unknown field")
        sid, nid, note, key = (body or {}).get("snapshot_id"), body.get("node_id"), body.get("note", ""), body.get("idempotency_key")
        if not (isinstance(sid, str) and isinstance(nid, str)):
            raise ArchitectureError("snapshot_id and node_id are required")
        if not isinstance(note, str) or len(note) > 500:
            raise ArchitectureError("note must be at most 500 characters")
        if key is not None and not (isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key)):
            raise ArchitectureError("invalid idempotency_key")
        g = self._load(sid)                      # KeyError -> 404
        if nid not in g.nodes:
            raise KeyError(nid)
        with self._lock:
            rows = self._links[room_id]
            link_id = "lnk_" + (identity.hash_obj([room_id, author, key])[:24] if key else uuid.uuid4().hex[:24])
            prior = next((r for r in rows if r["link_id"] == link_id), None)
            if prior:
                if (prior["snapshot_id"], prior["node_id"], prior["note"]) != (sid, nid, _CONTROL.sub("", note).strip()):
                    raise ArchitectureError("idempotency_key already used for a different link", 409)
                return {"link_id": link_id, "replayed": True}
            if len(rows) >= MAX_LINKS_PER_ROOM:
                raise ArchitectureError("this room has too many architecture links", 400)
            rec = {"link_id": link_id, "snapshot_id": sid, "node_id": nid, "note": _CONTROL.sub("", note).strip(),
                   "author": author, "created_at": self.repo._stamp()}
            self.repo.record_event(LINK_ENTITY, room_id, "architecture.link.created", rec, require_durable=True)
            rows.append(rec)
            return {"link_id": link_id, "replayed": False}

    def room_links(self, room_id: str, redact: bool = False) -> dict:
        out = []
        for r in self._links.get(room_id, []):
            row = {"link_id": r["link_id"], "note": r["note"], "created_at": r["created_at"]}
            if not redact:
                meta = self._meta.get(r["snapshot_id"])
                node = None
                try:
                    node = self._load(r["snapshot_id"]).nodes.get(r["node_id"]) if meta else None
                except (KeyError, ArchitectureError):
                    node = None
                row.update(snapshot_id=r["snapshot_id"], node_id=r["node_id"], author=r["author"],
                           commit_sha=meta and meta["commit_sha"], available=node is not None,
                           qualified_name=node and node["qualified_name"], kind=node and node["kind"],
                           relative_path=node and node["relative_path"], line_start=node and node["line_start"])
            out.append(row)
        return {"links": out, "redacted": redact}

    def node_rooms(self, sid: str, nid: str) -> dict:
        """Which rooms discuss this entity (ids only; the notes stay behind the room's own permissions)."""
        return {"rooms": sorted({rid for rid, rows in self._links.items() for r in rows
                                 if r["snapshot_id"] == sid and r["node_id"] == nid})}

    # ----------------------------------------------------------- cross-repository
    def cross_repo(self, snapshot_ids, mappings=None):
        if not snapshot_ids:
            raise ArchitectureError("pass at least two snapshot ids")
        pairs = []
        for sid in dict.fromkeys(snapshot_ids):
            if sid not in self._meta:
                raise KeyError(sid)
            pairs.append((self._meta[sid], self._load(sid)))
        if len(pairs) < 2:
            raise ArchitectureError("pass at least two snapshot ids")
        try:
            out = crossrepo.resolve(pairs, mappings)
        except ValueError as exc:
            raise ArchitectureError(str(exc)) from exc
        out["snapshots"] = [{"snapshot_id": m["snapshot_id"], "repository_id": m["repository_id"],
                             "commit_sha": m["commit_sha"], "completeness": m["completeness"]} for m, _ in pairs]
        out["warnings"] = ["name-match edges are inferred: a package name alone does not prove which project provides it"]
        return out

    # ----------------------------------------------------------- explanations
    def explain(self, sid, nid):
        if not self.explain_config:
            raise ArchitectureError("explanations are not configured on this control plane", 404)
        g = self._load(sid)
        if nid not in g.nodes:
            raise KeyError(nid)
        try:
            return ex.explain(self.explain_config, g, nid, self.explain_key)
        except ex.ExplainError as exc:
            raise ArchitectureError(str(exc), 502) from exc

    # ----------------------------------------------------------- timeline
    def timeline(self, repository_id, limit=200):
        """Two independent tracks for one repository. A correlation link exists only where a session's recorded
        commit equals a snapshot's commit; sessions without an exact commit stay on their own track, unlinked."""
        if ov.pl.canonical_repository(repository_id) != repository_id:
            raise ArchitectureError("repository_id must be a canonical repository identity")
        snaps = sorted((m for m in self._meta.values() if m["repository_id"] == repository_id),
                       key=lambda m: (m["created_at"], m["snapshot_id"]))
        by_commit: dict[str, list[str]] = defaultdict(list)
        for m in snaps:
            by_commit[m["commit_sha"]].append(m["snapshot_id"])
        sessions = []
        for sess in self.repo.list_sessions():
            if ov.pl._identity(sess) != repository_id:
                continue
            commit = ov.pl._commit({"commit_sha": sess.get("commit_sha")})
            sessions.append({"track": "agents", "at": sess.get("created_at"), "session_id": sess["session_id"],
                             "agent_role": sess.get("agent_role"), "parent_session_id": sess.get("parent_session_id"),
                             "mission_id": sess.get("mission_key") or sess.get("mission_id"), "host_id": sess.get("host_id"),
                             "commit_sha": commit, "snapshot_ids": by_commit.get(commit, []) if commit else []})
        sessions.sort(key=lambda r: (str(r["at"]), r["session_id"]))
        arch = [{"track": "architecture", "at": m["created_at"], "snapshot_id": m["snapshot_id"], "commit_sha": m["commit_sha"],
                 "host_id": m["host_id"], "completeness": m["completeness"],
                 "session_ids": [x["session_id"] for x in sessions if x["commit_sha"] == m["commit_sha"]][:20]} for m in snaps]
        n = max(1, min(int(limit), MAX_LIMIT))
        return {"repository_id": repository_id, "architecture": arch[-n:], "agents": sessions[-n:],
                "note": "tracks are independent; links exist only for an exact repository + full commit match"}
