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

        if entity_type == "host":
            return self._ingest_host(entity_id, event_type, payload)
        if entity_type == "session":
            return self._ingest_session(entity_id, event_type, payload)
        if entity_type == "mission":
            return self._ingest_mission(entity_id, event_type, payload)
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

    def _ingest_session(self, entity_id, event_type, payload) -> dict:
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
            summary=payload.get("summary"), attach=payload.get("attach"))

    def _ingest_mission(self, entity_id, event_type, payload) -> dict:
        if "stop" in event_type.lower():
            stage = "complete"
        elif "start" in event_type.lower():
            stage = "active"
        else:
            stage = payload.get("stage", "active")
        return self.repo.upsert_mission(
            entity_id, title=payload.get("title") or "",
            purpose=payload.get("purpose") or "", stage=stage,
            cwd=payload.get("cwd"))

    def _ingest_service(self, entity_id, event_type, payload) -> dict:
        """A ``service`` envelope maps idempotently to a (host, name) service.

        ``payload['service_name']`` (or ``entity_id`` as a fallback) is the
        display name; ``host_id``/``port``/``route``/``status`` come from the
        payload. Repeated heartbeats for the same (host, name) update in place
        rather than duplicating. A human ``description`` and ``owner`` (the
        process) ride along when present; the repo always ensures a
        description so every service is self-explanatory.
        """
        status = ("unavailable" if "unavailable" in event_type.lower()
                  else payload.get("status", "healthy"))
        svcid = self.repo.upsert_service(
            payload.get("host_id") or "",
            payload.get("service_name") or entity_id,
            int(payload["port"]) if payload.get("port") is not None else 0,
            status=status,
            session_id=payload.get("session_id"),
            route=payload.get("route"),
            description=payload.get("description"),
            owner=payload.get("owner"),
        )
        return {"entity_type": "service", "entity_id": svcid,
                "event_type": event_type}
