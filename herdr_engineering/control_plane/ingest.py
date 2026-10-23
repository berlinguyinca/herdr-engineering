"""Ingestion service (Phase 1b): map runtime lifecycle events to the repo.

The control-plane dashboard is populated by tapping the existing herdr system
(agents, workspaces/sessions, missions, machines) rather than custom per-host
reporters. This service defines the ingestion contract: a generic event
envelope that any emitter can POST, translated idempotently into stable,
structured entities (hosts / sessions / missions) plus their event streams —
so repeated heartbeats never duplicate a record, and the same external id maps
to one control-plane entity.
"""
from __future__ import annotations

import re

from .envelope import ENVELOPE_ONLY_TYPES, EnvelopeConflict, normalize_envelope
from .events import SequenceCollision
from .graph import validate_lineage
from .project_links import canonical_repository
from .sink import EventAlreadyStored


def _git_identity(payload):
    """Only explicit forge identity and a dedicated full commit SHA link CI."""
    fields = ("repository_id", "forge_url", "repository_url", "repo_url", "clone_url")
    advertised = [payload[k] for k in fields if payload.get(k) is not None]
    identities = {canonical_repository(value, forge_host=payload.get("forge_host"))
                  for value in advertised}
    if None in identities or len(identities) > 1:
        raise ValueError("repository identity must identify one explicit forge repository")
    commit_sha = payload.get("commit_sha")
    if commit_sha is not None:
        if not isinstance(commit_sha, str) or not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", commit_sha):
            raise ValueError("commit_sha must be a full 40 or 64 character hexadecimal commit")
        commit_sha = commit_sha.lower()
    return {"repository_id": next(iter(identities), None), "commit_sha": commit_sha}


