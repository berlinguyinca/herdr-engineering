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
        self.metrics_fn = None      # set by the API glue: () -> dict of bridge counters and incident states
        from .intel_webhook import ReplayGuard
        self._replay = ReplayGuard()

    @property
    def store(self) -> pi.IntelligenceStore:
        if self._store is None:
            self._store = self._store_fn() if self._store_fn else pi.IntelligenceStore()
        return self._store

    def _cfg(self) -> tuple[dict, dict]:
        raw = self._section() or {}
        return pi.config(raw), raw

    def post_webhook(self, headers, body: bytes) -> tuple[int, dict]:
        """``POST /api/v1/intelligence/webhooks/github``: authenticated by HMAC, not an API key.

        404 unless enabled + ``ingest`` + ``webhook_secret_file`` are set. The secret is read from that file on each
        delivery (rotation needs no restart) and is never logged. Repositories not in ``webhook_projects`` are
        acknowledged but not stored (never guessed into a project). A delivery is marked seen only after it is stored.
        """
        from datetime import UTC, datetime

        from . import intel_webhook as wh
        cfg, raw = self._cfg()
        path = raw.get("webhook_secret_file")
        if not (cfg["enabled"] and cfg["ingest"] and isinstance(path, str) and path):
            return 404, {"error": "not found"}
        if cfg["kill_switch"]:
            return 503, {"error": "ingestion is paused"}
        try:
            with open(path, "rb") as f:
                secret = f.read(4096).strip()
        except OSError:
            return 503, {"error": "webhook secret unavailable"}
        try:
            wh.verify_signature(secret, body, headers.get("X-Hub-Signature-256"))
            delivery = headers.get("X-GitHub-Delivery")
            if self._replay.seen(delivery):
                return 202, {"status": "duplicate"}
            import json as _json
            payload = _json.loads(body or b"{}")
        except wh.WebhookRejected as exc:
            return (413 if exc.reason == "body too large" else 401), {"error": exc.reason}
        except ValueError:
            return 400, {"error": "invalid json"}
        repo = ((payload.get("repository") or {}).get("full_name") if isinstance(payload, dict) else None)
        projects = (raw.get("webhook_projects") or {}).get(repo)
        org = raw.get("default_org_id")
        if not (isinstance(projects, list) and projects and all(isinstance(x, str) for x in projects) and isinstance(org, str)):
            self._replay.mark(delivery)
            return 202, {"status": "unmapped"}
        event = wh.normalise(headers.get("X-GitHub-Event") or "", payload, org_id=org,
                             installation=str(raw.get("webhook_installation") or "github"), project_ids=tuple(projects),
                             received_at=datetime.now(UTC))
        if event is None:
            self._replay.mark(delivery)
            return 202, {"status": "ignored"}
        try:
            _, created = self.store.append_event(event, cfg=cfg)
        except (pi.IngestionPaused, pi.Conflict):
            return 503, {"error": "ingestion is paused"}
        except pi.IntelligenceError:
            return 422, {"error": "event rejected"}
        except pi.StoreUnavailable:
            return 503, {"error": "store unavailable"}     # not marked: GitHub's retry must be processed
        self._replay.mark(delivery)
        return 202, {"status": "accepted" if created else "duplicate"}

    def assistant_read(self, tenant_id: str, user_id: str, payload: dict) -> dict:
        """Assistant tool entry: scope comes only from YAML ``assistant_viewers: {"<tenant>/<user>": {...}}``."""
        from .intel_assistant import ReadTools, execute_read
        cfg, raw = self._cfg()
        if not cfg["enabled"]:
            raise pi.NotFound("project intelligence is not enabled")
        entry = (raw.get("assistant_viewers") or {}).get(f"{tenant_id}/{user_id}")
        viewer = self._viewer_from_entry(entry) if isinstance(entry, dict) else None
        return execute_read(ReadTools(self.store), viewer, payload)

    @staticmethod
    def _viewer_from_entry(entry: dict) -> pi.Viewer | None:
        projects, clearance = entry.get("projects"), entry.get("clearance", "internal")
        if not isinstance(entry.get("org_id"), str) or clearance not in pi.LABELS or not (
                projects == "*" or (isinstance(projects, list) and all(isinstance(x, str) for x in projects))):
            return None
        return pi.Viewer(entry["org_id"], None if projects == "*" else frozenset(projects or ()), clearance)

    def viewer_for(self, principal: dict, role: str | None, raw: dict) -> pi.Viewer | None:
        if role not in READ_ROLES:
            return None
        entry = (raw.get("viewers") or {}).get(principal.get("id"))
        if isinstance(entry, dict) and isinstance(entry.get("org_id"), str):
            return self._viewer_from_entry(entry)   # a malformed scope sees nothing
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
                out = {"enabled": True, "flags": {f: cfg[f] for f in pi.FLAGS}, "kill_switch": cfg["kill_switch"],
                       "schema_version": pi.SCHEMA_VERSION}
                if role == "operator" and self.metrics_fn is not None:
                    out["metrics"] = self.metrics_fn()       # counters only: no content, ids or titles
                return 200, out
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
