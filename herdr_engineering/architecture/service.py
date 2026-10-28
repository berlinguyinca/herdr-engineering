"""Control-plane side: validate, store and query immutable snapshots.

Reuses the repo's artifact store (graph bytes) and event log (metadata). A
snapshot is never updated: republishing the same key with the same graph hash
is an idempotent no-op, with a different hash a conflict.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from datetime import datetime

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
FINISHED = frozenset({"completed", "failed", "cancelled", "expired"})
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}")
_HOST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# A repository-relative path: not absolute, no `..` segment, no backslash, no control character (a newline
# used to end the `.*` lookahead early, so "x\n../y" passed).
_REL = re.compile(r"(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\x00-\x1f\x7f\\]{1,1024}", re.DOTALL)
# Defaults for the YAML-configurable knobs (control_plane.architecture.*).
DEFAULT_CACHE_GRAPHS = 8
DEFAULT_CACHE_BYTES = 256 * 1024 * 1024
DEFAULT_REQUEST_TTL = 3600
_NODE_FIELDS = ("node_id", "kind", "qualified_name", "parent_id", "relative_path", "line_start", "line_end", "metadata")
_EDGE_FIELDS = ("edge_id", "kind", "from_id", "to_id", "source_span", "evidence_ref", "extractor", "inferred")


class ArchitectureError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _opt(value, kind) -> bool:
    return value is None or (isinstance(value, kind) and not isinstance(value, bool))


def _rel_ok(path) -> bool:
    return path is None or (isinstance(path, str) and _REL.fullmatch(path) is not None)


def _check_structure(nodes: dict, children: dict) -> None:
    """Exactly one root, and every node reachable from it: with all parents existing that also proves no cycle."""
    roots = children.get(None, [])
    if len(roots) != 1:
        raise ArchitectureError("graph must have exactly one root node")
    seen, stack = {roots[0]}, [roots[0]]
    while stack:
        for kid in children.get(stack.pop(), ()):
            if kid not in seen:
                seen.add(kid)
                stack.append(kid)
    if len(seen) != len(nodes):
        raise ArchitectureError("graph has a parent cycle")


def _validate(snapshot: dict, graph: dict) -> None:
    """Deep validation; runs before anything is stored, so a malformed snapshot is never persisted."""
    if not isinstance(snapshot, dict) or not isinstance(graph, dict):
        raise ArchitectureError("snapshot and graph must be objects")
    for f in identity.SNAPSHOT_KEY_FIELDS:
        if not isinstance(snapshot.get(f), str) or not snapshot[f]:
            raise ArchitectureError(f"snapshot.{f} is required")
    if not identity.is_full_sha(snapshot["commit_sha"]):
        raise ArchitectureError("commit_sha must be a full 40 or 64 character hexadecimal commit")
    if canonical_repository(snapshot["repository_id"]) != snapshot["repository_id"]:
        raise ArchitectureError("repository_id must be a canonical repository identity")
    if not _HOST_ID.fullmatch(snapshot["host_id"]):
        raise ArchitectureError("snapshot.host_id is invalid")
    if graph.get("schema") != identity.GRAPH_SCHEMA:
        raise ArchitectureError("unsupported graph schema")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ArchitectureError("graph.nodes and graph.edges must be arrays")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise ArchitectureError("graph exceeds the size limit", 413)
    diags = graph.get("diagnostics", [])
    if not isinstance(diags, list) or not all(isinstance(d, dict) for d in diags):
        raise ArchitectureError("graph.diagnostics must be an array of objects")
    if snapshot.get("snapshot_id") != identity.snapshot_id(snapshot):
        raise ArchitectureError("snapshot_id does not match its key fields")
    if snapshot.get("graph_hash") != identity.graph_hash(graph):
        raise ArchitectureError("graph_hash does not match the graph")
    by_id: dict = {}
    for n in nodes:
        if not isinstance(n, dict) or any(f not in n for f in _NODE_FIELDS):
            raise ArchitectureError("invalid node: " + ", ".join(_NODE_FIELDS) + " are required")
        if n["kind"] not in NODE_KINDS or not isinstance(n["node_id"], str) or not n["node_id"]:
            raise ArchitectureError("invalid node")
        if not isinstance(n["qualified_name"], str) or not isinstance(n["metadata"], dict):
            raise ArchitectureError("invalid node")
        if not (n["parent_id"] is None or isinstance(n["parent_id"], str)):
            raise ArchitectureError("invalid node parent_id")
        if not (_opt(n["line_start"], int) and _opt(n["line_end"], int)):
            raise ArchitectureError("invalid node line range")
        if n["node_id"] in by_id:
            raise ArchitectureError("duplicate node_id")
        by_id[n["node_id"]] = n
        if not _rel_ok(n["relative_path"]):
            raise ArchitectureError("node path escapes the repository")
    children: dict = defaultdict(list)
    for n in nodes:
        if n["parent_id"] is not None and n["parent_id"] not in by_id:
            raise ArchitectureError("node parent_id references an unknown node")
        children[n["parent_id"]].append(n["node_id"])
    _check_structure(by_id, children)
    for e in edges:
        if not isinstance(e, dict) or any(f not in e for f in _EDGE_FIELDS):
            raise ArchitectureError("invalid edge: " + ", ".join(_EDGE_FIELDS) + " are required")
        if e["kind"] not in EDGE_KINDS or not isinstance(e["edge_id"], str) or not e["edge_id"]:
            raise ArchitectureError("invalid edge")
        if e["from_id"] not in by_id or e["to_id"] not in by_id:
            raise ArchitectureError("edge references an unknown node")
        if not isinstance(e["inferred"], bool):
            raise ArchitectureError("edge.inferred must be a boolean")
        span = e["source_span"]
        if not e["extractor"] or not isinstance(e["extractor"], str) or not isinstance(span, dict):
            raise ArchitectureError("edge needs an extractor and a source span")
        if "path" not in span or not _rel_ok(span["path"]) or not _opt(span.get("line_start"), int):
            raise ArchitectureError("invalid edge source span")
        if not (e["evidence_ref"] is None or isinstance(e["evidence_ref"], str)):
            raise ArchitectureError("invalid edge evidence_ref")


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
        # Bounded even though publish rejects cycles: a graph stored before that check must not hang a request.
        for _ in range(len(self.nodes) + 1):
            if nid is None:
                return
            yield nid
            nid = self.nodes[nid]["parent_id"]
        raise ArchitectureError("snapshot graph has a parent cycle", 500)


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
    def __init__(self, repo, enabled=False, cache_graphs=DEFAULT_CACHE_GRAPHS, cache_bytes=DEFAULT_CACHE_BYTES,
                 request_ttl=DEFAULT_REQUEST_TTL):
        self.repo = repo
        self.enabled = bool(enabled)
        # Loaded graphs are an LRU bounded by count and by stored graph bytes (artifact size; the parsed form is
        # larger by a roughly constant factor). An evicted graph is re-read from its artifact on demand.
        self.cache_graphs, self.cache_bytes = int(cache_graphs), int(cache_bytes)
        self.request_ttl = int(request_ttl)       # a `requested` job nobody claimed expires after this many seconds
        self.gate_store_fn = lambda: None          # set by the server when the review gate is running
        self.explain_config = None                 # set from control_plane.architecture.explain (off by default)
        self.explain_key = None
        self._meta: dict[str, dict] = {}
        self._graphs: OrderedDict[str, tuple[_Loaded, int]] = OrderedDict()
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
    @staticmethod
    def _check_host(host_id, allowed_hosts) -> None:
        """`allowed_hosts` is the set an agent key is bound to (control_plane.architecture.agent_hosts), or None
        for a key with no binding: such agent keys are trusted to name any host_id."""
        if allowed_hosts is not None and host_id not in allowed_hosts:
            raise ArchitectureError("this API key is not bound to that host_id", 403)

    def publish(self, body: dict, allowed_hosts=None) -> dict:
        snapshot, graph = (body or {}).get("snapshot"), (body or {}).get("graph")
        _validate(snapshot, graph)
        self._check_host(snapshot["host_id"], allowed_hosts)
        sid = snapshot["snapshot_id"]
        loaded = _Loaded(graph)                    # before any durable write: a graph that cannot load is never stored
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
            self._cache_put(sid, loaded, art["size_bytes"])
            return {"snapshot": meta, "duplicate": False}

    def _cache_put(self, sid, loaded: _Loaded, size: int) -> None:
        with self._lock:
            self._graphs[sid] = (loaded, int(size or 0))
            self._graphs.move_to_end(sid)
            total = sum(sz for _, sz in self._graphs.values())
            while len(self._graphs) > 1 and (len(self._graphs) > self.cache_graphs or total > self.cache_bytes):
                _, (_, sz) = self._graphs.popitem(last=False)     # least recently used; never the one just added
                total -= sz

    def _load(self, sid) -> _Loaded:
        meta = self._meta.get(sid)
        if meta is None:
            raise KeyError(sid)
        with self._lock:
            hit = self._graphs.get(sid)
            if hit is not None:
                self._graphs.move_to_end(sid)
                return hit[0]
        # Read and parse outside the lock: a large artifact must not stall every other architecture call.
        data = self.repo.get_artifact_data(meta["artifact_ref"]["artifact_id"])
        if data is None:
            raise ArchitectureError("snapshot graph artifact is unavailable", 503)
        if hashlib.sha256(data).hexdigest() != meta["artifact_ref"]["sha256"]:
            raise ArchitectureError("snapshot graph artifact failed its integrity check", 503)
        g = _Loaded(json.loads(data))
        try:
            _check_structure(g.nodes, g.children)  # snapshots stored before deep validation existed
        except ArchitectureError as exc:
            raise ArchitectureError(f"stored snapshot graph is malformed: {exc}", 503) from None
        self._cache_put(sid, g, len(data))
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

        stack, seen = list(visible), set(visible)
        while stack:
            cur = stack.pop()
            for kid in g.children.get(cur, []):
                if kid not in seen:              # a cyclic legacy graph must not loop forever
                    seen.add(kid)
                    stack.append(kid)
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

    def _expires_at(self, job) -> float:
        """Epoch when a `requested` job lapses. Old events carry no expires_at: derive it from created_at."""
        if isinstance(job.get("expires_at"), (int, float)):
            return float(job["expires_at"])
        try:
            created = datetime.fromisoformat(str(job.get("created_at")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
        return created + self.request_ttl

    def _expire_stale(self, now: float) -> None:
        """Requested jobs nobody claimed in time end as `expired` (caller holds the lock)."""
        for job in list(self._jobs.values()):
            if job["state"] == "requested" and self._expires_at(job) <= now:
                self._apply(self.repo.record_event(JOB_ENTITY, job["analysis_id"], "architecture.analysis.expired",
                                                   {"message": "no host claimed the request before it expired"}))

    def _open(self, job) -> bool:
        if job["state"] == "requested":
            return self._expires_at(job) > time.time()
        return job["state"] in ("started", "progress") and job.get("lease_expires_at", 0) > time.time()

    @staticmethod
    def _request_key(repository_id, host_id, rev, base_rev=None) -> str:
        parts = [repository_id, host_id, rev] if base_rev is None else [repository_id, host_id, rev, base_rev]
        return identity.hash_obj(parts)[:24]

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
        base_rev = body.get("base_rev")
        if base_rev is not None and not (isinstance(base_rev, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", base_rev)):
            raise ArchitectureError("base_rev must be a branch, tag or commit name")
        prio = body.get("priority", "interactive")
        if prio not in ("interactive", "background"):
            raise ArchitectureError("priority must be interactive or background")
        key = self._request_key(repo_id, host_id, rev, base_rev)
        with self._lock:
            self._expire_stale(time.time())
            if identity.is_full_sha(rev) and base_rev is None:      # a base request needs the host to resolve the merge-base
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
                                         "key": key, "expires_at": time.time() + self.request_ttl,
                                         **({"base_rev": base_rev} if base_rev else {})})
            self._apply(ev)
            return {"analysis_id": aid, "deduplicated": False, "state": "requested"}

    def claim_analysis(self, body: dict, allowed_hosts=None) -> dict:
        """A host takes the oldest requested job for itself (interactive first) under a lease."""
        host_id = (body or {}).get("host_id")
        if not (isinstance(host_id, str) and _HOST_ID.fullmatch(host_id)):
            raise ArchitectureError("host_id is required")
        self._check_host(host_id, allowed_hosts)
        try:
            lease = max(30, min(int((body or {}).get("lease_seconds", 600)), 3600))
        except (TypeError, ValueError):
            raise ArchitectureError("lease_seconds must be an integer") from None
        with self._lock:
            now = time.time()
            self._expire_stale(now)
            ready = [j for j in self._jobs.values() if j.get("host_id") == host_id and (
                j["state"] == "requested" or (j["state"] in ("started", "progress") and j.get("lease_expires_at", 0) <= now))]
            if not ready:
                return {"job": None}
            ready.sort(key=lambda j: (j.get("priority") != "interactive", j["created_at"], j["analysis_id"]))
            job = ready[0]
            ev = self.repo.record_event(JOB_ENTITY, job["analysis_id"], "architecture.analysis.started",
                                        {"host_id": host_id, "lease_expires_at": now + lease})
            self._apply(ev)
            return {"job": {k: job[k] for k in ("analysis_id", "repository_id", "host_id", "rev", "base_rev", "priority") if k in job},
                    "lease_expires_at": now + lease}

    def pr_base(self, sid: str) -> dict:
        """The base snapshot a host paired with this one: merge-base of the head and the requested base ref, resolved
        from the host's own git history. None when no completed request asked for one, or the host could not."""
        if sid not in self._meta:
            raise KeyError(sid)
        with self._lock:
            done = [j for j in self._jobs.values() if j.get("state") == "completed" and j.get("snapshot_id") == sid
                    and j.get("base_rev")]
        done.sort(key=lambda j: j.get("updated_at") or "")
        for job in reversed(done):
            bid = job.get("base_snapshot_id")
            if bid and bid in self._meta:
                return {"snapshot_id": sid, "base": {"snapshot_id": bid, "commit_sha": job.get("base_commit_sha"),
                                                     "base_rev": job["base_rev"], "analysis_id": job["analysis_id"],
                                                     "resolved": "git merge-base on the analysis host"}}
        last = done[-1] if done else None
        return {"snapshot_id": sid, "base": None,
                "reason": (last.get("base_message") if last else None) or "no analysis asked for a base ref"}

    def cancel_analysis(self, analysis_id: str) -> dict:
        """Operator cancel: any unfinished job ends as `cancelled`; a running host sees it on its next heartbeat."""
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None:
                raise KeyError(analysis_id)
            if job["state"] in FINISHED:
                raise ArchitectureError("analysis already finished", 409)
            self._apply(self.repo.record_event(JOB_ENTITY, analysis_id, "architecture.analysis.cancelled",
                                               {"message": "cancelled by an operator"}, require_durable=True))
            return {"analysis": dict(job)}

    def record_analysis(self, body: dict, allowed_hosts=None) -> dict:
        if not isinstance(body, dict):
            raise ArchitectureError("body must be an object")
        aid, event = body.get("analysis_id"), body.get("event")
        if not isinstance(aid, str) or not _JOB_ID.fullmatch(aid):
            raise ArchitectureError("analysis_id is required")
        if event not in EVENTS:
            raise ArchitectureError("event must be one of " + ", ".join(sorted(EVENTS)))
        payload = {k: body[k] for k in ("repository_id", "commit_sha", "host_id", "worktree_id", "snapshot_id",
                                       "progress", "message", "base_commit_sha", "base_snapshot_id", "base_message")
                   if k in body}
        for k in ("repository_id", "commit_sha", "host_id", "worktree_id", "snapshot_id", "message",
                  "base_commit_sha", "base_snapshot_id", "base_message"):
            if k in payload and not (isinstance(payload[k], str) and len(payload[k]) <= 500):
                raise ArchitectureError(f"invalid {k}")
        for k in ("commit_sha", "base_commit_sha"):
            if k in payload and not identity.is_full_sha(payload[k]):
                raise ArchitectureError(f"{k} must be a full hexadecimal commit")
        if "progress" in payload and not (isinstance(payload["progress"], (int, float)) and 0 <= payload["progress"] <= 1):
            raise ArchitectureError("progress must be between 0 and 1")
        if "host_id" in payload and not _HOST_ID.fullmatch(payload["host_id"]):
            raise ArchitectureError("invalid host_id")
        with self._lock:
            cur = self._jobs.get(aid)
            if allowed_hosts is not None:
                # a bound key reports only for its own hosts, and only on jobs of those hosts
                self._check_host(payload.get("host_id"), allowed_hosts)
                if cur and cur.get("host_id") is not None:
                    self._check_host(cur.get("host_id"), allowed_hosts)
            if cur and "host_id" in payload and cur.get("host_id") not in (None, payload["host_id"]):
                raise ArchitectureError("analysis belongs to another host", 409)
            if cur and cur["state"] in FINISHED:
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
    def room_repositories(self, mission_ids) -> frozenset:
        """The repositories a room may annotate: those of the sessions of the missions linked to the room."""
        mids = set(mission_ids or ())
        if not mids:
            return frozenset()
        return frozenset(r for sess in self.repo.list_sessions()
                         if (sess.get("mission_key") in mids or sess.get("mission_id") in mids)
                         and (r := ov.pl._identity(sess)))

    def prepare_room_link(self, body: dict, repositories) -> dict:
        """Validate a link and resolve its target; takes no collaboration lock (it may read a graph artifact).
        A snapshot outside the room's repositories is the same 404 as an unknown one: no cross-fleet oracle."""
        if not isinstance(body, dict):
            raise ArchitectureError("body must be an object")
        bad = [k for k in body if k not in ("snapshot_id", "node_id", "note", "idempotency_key")]
        if bad:
            raise ArchitectureError("unknown field")
        sid, nid, note, key = body.get("snapshot_id"), body.get("node_id"), body.get("note", ""), body.get("idempotency_key")
        if not (isinstance(sid, str) and isinstance(nid, str)):
            raise ArchitectureError("snapshot_id and node_id are required")
        if not isinstance(note, str) or len(note) > 500:
            raise ArchitectureError("note must be at most 500 characters")
        if key is not None and not (isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key)):
            raise ArchitectureError("invalid idempotency_key")
        meta = self._meta.get(sid)
        if meta is None or meta["repository_id"] not in (repositories or ()):
            raise KeyError(sid)
        if nid not in self._load(sid).nodes:     # KeyError -> 404
            raise KeyError(nid)
        return {"snapshot_id": sid, "node_id": nid, "note": _CONTROL.sub("", note).strip(), "idempotency_key": key}

    def record_room_link(self, room_id: str, prepared: dict, author: str) -> dict:
        """Called by CollabService AFTER it (re)authorised the caller for this room. Links, never copies, graph data."""
        sid, nid, note, key = (prepared[k] for k in ("snapshot_id", "node_id", "note", "idempotency_key"))
        with self._lock:
            rows = self._links[room_id]
            link_id = "lnk_" + (identity.hash_obj([room_id, author, key])[:24] if key else uuid.uuid4().hex[:24])
            prior = next((r for r in rows if r["link_id"] == link_id), None)
            if prior:
                if (prior["snapshot_id"], prior["node_id"], prior["note"]) != (sid, nid, note):
                    raise ArchitectureError("idempotency_key already used for a different link", 409)
                return {"link_id": link_id, "replayed": True}
            if len(rows) >= MAX_LINKS_PER_ROOM:
                raise ArchitectureError("this room has too many architecture links", 400)
            rec = {"link_id": link_id, "snapshot_id": sid, "node_id": nid, "note": note,
                   "author": author, "created_at": self.repo._stamp()}
            self.repo.record_event(LINK_ENTITY, room_id, "architecture.link.created", rec, require_durable=True)
            rows.append(rec)
            return {"link_id": link_id, "replayed": False}

    def add_room_link(self, room_id: str, body: dict, author: str, repositories) -> dict:
        return self.record_room_link(room_id, self.prepare_room_link(body, repositories), author)

    def room_links(self, room_id: str, redact: bool = False) -> dict:
        out = []
        with self._lock:
            rows = list(self._links.get(room_id, []))
        for r in rows:
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

    def node_rooms(self, sid: str, nid: str, reveal: bool = False) -> dict:
        """Which rooms discuss this entity. Room ids go to operators only (an API key is never a room member);
        everyone else learns how many rooms do. The notes stay behind each room's own permissions."""
        with self._lock:
            rooms = sorted({rid for rid, rows in self._links.items() for r in rows
                            if r["snapshot_id"] == sid and r["node_id"] == nid})
        return {"rooms": rooms if reveal else [], "count": len(rooms), "redacted": not reveal}

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