class IngestService:
    """Translates lifecycle event envelopes into idempotent repo mutations."""

    def __init__(self, repo):
        self.repo = repo

    def ingest(self, body: dict, principal: dict | None = None) -> dict:
        """Apply one ingest body.

        ``principal`` is the authenticated caller (``{kind, id, authenticated_by}``)
        and becomes the ``actor`` of a schema_version 2 envelope. v1 bodies
        (no/1 ``schema_version``) take the legacy path and ignore it.
        Raises ``EnvelopeConflict`` (HTTP 409) when a v2 envelope contradicts
        stored state.
        """
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        entity_type = body.get("entity_type")
        entity_id = body.get("entity_id")
        event_type = body.get("event_type")
        if not entity_type or not entity_id or not event_type:
            raise ValueError(
                "entity_type, entity_id, and event_type are required")
        if not all(isinstance(v, str) for v in (entity_type, entity_id, event_type)):
            raise ValueError("entity_type, entity_id, and event_type must be strings")
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        source = body.get("source") or "herdr"
        if not isinstance(source, str):
            raise ValueError("invalid source")
        source = source.lower().strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", source):
            raise ValueError("invalid source")

        # Provider-owned observations and review outcomes have dedicated,
        # authenticated adapters; generic telemetry cannot forge those streams.
        if entity_type in {"plannotator_review", "ci_pipeline", "agent_identity", "agent_summary",
                           "custom_model", "llm_key", "token_usage"}:
            raise ValueError("provider-owned entity type is not accepted by generic ingestion")
        env = normalize_envelope(body, principal, self.repo._stamp())
        if env is None and entity_type == "lineage":
            raise ValueError("lineage events require schema_version 2")
        if env is None:
            return self._dispatch(entity_type, entity_id, event_type, payload, source)
        env["entity_type"] = entity_type
        self._prevalidate(entity_type, entity_id, payload, event_type)
        ledger = self.repo.ledger
        ack = {"event_id": env["event_id"], "source": env["source"],
               "source_sequence": env["source_sequence"]}
        head = {"entity_type": entity_type, "entity_id": entity_id, "event_type": event_type}
        # Inspector/mission_activity payloads are redacted per field by their
        # own stores, so the envelope row never carries them.
        row_payload = {} if entity_type in ("inspector", "mission_activity") else payload
        with ledger.lock:
            if ledger.check(env) == "duplicate":
                return {**head, "envelope": {**ack, "duplicate": True}}
            if entity_type == "lineage":
                # Ownership/capacity gate: runs under the ledger lock and BEFORE
                # the row is persisted, so a refused change is never stored and
                # its sequence is not consumed. Raises PermissionError (403).
                decision = self.repo.graph.admit(env["actor"], event_type, payload, env["source"])
                if decision["mode"] != "normal":
                    env["lineage_mode"] = decision["mode"]
                # Server-assigned monotonic admission number, persisted with the
                # row: ownership and cross-writer ordering never depend on the
                # wall clock, source names or client sequences.
                env["lineage_seq"] = self.repo.graph.next_seq()
            # The envelope row is persisted FIRST. If that fails nothing has
            # been applied and the ledger is untouched, so the emitter retries.
            try:
                self.repo.record_event(
                    entity_type, entity_id, event_type, row_payload,
                    envelope=env, feed=entity_type not in ENVELOPE_ONLY_TYPES,
                    publish=entity_type not in ENVELOPE_ONLY_TYPES)
            except SequenceCollision as exc:
                if exc.event_id_clash:
                    raise EnvelopeConflict(str(exc)) from None
                raise EnvelopeConflict("source_sequence is already used by another event") from None
            except EventAlreadyStored:
                # Durable insert landed earlier but the ack was lost (or the
                # in-memory index evicted it): same identity => already stored.
                if self._stored_identity(env["event_id"], (env.get("actor") or {}).get("id") or "") != (env["source"], env["source_sequence"]):
                    raise EnvelopeConflict("event_id already stored for a different source sequence") from None
                ledger.commit(env)
                return {**head, "envelope": {**ack, "duplicate": True}}
            try:
                if entity_type == "lineage":
                    result = self._apply_lineage(entity_id, event_type, payload, env)
                else:
                    result = self._dispatch(entity_type, entity_id, event_type, payload, source,
                                            record_generic=False)
            finally:
                # The row is durable, so its (source, sequence) is consumed even
                # if the handler then fails; the failure is reported once.
                ledger.commit(env)
        return {**result, "envelope": {**ack, "duplicate": False}}

    def _apply_lineage(self, entity_id, event_type, payload, env) -> dict:
        """Project a persisted lineage envelope into the graph (never infers edges)."""
        self.repo.graph.apply({
            "event_id": env["event_id"], "source": env["source"],
            "source_sequence": env["source_sequence"], "event_type": event_type,
            "payload": payload, "visibility_scope": env["visibility_scope"],
            # the room/mission/workspace context a `room`/`mission` scope is resolved against
            # (the same fields a rebuild reads back from the stored row)
            "room_id": env.get("room_id"), "mission_id": env.get("mission_id"),
            "workspace_id": env.get("workspace_id"),
            "actor": env.get("actor"), "ingest_timestamp": env["received_at"],
            "lineage_seq": env.get("lineage_seq"), "lineage_mode": env.get("lineage_mode")})
        return {"entity_type": "lineage", "entity_id": entity_id, "event_type": event_type}

    def _stored_identity(self, event_id, actor_id=""):
        lookup = getattr(getattr(self.repo, "_sink", None), "stored_identity", None)
        if not callable(lookup):
            raise RuntimeError("durable event identity is unavailable")
        found = lookup(event_id, actor_id)
        if found is None:
            raise RuntimeError("durable event identity is unavailable")
        return tuple(found)

    @staticmethod
    def _prevalidate(entity_type, entity_id, payload, event_type="") -> None:
        """Reject what a handler would reject BEFORE the envelope is persisted."""
        if entity_type == "mission" and ":" in entity_id:
            raise ValueError("invalid mission id")
        if entity_type == "lineage":
            validate_lineage(event_type, payload)
        if entity_type in ("session", "mission"):
            _git_identity(payload)
        if entity_type == "service" and any(
                k in payload for k in ("repository_id", "forge_url", "repository_url",
                                       "repo_url", "clone_url", "commit_sha")):
            _git_identity(payload)

    def _dispatch(self, entity_type, entity_id, event_type, payload, source,
                  record_generic=True) -> dict:
        if entity_type == "mission_activity":
            if event_type != "mission_activity.snapshot":
                raise ValueError("invalid mission activity event_type")
            result = self.repo.mission_activity.ingest(entity_id, payload, source=source)
            return {"entity_type": entity_type, "entity_id": entity_id, **result}
        if entity_type == "host":
            return self._ingest_host(entity_id, event_type, payload)
        if entity_type == "session":
            return self._ingest_session(entity_id, event_type, payload,
                                        source)
        if entity_type == "mission":
            if ":" in entity_id:
                raise ValueError("invalid mission id")
            return self._ingest_mission(entity_id, event_type, payload,
                                        source)
        if entity_type == "service":
            return self._ingest_service(entity_id, event_type, payload)
        if entity_type == "inspector":
            # Agent inspector detail (spec 0200). Sanitised/redacted per field
            # by the store; not recorded on the global event feed.
            if "kind" not in payload and "events" not in payload and event_type != "inspector.batch":
                payload = {**payload, "kind": event_type.removeprefix("inspector.")}
            res = self.repo.inspector.ingest(entity_id, payload)
            return {"entity_type": "inspector", "entity_id": entity_id,
                    "event_type": event_type, **res}
        # Unknown/other entity types: record the event generically so nothing is
        # silently dropped, and the UI still sees it on the activity stream.
        if record_generic:
            self.repo.record_event(entity_type, entity_id, event_type, payload)
        return {"entity_type": entity_type, "entity_id": entity_id,
                "event_type": event_type}

    def _ingest_host(self, entity_id, event_type, payload) -> dict:
        status = ("unavailable" if "unavailable" in event_type.lower()
                  else "healthy")
        host = self.repo.upsert_host(
            entity_id,
            payload.get("host_name") or payload.get("name") or entity_id,
            payload.get("tailnet_ip") or payload.get("ip"),
            cpu=payload.get("cpu"), mem=payload.get("mem"), status=status,
            inventory=payload.get("inventory"))
        sample = {k: payload[k] for k in ("cpu", "mem", "disk")
                  if isinstance(payload.get(k), (int, float))
                  and not isinstance(payload.get(k), bool)}
        if sample:
            self.repo.record_telemetry(entity_id, {
                "cpu_pct": sample.get("cpu"), "mem_pct": sample.get("mem"),
                "disk_pct": sample.get("disk")})
        return host

    def _ingest_session(self, entity_id, event_type, payload,
                        source="herdr") -> dict:
        if "stop" in event_type.lower():
            status = "stopped"
        elif "start" in event_type.lower():
            status = "active"
        else:
            status = payload.get("status", "active")
        return self.repo.upsert_session(
            entity_id, host_id=payload.get("host_id"),
            mission_id=payload.get("mission_id"),
            agent_role=payload.get("agent_role") or payload.get("role") or "",
            model=payload.get("model") or "", status=status,
            cwd=payload.get("cwd"), branch=payload.get("branch"),
            pid=payload.get("pid"), cmdline=payload.get("cmdline"),
            summary=payload.get("summary"), attach=payload.get("attach"),
            source=source, **_git_identity(payload),
            **{k: payload.get(k) for k in (
                "pane_id", "terminal_id", "herdr_session", "machine", "agent_session",
                "revision", "interactive_ready", "launch_pending", "state_change_seq",
                "transcript", "parent_session_id", "name")})

    def _ingest_mission(self, entity_id, event_type, payload,
                        source="herdr") -> dict:
        if "stop" in event_type.lower():
            stage = "complete"
        elif "start" in event_type.lower():
            stage = "active"
        else:
            stage = payload.get("stage", "active")
        return self.repo.upsert_mission(
            entity_id, title=payload.get("title") or "",
            purpose=payload.get("purpose") or "", stage=stage,
            cwd=payload.get("cwd"), source=source, **_git_identity(payload))

    def _ingest_service(self, entity_id, event_type, payload) -> dict:
        """A ``service`` envelope maps idempotently to a (host, name) service.

        ``payload['service_name']`` (or ``entity_id`` as a fallback) is the
        display name; ``host_id``/``port``/``route``/``status`` come from the
        payload. Repeated heartbeats for the same (host, name) update in place
        rather than duplicating. A human ``description`` and ``owner`` (the
        process) ride along when present; the repo always ensures a
        description so every service is self-explanatory.
        """
        metadata = {key: payload[key] for key in ("ownership", "session_id") if key in payload}
        identity_fields = ("repository_id", "forge_url", "repository_url", "repo_url", "clone_url")
        if any(key in payload for key in (*identity_fields, "commit_sha")):
            git = _git_identity(payload)
            if any(key in payload for key in identity_fields):
                metadata["repository_id"] = git["repository_id"]
            if "commit_sha" in payload:
                metadata["commit_sha"] = git["commit_sha"]
        if event_type in {"service.metadata", "service.listener_observed"}:
            observer = self.repo.observe_service_metadata if event_type == "service.metadata" else self.repo.observe_service_listener
            row = observer(entity_id, {**payload, **metadata})
            return {"entity_type": "service", "entity_id": entity_id, "event_type": event_type,
                    "metadata_observation": row.get("metadata_observation")}
        status = ("unavailable" if "unavailable" in event_type.lower()
                  else payload.get("status", "healthy"))
        svcid = self.repo.upsert_service(
            payload.get("host_id") or "",
            payload.get("service_name") or entity_id,
            int(payload["port"]) if payload.get("port") is not None else 0,
            status=status,
            route=payload.get("route"),
            description=payload.get("description"),
            owner=payload.get("owner"),
            project=payload.get("project"),
            source_port=payload.get("source_port"),
            **metadata,
        )
        return {"entity_type": "service", "entity_id": svcid,
                "event_type": event_type}
