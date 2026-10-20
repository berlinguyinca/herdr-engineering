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

from .project_links import canonical_repository


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

    def ingest(self, body: dict) -> dict:
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
        if entity_type in {"plannotator_review", "ci_pipeline"}:
            raise ValueError("provider-owned entity type is not accepted by generic ingestion")
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
        self.repo.record_event(entity_type, entity_id, event_type, payload)
        return {"entity_type": entity_type, "entity_id": entity_id,
                "event_type": event_type}

    def _ingest_host(self, entity_id, event_type, payload) -> dict:
        status = ("unavailable" if "unavailable" in event_type.lower()
                  else "healthy")
        return self.repo.upsert_host(
            entity_id,
            payload.get("host_name") or payload.get("name") or entity_id,
            payload.get("tailnet_ip") or payload.get("ip"),
            cpu=payload.get("cpu"), mem=payload.get("mem"), status=status)

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
                "revision", "interactive_ready", "launch_pending", "state_change_seq")})

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
