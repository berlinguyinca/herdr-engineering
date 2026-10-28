"""HTTP surface for project intelligence (``/api/v1/intelligence/...``), kept out of api.py.

Fail-closed rules (DECISIONS D3/D9):

* Off unless ``control_plane.project_intelligence.enabled``; a disabled feature answers 404.
* The viewer comes from the authenticated API-key principal mapped in YAML
  (``control_plane.project_intelligence.viewers: {"key:<digest>": {org_id, projects, clearance}}``).
  The assistant's trusted ``X-Herdr-User`` header is never consulted. An unmapped principal sees
  nothing, except the open loopback listener's operator, which sees ``default_org_id``.
* Only ``viewer`` and ``operator`` keys read. Ingest/agent/reviewer keys get 404.
* Unknown and not-visible resources are indistinguishable (404).
"""
from __future__ import annotations

from . import project_intelligence as pi

READ_ROLES = frozenset({"viewer", "operator"})


class IntelligenceRoutes:
    def __init__(self, store: pi.IntelligenceStore | None = None, section_fn=lambda: {}, store_fn=None):
        """``store_fn`` builds the store lazily (only once the feature is enabled); ``store`` is for tests/embedding."""
        self._store, self._store_fn = store, store_fn
        self._section = section_fn

    @property
    def store(self) -> pi.IntelligenceStore:
        if self._store is None:
            self._store = self._store_fn() if self._store_fn else pi.IntelligenceStore()
        return self._store

    def _cfg(self) -> tuple[dict, dict]:
        raw = self._section() or {}
        return pi.config(raw), raw

    def viewer_for(self, principal: dict, role: str | None, raw: dict) -> pi.Viewer | None:
        if role not in READ_ROLES:
            return None
        entry = (raw.get("viewers") or {}).get(principal.get("id"))
        if isinstance(entry, dict) and isinstance(entry.get("org_id"), str):
            projects = entry.get("projects")
            clearance = entry.get("clearance", "internal")
            if clearance not in pi.LABELS or not (projects == "*" or (
                    isinstance(projects, list) and all(isinstance(x, str) for x in projects))):
                return None     # a malformed scope sees nothing (a bare string would otherwise become a set of letters)
            return pi.Viewer(entry["org_id"], None if projects == "*" else frozenset(projects or ()), clearance)
        org = raw.get("default_org_id")
        if principal.get("authenticated_by") == "open_listener" and isinstance(org, str):
            return pi.Viewer(org, None, "internal")
        return None

    def get(self, parts: list[str], query: dict[str, str], principal: dict, role: str | None):
        """``parts`` are the path segments after ``/api/v1/intelligence``. Returns ``(status, body)``."""
        cfg, raw = self._cfg()
        if not cfg["enabled"]:
            return 404, {"error": "not found"}
        viewer = self.viewer_for(principal, role, raw)
        if viewer is None:
            return 404, {"error": "not found"}
        try:
            if parts == ["status"]:
                return 200, {"enabled": True, "flags": {f: cfg[f] for f in pi.FLAGS}, "kill_switch": cfg["kill_switch"],
                             "schema_version": pi.SCHEMA_VERSION}
            if parts in (["projects"], ["investigations"]):
                kind = parts[0][:-1] if parts[0] == "projects" else "investigation"
                return 200, {"items": [{"id": r.id, "name": r.name, "status": r.status, "owner": r.owner,
                                        "version": r.version} for r in self.store.records(kind, viewer)]}
            if len(parts) == 3 and parts[0] == "projects" and parts[2] == "timeline":
                as_of = query.get("as_of")
                return 200, self.store.timeline(
                    viewer, project_id=parts[1], investigation_id=query.get("investigation"),
                    source_system=query.get("source"), actor_ref=query.get("actor"), category=query.get("category"),
                    as_of=pi._utc(as_of, "as_of") if as_of else None, limit=int(query.get("limit", 100)))
            if parts == ["relationships"]:
                ref = query.get("ref")
                if not ref:
                    return 400, {"error": "ref is required"}
                return 200, {"items": [{
                    "id": r.id, "from": r.from_ref, "to": r.to_ref, "predicate": r.predicate,
                    "derivation": r.derivation, "review_state": r.review_state, "confidence": r.confidence,
                    "version": r.version, "provisional": r.review_state == "provisional",
                    "evidence_refs": [e.as_dict() for e in r.evidence]} for r in self.store.neighbours(ref, viewer)]}
            if parts == ["findings"]:
                return 200, {"items": self.store.findings(viewer)}
        except (pi.IntelligenceError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except pi.StoreUnavailable:
            return 503, {"error": "store unavailable"}
        return 404, {"error": "not found"}

    def post(self, parts: list[str], body: dict, principal: dict, role: str | None):
        """Operator-only controls: confirm/reject an inference, pause or resume a source."""
        cfg, raw = self._cfg()
        if not cfg["enabled"] or role != "operator":
            return 404, {"error": "not found"}
        viewer = self.viewer_for(principal, role, raw)
        if viewer is None:
            return 404, {"error": "not found"}
        actor = str(principal.get("id") or "operator")
        try:
            if len(parts) == 3 and parts[0] == "relationships" and parts[2] == "review":
                if not cfg["suggest"]:
                    return 409, {"error": "review requires the suggest flag"}
                version = body.get("expected_version")
                if not isinstance(version, int) or not isinstance(body.get("accept"), bool):
                    return 400, {"error": "accept (bool) and expected_version (int) are required"}
                rel = self.store.review_relationship(parts[1], accept=body["accept"], actor=actor,
                                                     expected_version=version, viewer=viewer)
                return 200, {"id": rel.id, "review_state": rel.review_state, "version": rel.version}
            if len(parts) == 3 and parts[0] == "sources" and parts[2] in ("pause", "resume"):
                if viewer.project_ids is not None:     # pausing a source is org-wide: project-scoped viewers may not
                    return 404, {"error": "not found"}
                self.store.pause_source(viewer.org_id, parts[1], parts[2] == "pause")
                return 200, {"source": parts[1], "paused": parts[2] == "pause"}
        except pi.NotFound:
            return 404, {"error": "not found"}
        except pi.StoreUnavailable:
            return 503, {"error": "store unavailable"}
        except pi.Conflict as exc:
            return 409, {"error": str(exc)}
        except pi.IntelligenceError as exc:
            return 400, {"error": str(exc)}
        return 404, {"error": "not found"}
