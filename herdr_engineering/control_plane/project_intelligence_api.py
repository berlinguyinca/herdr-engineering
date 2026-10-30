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

import json

from . import project_intelligence as pi

READ_ROLES = frozenset({"viewer", "operator"})


def _unwired():
    raise pi.IntelligenceError("no executor wired for request_ci_repair")


class IntelligenceRoutes:
    def __init__(self, store: pi.IntelligenceStore | None = None, section_fn=lambda: {}, store_fn=None):
        """``store_fn`` builds the store lazily (only once the feature is enabled); ``store`` is for tests/embedding."""
        self._store, self._store_fn = store, store_fn
        self._section = section_fn
        from .intel_assistant import ActionBoard
        self.board = ActionBoard()   # proposals live 30 minutes by design, so they are not persisted
        self.github_app_fn = None   # set by the API glue: () -> review-gate GitHub client or None
        self.metrics_fn = None      # set by the API glue: () -> dict of bridge counters and incident states
        self.ci_repair_fn = None    # set by the API glue: (repository, run, operation_id, actor) -> CI-fix request
        self.ci_rerun_fn = None     # set by the API glue: (repository, run) -> restarts that CI run
        self.poll_status_fn = None  # set by the API glue: () -> {client, last_error} for the PR poller
        self.single_operator_fn = lambda: False   # set by the API glue: the server runs in single-operator mode
        self.last_reconcile: dict | None = None   # counters of the latest pass; a reason when it could not run
        self._executor = None
        from .intel_webhook import ReplayGuard
        self._replay = ReplayGuard()

    @property
    def store(self) -> pi.IntelligenceStore:
        if self._store is None:
            self._store = self._store_fn() if self._store_fn else pi.IntelligenceStore()
        return self._store

    @property
    def executor(self):
        if self._executor is None:
            from .intel_executor import ProposalExecutor
            self._executor = ProposalExecutor(self.board, self.store, section_fn=self._section,
                                              ci_repair=lambda *a: self.ci_repair_fn(*a) if self.ci_repair_fn else _unwired(),
                                              ci_rerun=lambda *a: self.ci_rerun_fn(*a) if self.ci_rerun_fn else _unwired())
        return self._executor

    def reconcile_now(self) -> dict | None:
        """Record open PR heads no delivery has recorded (outbound polling; needs no inbound connection).
        ``None`` when it cannot run: ingest off, kill switch, or missing client/config."""
        from datetime import UTC, datetime

        from .intel_webhook import reconcile_open_prs
        cfg, raw = self._cfg()
        app = self.github_app_fn() if self.github_app_fn else None
        projects, org = raw.get("webhook_projects"), raw.get("default_org_id")
        if not (cfg["enabled"] and cfg["ingest"]) or cfg["kill_switch"] or app is None \
                or not isinstance(projects, dict) or not projects or not isinstance(org, str):
            self.last_reconcile = {"ran": False, "at": datetime.now(UTC).isoformat(timespec="seconds")}
            return None
        out = reconcile_open_prs(app, self.store, cfg, org_id=org, installation=str(raw.get("webhook_installation") or "github"),
                                 repo_projects=projects, received_at=datetime.now(UTC))
        self.last_reconcile = {"ran": True, "at": datetime.now(UTC).isoformat(timespec="seconds"), **out}
        return out

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
        repo = ((payload.get("repository") or {}).get("full_name") if isinstance(payload, dict) and isinstance(payload.get("repository"), dict) else None)
        repo = repo if isinstance(repo, str) else None
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

    @staticmethod
    def _proposal_view(p) -> dict:
        return {"id": p.id, "action": p.action, "payload": p.payload, "payload_sha256": p.payload_sha256,
                "resource_version": p.resource_version, "requester": p.requester, "state": p.state,
                "expires_at": p.expires_at.isoformat(), "approver": p.approver, "history": list(p.history),
                "rollback_available": p.rollback_available}

    def assistant_read(self, tenant_id: str, user_id: str, payload: dict) -> dict:
        """Assistant tool entry: scope comes only from YAML ``assistant_viewers: {"<tenant>/<user>": {...}}``."""
        from .intel_assistant import ReadTools, execute_read
        cfg, raw = self._cfg()
        if not cfg["enabled"]:
            raise pi.NotFound("project intelligence is not enabled")
        entry = (raw.get("assistant_viewers") or {}).get(f"{tenant_id}/{user_id}")
        viewer = self._viewer_from_entry(entry) if isinstance(entry, dict) else None
        return execute_read(ReadTools(self.store), viewer, payload)

    def assistant_ci_repair(self, tenant_id: str, user_id: str, payload: dict, idempotency_key: str) -> dict:
        """Assistant CI watch entry: queue a ``request_ci_repair`` PROPOSAL on the existing board. Scope comes only
        from YAML ``assistant_viewers`` (as for reads). The requester is the user, so the board's own refusal of
        self-approval applies. Executing an approved proposal stays with the existing executor and its policy."""
        cfg, raw = self._cfg()
        if not cfg["enabled"]:
            raise pi.NotFound("project intelligence is not enabled")
        entry = (raw.get("assistant_viewers") or {}).get(f"{tenant_id}/{user_id}")
        viewer = self._viewer_from_entry(entry) if isinstance(entry, dict) else None
        if viewer is None:
            raise pi.NotFound("no project-intelligence scope is configured for this user")
        repo = payload.get("repo")
        mapped = (raw.get("webhook_projects") or {}).get(repo) if isinstance(repo, str) else None
        if not (isinstance(mapped, list) and mapped) or (
                viewer.project_ids is not None and not set(mapped) <= viewer.project_ids):
            raise pi.NotFound("the repository is outside this user's project scope")
        p = self.board.request(org_id=viewer.org_id, requester=user_id, action="request_ci_repair",
                               payload=dict(payload), resource_version=1, idempotency_key=idempotency_key[:64])
        return {"id": p.id, "expires_at": p.expires_at.isoformat(), "state": p.state,
                "payload_sha256": p.payload_sha256}

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
                if role == "operator":
                    poller = dict(self.last_reconcile or {})
                    if self.poll_status_fn is not None:
                        poller.update(self.poll_status_fn())  # fixed reason strings (permission, not_configured ...)
                    out["poller"] = poller
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
            if parts == ["proposals"]:
                return 200, {"items": [self._proposal_view(p) for p in self.board.pending(viewer.org_id)
                                       if viewer.project_ids is None or p.payload.get("project") in viewer.project_ids],
                             "note": "An approved proposal is executed only when the execute flag is on; otherwise it records permission only."}
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
            if parts == ["proposals"]:
                if not cfg["suggest"] or cfg["kill_switch"]:
                    return 409, {"error": "proposals require the suggest flag and no kill switch"}
                payload, version = body.get("payload"), body.get("resource_version")
                if not isinstance(payload, dict) or not isinstance(version, int) or isinstance(version, bool):
                    return 400, {"error": "payload (object) and resource_version (int) are required"}
                project = payload.get("project")
                if not isinstance(project, str) or not project:
                    return 400, {"error": "payload.project (string) is required"}
                if len(json.dumps(payload, sort_keys=True)) > 4096:
                    return 400, {"error": "payload is too large to review in full (max 4096 characters)"}
                if viewer.project_ids is not None and project not in viewer.project_ids:
                    return 404, {"error": "not found"}
                p = self.board.request(org_id=viewer.org_id, requester=actor, action=str(body.get("action") or ""),
                                       payload=payload, resource_version=version, idempotency_key=body.get("idempotency_key"))
                self.store.record_audit({"action": "proposal_requested", "id": p.id, "actor": actor, "kind": p.action})
                return 201, self._proposal_view(p)
            if len(parts) == 3 and parts[0] == "proposals" and parts[2] in ("approve", "reject"):
                if not cfg["suggest"] or cfg["kill_switch"]:
                    return 409, {"error": "proposals require the suggest flag and no kill switch"}
                approvers = raw.get("approvers")
                single = self.single_operator_fn() and principal.get("authenticated_by") == "open_listener"
                if not single and (not isinstance(approvers, list) or actor not in [a for a in approvers if isinstance(a, str)]
                                   or principal.get("authenticated_by") == "open_listener"):
                    # Only named principals decide. An absent list means nobody can; an open listener has no identity.
                    return 403, {"error": "this principal is not in project_intelligence.approvers"}
                known = self.board.get(parts[1], viewer.org_id)
                if viewer.project_ids is not None and known.payload.get("project") not in viewer.project_ids:
                    return 404, {"error": "not found"}       # a project-scoped operator cannot act on another project's proposal
                if parts[2] == "reject":
                    p = self.board.reject(parts[1], approver=actor, org_id=viewer.org_id)
                else:
                    v = body.get("resource_version")
                    if not isinstance(body.get("payload_sha256"), str) or not isinstance(v, int) or isinstance(v, bool):
                        return 400, {"error": "payload_sha256 (string) and resource_version (int) are required"}
                    p = self.board.approve(parts[1], approver=actor, org_id=viewer.org_id,
                                           payload_sha256=body["payload_sha256"], resource_version=v, allow_self=single)
                self.store.record_audit({"action": f"proposal_{p.state}", "id": p.id, "actor": actor})
                if p.state == "approved":
                    p = self.executor.execute(p.id, viewer.org_id)     # no-op unless the execute flag is on
                return 200, self._proposal_view(p)
            if parts == ["webhooks", "reconcile"]:
                if viewer.project_ids is not None:
                    return 404, {"error": "not found"}
                out = self.reconcile_now()
                if out is None:
                    return 409, {"error": "reconcile needs ingest, no kill switch, the review-gate GitHub client, webhook_projects and default_org_id"}
                return 200, out
        except pi.NotFound:
            return 404, {"error": "not found"}
        except pi.StoreUnavailable:
            return 503, {"error": "store unavailable"}
        except pi.Conflict as exc:
            return 409, {"error": str(exc)}
        except pi.IntelligenceError as exc:
            return 400, {"error": str(exc)}
        return 404, {"error": "not found"}
