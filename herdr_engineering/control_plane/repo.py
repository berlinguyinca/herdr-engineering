"""Control-plane structured repository (Spec §17-19, §48-57).

The canonical in-memory domain model the control-plane API and web UI consume.
Structured, event-sourced state: every mutation records an ordered, replayable
event on the entity's logical stream (0-indexed sequence, like Phase 1's
Postgres EventStore). Identity is stable; location is dynamic. The API/UI is a
client of this repository and of the durable stores (Phase 1).

This repository is fully unit-testable without a live Postgres/RustFS — it is
the honest, structured core the UI reads and the SSE stream feeds from. The
durable Postgres/RustFS path (Phase 1 async stores) is bridged in deployment.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from uuid import uuid4

from .agent_identity import AgentIdentityRegistry
from .ids import new_id
from .inspector import REQUIRED_DURABLE_KINDS, InspectorStore
from .mission_activity import MissionActivityStore
from .redact import redact_payload
from .service_catalog import (
    INACTIVE,
    capture_service_origin,
    normalize_project,
    normalize_service_git,
    normalize_service_observation,
    normalize_service_ownership,
    normalize_source_port,
    service_catalog,
    service_lifecycle,
    service_listener_id,
    track_availability,
    validate_archive_days,
)

_STAGES = {
    "planning", "specification", "ready", "implementation", "testing",
    "review", "repair", "validation", "pr", "complete", "failed", "cancelled",
}
_SERVICE_UNSET = object()
_SESSION_ACTIONS = {"steer", "approve", "interrupt", "resume", "stop"}



def _session_mission_identity(mission_id, source):
    if mission_id is None:
        return {"mission_key": None, "mission_source": None}
    if not isinstance(mission_id, str) or not mission_id:
        raise ValueError("mission_id must be a non-empty string")
    if ":" in mission_id:
        namespace, raw_id = mission_id.split(":", 1)
        if not raw_id or ":" in raw_id or (source != "herdr" and namespace != source):
            raise ValueError("mission identity conflicts with its source")
        return {"mission_key": mission_id, "mission_source": namespace}
    return {"mission_key": mission_id if source == "herdr" else f"{source}:{mission_id}",
            "mission_source": source}


def _name_prefix(service_name):
    """First token of a ``<kind> on <host> (port <n>)``-style service label.

    Used to derive a human "what is this" for a service when no explicit
    description was supplied.
    """
    if not service_name:
        return None
    tokens = str(service_name).split()
    return tokens[0] if tokens else None


def describe_service(*, service_name=None, host_id=None, port=None,
                     owner=None, route=None) -> str:
    """Build a human one-liner describing a service: what it is and where it
    runs.

    Always returns a non-empty string so every service in the fleet carries a
    description, even when no explicit one was supplied. ``owner`` (the
    process name) is the best "what is this" signal; the service-name prefix
    and a generic fallback follow it.
    """
    kind = (owner or "").strip() or _name_prefix(service_name) or "web"
    host = (host_id or "").strip() or "an unknown host"
    desc = f"{kind} service on {host}"
    if port:
        desc += f" (port {port})"
    if route:
        desc += f", reachable at {route}"
    return desc


def _clean_inventory(raw):
    """Keep only ``{tools: {name: str}, plugins: {name: str}}`` with bounded sizes."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for group in ("tools", "plugins"):
        items = raw.get(group)
        if isinstance(items, dict):
            out[group] = {str(k)[:80]: str(v)[:80] for k, v in list(items.items())[:200]
                          if v not in (None, "")}
    return out if any(out.values()) else None


def _label(value, forbidden=None):
    """A short opaque label (session id or agent name) from an untrusted emitter, or None when it is not one."""
    if not isinstance(value, str) or not value or len(value) > 128 or value == forbidden:
        return None
    return value if all(c.isprintable() for c in value) else None


class ControlPlaneRepo:
    """In-memory structured control-plane state with event sequencing."""

    def __init__(self, *, now=None, sink=None, service_archive_days=7, host_aliases=None,
                 inspector_limits=None):
        """``sink`` is an optional durable writer (e.g. DurableEventSink) called
        with each recorded event so ingestion survives a restart.

        ``host_aliases`` maps an alternate host name to the canonical one (one
        machine that reports under two names). Stored records keep what was
        reported; every read presents the canonical name."""
        self._host_aliases = {str(k): str(v) for k, v in (host_aliases or {}).items() if k and v and k != v}
        self._command_lock = threading.RLock()
        self._event_lock = threading.RLock()
        self._agent_projection_lock = threading.RLock()
        self._agent_records = {}
        self._agent_sequence = 0
        self._sink = sink
        self._now = now or (lambda: time.time())
        self._missions = {}
        self._sessions = {}
        self._services = {}
        self._service_archive_days = validate_archive_days(service_archive_days)
        self._hosts = {}
        self._artifacts = {}
        self._blobs = {}  # artifact_id -> bytes (in-memory RustFS stand-in)
        self._leases = {}
        self._test_runs = {}
        self._reviews = {}
        self._pull_requests = {}
        self._telemetry = {}  # host_id -> [sample, ...]
        self._events = {}  # (entity_type, entity_id) -> [event, ...]
        self._event_order = []  # (entity_type, entity_id, seq) chronological
        self._ci_sequence = 0  # one bounded counter survives stream eviction
        self._listeners = set()  # pub/sub for the SSE live stream (Spec §52)
        self._collab_replay = []  # durable HCN events diverted out of the shared streams
        self.agent_identities = AgentIdentityRegistry(persist=self._persist_agent_projection)
        # Agent inspector (spec 0200): bounded per-session detail projection.
        self.inspector = InspectorStore.from_config(inspector_limits, persist=self._persist_inspector)
        self.mission_activity = MissionActivityStore(now=self._now)

    def _persist_inspector(self, ev):
        """Persist inspector detail outside global streams; require capture ACKs."""
        if self._sink is None:
            return
        ev = dict(ev)
        ev.setdefault("event_id", new_id("event"))
        ev.setdefault("schema_version", 1)
        ev["ingest_timestamp"] = self._stamp()
        ev["sequence"] = int(time.time() * 1000)
        if ev.get("event_type") in {f"inspector.{kind}" for kind in REQUIRED_DURABLE_KINDS}:
            required = getattr(self._sink, "record_required", None)
            if not callable(required):
                raise RuntimeError("inspector persistence requires an acknowledged sink")
            required(ev)
        else:
            self._sink.record(ev)

    # ------------------------------------------------------------------ ids
    def subscribe(self, listener):
        """Register a callable invoked with each recorded event (for SSE)."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)
    def _stamp(self):
        return _iso(self._now())

    # -------------------------------------------------------------- missions
    def create_mission(self, title, purpose="", stage="ready"):
        mid = new_id("mission")
        rec = {
            "mission_id": mid, "title": title, "purpose": purpose,
            "stage": stage, "status": stage, "created_at": self._stamp(),
            "updated_at": self._stamp(), "sessions": [],
        }
        self._missions[mid] = rec
        self.record_event("mission", mid, "MissionCreated",
                          {"title": title, "purpose": purpose}, mission_id=mid)
        return mid

    def get_mission(self, mission_id):
        rec = self._missions.get(mission_id)
        if rec is None:
            for m in self._missions.values():
                if (m.get("mission_id") == mission_id
                        and m.get("source", "herdr") == "herdr"):
                    rec = m
                    break
        return _copy(rec)

    def _resolve_mission_key(self, mission_id, source="herdr"):
        """Return the stored key for ``mission_id`` under ``source``.

        For a herdr source the raw ``mission_id`` is the stored key (an exact
        stored key if present, otherwise the raw id as the legacy fallback).
        For a non-herdr source the namespaced key (e.g. ``pi:MSN-1``) wins so
        a session for a pi-sourced mission never attaches to a herdr mission
        that happens to share the same raw id. An exact stored key that already
        carries the same source prefix is honoured; otherwise ``None`` is
        returned (fail closed) so callers never attach a session/stage to an
        unrelated mission.
        """
        if source == "herdr":
            return mission_id
        namespaced = f"{source}:{mission_id}"
        if namespaced in self._missions:
            return namespaced
        if mission_id in self._missions and mission_id.startswith(source + ":"):
            return mission_id
        return None

    def list_missions(self, status=None):
        out = [_copy(m) for m in self._missions.values()]
        if status:
            out = [m for m in out if m["status"] == status]
        return _sort_by(out, "created_at", reverse=True)

    def set_mission_stage(self, mission_id, stage, source="herdr"):
        if stage not in _STAGES:
            raise ValueError(f"unknown stage: {stage}")
        key = self._resolve_mission_key(mission_id, source=source)
        m = self._missions.get(key)
        if m is None:
            raise KeyError(mission_id)
        old = m["stage"]
        m["stage"] = stage
        m["status"] = stage
        m["updated_at"] = self._stamp()
        self.record_event("mission", key, "MissionStageChanged",
                          {"from": old, "to": stage}, mission_id=mission_id)

    # -------------------------------------------------------------- sessions
    def create_session(self, mission_id=None, agent_role="", model="",
                       source="herdr"):
        sid = new_id("session")
        binding = _session_mission_identity(mission_id, source)
        rec = {
            "session_id": sid, "mission_id": mission_id, "agent_role": agent_role, **binding,
            "model": model, "status": "active", "created_at": self._stamp(),
            "updated_at": self._stamp(), "messages": 0, "tool_calls": 0,
        }
        self._sessions[sid] = rec
        if mission_id:
            key = binding["mission_key"]
            if key in self._missions:
                self._missions[key]["sessions"].append(sid)
        self.record_event("session", sid, "SessionCreated",
                          {"mission_id": mission_id, "agent_role": agent_role, **binding},
                          mission_id=mission_id, session_id=sid)
        return sid

    def _canonical_host(self, host_id):
        return self._host_aliases.get(host_id, host_id)

    def _present_host_id(self, record):
        if record and record.get("host_id") in self._host_aliases:
            record["host_id"] = self._host_aliases[record["host_id"]]
        return record

    def get_session(self, session_id):
        return self._named_session(self._present_host_id(_copy(self._sessions.get(session_id))))

    def list_sessions(self, status=None):
        records = list(self._sessions.values())
        out = [self._named_session(self._present_host_id(_copy(s))) for s in records]
        if status:
            out = [s for s in out if s["status"] == status]
        return _sort_by(out, "created_at", reverse=True)

    def _named_session(self, record):
        if record is None:
            return None
        profile = self.agent_identities.allocate(record["session_id"], record.get("name"))
        return {**record, "display_name": profile["display_name"],
                "agent_handle": profile["handle"], "name_version": profile["version"]}

    def rename_agent(self, session_id, name, expected_version):
        if session_id not in self._sessions:
            raise KeyError(session_id)
        self.list_sessions()  # Reserve existing agents' preferred names before renaming.
        return self.agent_identities.rename(session_id, name, expected_version)

    def _persist_agent_projection(self, event):
        """Acknowledged private projections never enter activity or global SSE."""
        event = copy.deepcopy(event)
        event.setdefault("event_id", new_id("event"))
        event.setdefault("schema_version", 1)
        event.setdefault("source_timestamp", self._stamp())
        event["ingest_timestamp"] = self._stamp()
        with self._agent_projection_lock:
            sequence = max(self._agent_sequence + 1, int(self._now() * 1_000_000))
            event["sequence"] = sequence
            if self._sink is not None:
                required = getattr(self._sink, "record_required", None)
                if not callable(required):
                    raise RuntimeError("agent projections require acknowledged persistence")
                required(event)
            self._agent_sequence = sequence
            self._agent_records[(event["entity_type"], event["entity_id"])] = event

    def persist_agent_summary(self, event):
        sid = event["session_id"]
        if sid not in self._sessions or not isinstance(event.get("summary"), dict):
            raise ValueError("summary requires an existing agent and structured content")
        self._persist_agent_projection({"entity_type": "agent_summary", "entity_id": sid,
                                        "event_type": "agent_summary.updated",
                                        "payload": {"summary": event["summary"]}})

    def agent_summary_events(self):
        with self._agent_projection_lock:
            return [{"kind": "agent_summary", "session_id": sid,
                     "summary": copy.deepcopy(_payload(event)["summary"])}
                    for (kind, sid), event in self._agent_records.items()
                    if kind == "agent_summary" and isinstance(_payload(event).get("summary"), dict)]

    def session_action(self, session_id, action, *, operation_id=None):
        if action not in _SESSION_ACTIONS:
            raise ValueError(f"unknown session action: {action}")
        if session_id not in self._sessions:
            raise KeyError(session_id)
        # Native prompt delivery cannot implement lifecycle transitions or
        # approve mission decisions. Preserve observed state until telemetry
        # confirms a supported operation, instead of pretending to resume.
        result = {"operation_id": operation_id or f"command_{uuid4().hex}",
                  "action": action, "status": "failed",
                  "error": "unsupported_action"}
        self.record_event("session", session_id, "SessionActionRejected",
                          result, session_id=session_id)
        return result

    def send_message(self, session_id, message, artifact_ids=None, *,
                     operation_id=None, dispatcher=None):
        """Record submission before transport, and only count acknowledged delivery.

        Replayed operation IDs return their last receipt even if it is still
        queued: a crash during a non-idempotent native prompt has an unknown
        outcome and must never trigger automatic duplicate submission.
        """
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a nonempty string")
        artifact_ids = [] if artifact_ids is None else artifact_ids
        if not isinstance(artifact_ids, list) or not all(isinstance(a, str) for a in artifact_ids):
            raise ValueError("artifact_ids must be a list of strings")
        operation_id = f"command_{uuid4().hex}" if operation_id is None else operation_id
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
            raise ValueError("invalid operation_id")
        fingerprint = hashlib.sha256(json.dumps(
            [session_id, message, artifact_ids], ensure_ascii=False).encode()).hexdigest()
        with self._command_lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            for event in reversed(self.stream("session", session_id)):
                if event["event_type"] not in {"SessionCommandQueued", "SessionCommandSettled"}:
                    continue
                receipt = _payload(event)
                if receipt.get("operation_id") == operation_id:
                    if receipt.get("fingerprint") != fingerprint:
                        raise ValueError("operation_id already used for a different message")
                    return _deep_copy(receipt)
            result = {"operation_id": operation_id, "fingerprint": fingerprint,
                      "status": "queued", "mission_progress_verified": False,
                      "durable": self._sink is not None,
                      "text": redact_payload(message), "artifact_ids": list(artifact_ids)}
            if dispatcher is not None and not callable(getattr(self._sink, "record_required", None)):
                result.update(status="failed", error="receipt_persistence_unavailable",
                              durable=False)
                return result
            try:
                self.record_event("session", session_id, "SessionCommandQueued",
                                  dict(result), session_id=session_id, require_durable=True)
            except Exception:
                result.update(status="failed", error="receipt_persistence_failed")
                return result
            if dispatcher is None:
                return result
            try:
                delivery = dispatcher(_deep_copy(session), message, list(artifact_ids))
                if not isinstance(delivery, dict) or delivery.get("status") != "delivered":
                    raise ValueError("transport did not acknowledge delivery")
                result["status"] = "delivered"
                result["delivery"] = redact_payload(delivery)
            except Exception as exc:
                result.update(status="failed", error="transport_error",
                              detail=redact_payload(str(exc)))
            try:
                self.record_event("session", session_id, "SessionCommandSettled",
                                  _deep_copy(result), session_id=session_id, require_durable=True)
            except Exception:
                # The durable queued receipt prevents resending after restart.
                result.update(status="queued", error="receipt_settlement_unknown")
                return result
            if result["status"] == "delivered":
                session["messages"] += 1
                session["updated_at"] = self._stamp()
            return result

    def record_tool_call(self, session_id, tool, detail=""):
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(session_id)
        s["tool_calls"] += 1
        s["updated_at"] = self._stamp()
        self.record_event("session", session_id, "SessionToolCalled",
                          {"tool": tool, "detail": detail},
                          session_id=session_id)

    # -------------------------------------------------------------- services
    def register_service(self, service_name, host_id, port,
                         session_id=None, status="healthy", route=None,
                         description=None, owner=None, project=None, source_port=None, ownership=None,
                         repository_id=None, commit_sha=None):
        git = normalize_service_git(repository_id, commit_sha)
        project = normalize_project(project)
        source_port = normalize_source_port(source_port)
        svcid = new_id("service")
        rec = {
            "service_id": svcid, "service_name": service_name,
            "host_id": host_id, "port": port, "session_id": session_id,
            "status": status, "route": route, "owner": owner,
            "project": project, "source_port": source_port,
            "ownership": normalize_service_ownership(ownership), **git,
            "metadata_updated_at": self._stamp(),
            "created_at": self._stamp(), "updated_at": self._stamp(),
            "url": route or f"http://{host_id}:{port}",
        }
        rec["verified_origin"] = capture_service_origin(rec, self._sessions.get(session_id), rec["created_at"])
        rec["description"] = (description or "").strip() or describe_service(
            service_name=service_name, host_id=host_id, port=port,
            owner=owner, route=rec["url"])
        track_availability(rec, status, rec["created_at"])
        self._services[svcid] = rec
        self.record_event("service", svcid, "ServiceRegistered",
                          {"service_name": service_name, "host_id": host_id,
                           "port": port, "route": route, "status": status,
                           "description": rec["description"],
                           "owner": owner, "project": project, "source_port": source_port,
                           "ownership": rec["ownership"], "session_id": session_id, **git,
                           "verified_origin": rec["verified_origin"], "metadata_updated_at": rec["metadata_updated_at"],
                           "unavailable_since": rec.get("unavailable_since")},
                          session_id=session_id)
        return svcid

    def upsert_service(self, host_id, service_name, port, **kwargs):
        # Serialize health and metadata observations across the two reporters.
        with self._event_lock:
            return self._upsert_service(host_id, service_name, port, **kwargs)

    def _upsert_service(self, host_id, service_name, port, *,
                       session_id=_SERVICE_UNSET, status="healthy", route=None,
                       description=None, owner=None, project=None, source_port=None,
                       ownership=_SERVICE_UNSET, repository_id=_SERVICE_UNSET, commit_sha=_SERVICE_UNSET):
        """Legacy heartbeats preserve omitted metadata; explicit observations replace it."""
        explicit = any(value is not _SERVICE_UNSET for value in (session_id, ownership, repository_id, commit_sha))
        for svcid, rec in self._services.items():
            if (rec.get("host_id") != host_id or rec.get("service_name") != service_name
                    or rec.get("discovery_source") == "native_listener"):
                continue
            inactive = str(status).lower() in INACTIVE
            clear = ownership is None and not inactive
            old_repository = rec.get("repository_id")
            resolved_repository = (None if clear else old_repository) if repository_id is _SERVICE_UNSET else repository_id
            resolved_commit = (None if clear or resolved_repository != old_repository else rec.get("commit_sha")) if commit_sha is _SERVICE_UNSET else commit_sha
            resolved_session = (None if clear else rec.get("session_id")) if session_id is _SERVICE_UNSET else session_id
            resolved_ownership = rec.get("ownership") if ownership is _SERVICE_UNSET else ownership
            if inactive and ownership is None:
                resolved_ownership = rec.get("ownership")
                resolved_session = resolved_session or rec.get("session_id")
                if repository_id in (_SERVICE_UNSET, None) and commit_sha in (_SERVICE_UNSET, None):
                    resolved_repository, resolved_commit = old_repository, rec.get("commit_sha")
            git = normalize_service_git(resolved_repository, resolved_commit)
            rec["port"] = port
            track_availability(rec, status, self._stamp())
            rec.update(status=status, session_id=resolved_session,
                       ownership=normalize_service_ownership(resolved_ownership), **git)
            rec["route"] = route if route is not None else rec.get("route")
            rec["url"] = rec["route"] or f"http://{host_id}:{port}"
            if owner is not None:
                rec["owner"] = owner
            if project is not None:
                rec["project"] = normalize_project(project)
            if source_port is not None:
                rec["source_port"] = normalize_source_port(source_port)
            provided = (description or "").strip()
            rec["description"] = provided or rec.get("description") or describe_service(
                service_name=service_name, host_id=host_id, port=port,
                owner=owner or rec.get("owner"), route=rec["route"])
            rec["updated_at"] = self._stamp()
            if explicit:
                rec["metadata_updated_at"] = rec["updated_at"]
                rec["verified_origin"] = capture_service_origin(rec, self._sessions.get(resolved_session), rec["updated_at"])
            self.record_event("service", svcid, "ServiceUpdated", {
                key: _deep_copy(rec.get(key)) for key in (
                    "service_name", "host_id", "port", "route", "status", "description", "owner",
                    "project", "source_port", "ownership", "session_id", "repository_id", "commit_sha",
                    "verified_origin", "unavailable_since", "metadata_updated_at", "listener_identity", "metadata_observation")},
                session_id=resolved_session)
            return svcid
        values = {key: None if value is _SERVICE_UNSET else value for key, value in (
            ("session_id", session_id), ("ownership", ownership), ("repository_id", repository_id), ("commit_sha", commit_sha))}
        return self.register_service(service_name, host_id, port, status=status,
            route=route, description=description, owner=owner, project=project, source_port=source_port, **values)

    def observe_service_metadata(self, service_id, payload):
        with self._event_lock:
            return self._observe_service_metadata(service_id, payload)

    def _observe_service_metadata(self, service_id, payload):
        """Observe ownership only; never create a listener or refresh its health."""
        rec = self._services.get(service_id)
        if rec is None:
            raise KeyError(service_id)
        if (payload.get("host_id") != rec.get("host_id")
                or normalize_source_port(payload.get("source_port")) is None
                or payload["source_port"] != rec.get("source_port")):
            raise ValueError("metadata must match the existing service host and source port")
        if rec.get("discovery_source") == "native_listener":
            raise ValueError("native listener records require service.listener_observed")
        observation = normalize_service_observation(payload, self._now(), rec.get("metadata_observation"))
        state = observation["state"]
        candidate = _deep_copy(rec)
        if state == "observed":
            pid, start = payload.get("listener_pid"), payload.get("listener_start")
            if (type(pid) is not int or pid < 1 or not isinstance(start, str) or not start
                    or len(start) > 300 or any(ord(char) < 32 for char in start)):
                raise ValueError("metadata requires a positive listener PID and process birth identity")
            instance = {"pid": pid, "start": start}
            old = rec.get("listener_identity") or {
                "pid": (rec.get("ownership") or {}).get("listener_pid"),
                "start": (rec.get("ownership") or {}).get("listener_start")}
            changed = instance != old
            proof = normalize_service_ownership(payload.get("ownership")) if "ownership" in payload else (None if changed else rec.get("ownership"))
            if proof is not None and (proof.get("kind") not in {"herdr_process_ancestry", "system_unit"}
                    or proof.get("host_id") != rec["host_id"] or proof.get("listener_pid") != pid
                    or proof.get("listener_start") != start):
                raise ValueError("ownership proof must match the observed listener instance")
            clear = changed or ("ownership" in payload and proof is None)
            candidate.update(listener_identity=instance, ownership=proof,
                session_id=payload.get("session_id", (proof or {}).get("session_id") or (None if clear else rec.get("session_id"))))
            repository = payload.get("repository_id", None if clear else rec.get("repository_id"))
            commit = payload.get("commit_sha", None if clear or repository != rec.get("repository_id") else rec.get("commit_sha"))
            candidate.update(normalize_service_git(repository, commit))
            candidate["verified_origin"] = capture_service_origin(candidate,
                self._sessions.get(candidate.get("session_id")), self._stamp())
        candidate["metadata_observation"] = observation
        candidate["metadata_updated_at"] = self._stamp()
        fields = ("host_id", "source_port", "ownership", "session_id", "verified_origin", "repository_id",
                  "commit_sha", "listener_identity", "metadata_observation", "metadata_updated_at")
        self.record_event("service", service_id, "ServiceMetadataObserved", {
            **{key: _deep_copy(candidate.get(key)) for key in fields},
            "service_updated_at": rec.get("updated_at"), "service_created_at": rec.get("created_at")},
            session_id=candidate.get("session_id"), publish=False)
        rec.update({key: candidate.get(key) for key in fields if key not in {"host_id", "source_port"}})
        return self.get_service(service_id)

    def observe_service_listener(self, service_id, payload):
        """Observe an unforwarded native listener without operating the dev fabric."""
        with self._event_lock:
            return self._observe_service_listener(service_id, payload)

    def _observe_service_listener(self, service_id, payload):
        host, pid, birth, port = (payload.get(key) for key in (
            "host_id", "listener_pid", "listener_start", "source_port"))
        if service_id != service_listener_id(host, pid, birth, port):
            raise ValueError("listener advertisement must target its physical instance ID")
        existing = self._services.get(service_id)
        if existing is not None and existing.get("discovery_source") != "native_listener":
            raise ValueError("listener advertisements cannot alter a fabric-managed service")
        observation = normalize_service_observation(payload, self._now(),
            existing.get("metadata_observation") if existing else None)
        stamp = self._stamp()
        if observation["state"] in {"missing", "unconfirmed"}:
            if existing is None:
                raise KeyError(service_id)
            candidate = _deep_copy(existing)
            candidate["status"] = "unknown"
        else:
            candidate = _deep_copy(existing) if existing else {
                "service_id": service_id, "host_id": host, "source_port": port, "port": port,
                "discovery_source": "native_listener", "endpoint_scope": "local_only",
                "route": None, "url": None, "created_at": stamp,
                "listener_identity": {"pid": pid, "start": birth}, "unavailable_since": None}
            advertised = payload.get("ownership")
            proof = normalize_service_ownership(advertised) if advertised is not None else candidate.get("ownership")
            if (not proof or proof.get("kind") != "herdr_process_ancestry"
                    or proof.get("host_id") != host or proof.get("listener_pid") != pid
                    or proof.get("listener_start") != birth):
                raise ValueError("new listener requires verified native HerdR process ancestry")
            candidate.update(ownership=proof, session_id=proof.get("session_id"))
            candidate["verified_origin"] = capture_service_origin(candidate,
                self._sessions.get(candidate.get("session_id")), stamp)
            if candidate["verified_origin"] is None:
                raise ValueError("listener native ownership could not be verified")
            candidate.update(normalize_service_git(payload.get("repository_id", candidate.get("repository_id")),
                payload.get("commit_sha", None if payload.get("repository_id", candidate.get("repository_id")) != candidate.get("repository_id") else candidate.get("commit_sha"))))
            def label(key, fallback):
                value = payload.get(key)
                return value[:200] if isinstance(value, str) and value and all(ord(c) >= 32 for c in value) else fallback
            candidate["owner"] = label("owner", candidate.get("owner"))
            candidate["service_name"] = label("service_name", candidate.get("service_name") or f"App on {host} (port {port})")
            candidate["description"] = f"{candidate.get('owner') or 'Application'} listener on {host}, observed port {port}; no shared route"
            if "project" in payload:
                candidate["project"] = normalize_project(payload["project"])
            candidate["status"] = "healthy"
        candidate.update(updated_at=stamp, metadata_updated_at=stamp, metadata_observation=observation,
                         endpoint_scope="local_only", route=None, url=None)
        self.record_event("service", service_id, "ServiceListenerObserved", _deep_copy(candidate),
                          session_id=candidate.get("session_id"), publish=False)
        self._services[service_id] = candidate
        return self.get_service(service_id)

    def _ensure_service_desc(self, rec):
        """Guarantee ``rec`` carries a non-empty human description.

        Applied at read time so every service returned to the API/UI is
        self-explanatory, including records hydrated from an older event log
        that predates explicit descriptions.
        """
        if not (rec.get("description") or "").strip():
            rec["description"] = describe_service(
                service_name=rec.get("service_name"),
                host_id=rec.get("host_id"), port=rec.get("port"),
                owner=rec.get("owner"), route=rec.get("route"))
        return rec

    def get_service(self, service_id):
        s = self._services.get(service_id)
        if s is None:
            return None
        return self._service_projection(s)

    def _service_projection(self, record):
        result = _copy(self._ensure_service_desc(record))
        result["project"] = normalize_project(result.get("project"))
        result["source_port"] = normalize_source_port(result.get("source_port"))
        result["catalog"] = service_catalog(result, self._sessions.get(result.get("session_id")))
        for field in ("repository_id", "commit_sha", "project_id"):
            result[field] = result["catalog"][field]
        result["lifecycle"] = service_lifecycle(
            result, self._now(), archive_after_days=self._service_archive_days)
        return self._present_host_id(result)

    def list_services(self, *, include_archived=True):
        services = [self._service_projection(s) for s in self._services.values()]
        return _sort_by([s for s in services if include_archived
                         or not s["lifecycle"]["archived"]], "created_at", reverse=True)

    def retire_services(self, *, older_than_days=7.0, states=("unknown",), dry_run=True, limit=5000):
        """Archive services with no report for ``older_than_days`` (operator action).

        Nothing is deleted: each record keeps its history and a later report from
        the service lifts the retirement. ``dry_run`` only counts."""
        with self._event_lock:
            cutoff = float(older_than_days) * 86400
            matched = []
            for record in self._services.values():
                view = self._service_projection(record)
                life = view["lifecycle"]
                if (life["state"] in states and not life["archived"]
                        and (life["age_seconds"] is None or life["age_seconds"] >= cutoff)):
                    matched.append(record)
                    if len(matched) >= limit:
                        break
            if not dry_run:
                stamp = self._stamp()
                for record in matched:
                    record["retired_at"] = stamp
                    self.record_event("service", record["service_id"], "ServiceRetired",
                                      {"retired_at": stamp, "older_than_days": float(older_than_days)})
            return {"matched": len(matched), "retired": 0 if dry_run else len(matched),
                    "dry_run": bool(dry_run), "older_than_days": float(older_than_days),
                    "sample": [r["service_id"] for r in matched[:5]]}

    def mark_service_unavailable(self, service_id):
        with self._event_lock:
            return self._mark_service_unavailable(service_id)

    def _mark_service_unavailable(self, service_id):
        s = self._services.get(service_id)
        if s is None:
            return False
        if s.get("status") == "unavailable":
            return True
        track_availability(s, "unavailable", self._stamp())
        s["status"] = "unavailable"
        s["updated_at"] = self._stamp()
        self.record_event("service", service_id, "ServiceUnavailable",
                          {"status": "unavailable",
                           "unavailable_since": s["unavailable_since"]},
                          session_id=s.get("session_id"))
        return True

    # ---------------------------------------------------------------- hosts
    def register_host(self, host_name, tailnet_ip):
        hid = new_id("host")
        rec = {
            "host_id": hid, "host_name": host_name, "tailnet_ip": tailnet_ip,
            "status": "healthy", "last_seen": self._stamp(),
            "created_at": self._stamp(),
        }
        self._hosts[hid] = rec
        self.record_event("host", hid, "HostRegistered",
                          {"host_name": host_name, "tailnet_ip": tailnet_ip})
        return hid

    def get_host(self, host_id):
        return self._merged_hosts().get(self._canonical_host(host_id))

    def _merged_hosts(self):
        """Hosts keyed by canonical id; an alias contributes its freshest report."""
        merged = {}
        for hid, rec in self._hosts.items():
            if hid in self._host_aliases:
                continue
            merged[hid] = _copy(rec)
        for alias, canonical in self._host_aliases.items():
            rec = self._hosts.get(alias)
            if rec is None:
                continue
            base = merged.get(canonical)
            if base is None:
                base = merged[canonical] = _copy(rec)
                base["host_id"] = base["host_name"] = canonical
                continue
            if (rec.get("last_seen") or "") > (base.get("last_seen") or ""):
                for key in ("last_seen", "status", "cpu", "mem"):
                    if rec.get(key) is not None:
                        base[key] = rec[key]
            if rec.get("inventory") and not base.get("inventory"):
                base["inventory"] = _copy(rec["inventory"])
        return merged

    def list_hosts(self):
        return _sort_by(list(self._merged_hosts().values()), "host_name")

    def fleet_inventory(self):
        """Host x component version matrix with drift against the fleet reference.

        The reference for a component is its most common version across hosts that
        report it (ties go to the highest version string); a host that reports a
        different value is ``drifted`` and one that does not report it is ``missing``.
        """
        hosts = [h for h in self.list_hosts() if h.get("inventory")]
        names = sorted(h["host_name"] or h["host_id"] for h in hosts)
        by_name = {h["host_name"] or h["host_id"]: h["inventory"] for h in hosts}
        components = []
        for group in ("tools", "plugins"):
            keys = sorted({k for inv in by_name.values() for k in inv.get(group, {})})
            for key in keys:
                seen = {n: by_name[n].get(group, {}).get(key) for n in names}
                counts = {}
                for v in seen.values():
                    if v:
                        counts[v] = counts.get(v, 0) + 1
                ref = max(counts, key=lambda v: (counts[v], v)) if counts else None
                components.append({
                    "group": group, "name": key, "reference": ref,
                    "hosts": {n: {"version": v,
                                  "state": ("missing" if not v else
                                            "ok" if v == ref else "drifted")}
                              for n, v in seen.items()},
                    "drifted": sum(1 for v in seen.values() if v and v != ref),
                    "missing": sum(1 for v in seen.values() if not v)})
        return {"hosts": [{"host": n, "collected_at": by_name[n].get("collected_at")}
                          for n in names],
                "components": components,
                "summary": {"hosts": len(names),
                            "components": len(components),
                            "drifted": sum(c["drifted"] for c in components),
                            "missing": sum(c["missing"] for c in components)}}

    def record_heartbeat(self, host_id, tailnet_ip=None, cpu=None, mem=None):
        h = self._hosts.get(host_id)
        if h is None:
            return False
        h["last_seen"] = self._stamp()
        h["status"] = "healthy"
        if tailnet_ip is not None:
            h["tailnet_ip"] = tailnet_ip
        if cpu is not None:
            h["cpu"] = cpu
        if mem is not None:
            h["mem"] = mem
        return True

    def mark_host_unavailable(self, host_id):
        h = self._hosts.get(host_id)
        if h is None:
            return False
        h["status"] = "unavailable"
        return True

    def record_telemetry(self, host_id, sample):
        """Record a telemetry sample (cpu/mem/disk/network/gpu) for a host."""
        rec = {"host_id": host_id, "at": self._stamp(), **dict(sample or {})}
        samples = self._telemetry.setdefault(host_id, [])
        samples.append(rec)
        del samples[:-500]  # a day of 2-minute samples is plenty; keep memory bounded
        return True

    def list_telemetry(self, host_id, limit=100):
        samples = self._telemetry.get(host_id, [])
        return samples[-limit:]

    # ---------------------------------------------------------- ingestion
    # Idempotent upserts that honor an external (runtime-supplied) entity id, so
    # agents/machines can report their lifecycle without minting new ids on every
    # heartbeat. The durable sink persists each recorded event, so re-hydration
    # after a restart reproduces exactly the same stable entities.

    def upsert_host(self, host_id, host_name, tailnet_ip, *,
                    cpu=None, mem=None, status="healthy", inventory=None):
        stamp = self._stamp()
        inventory = _clean_inventory(inventory)
        existing = self._hosts.get(host_id)
        if existing is None:
            rec = {"host_id": host_id, "host_name": host_name,
                   "tailnet_ip": tailnet_ip, "status": status,
                   "cpu": cpu, "mem": mem,
                   "last_seen": stamp, "created_at": stamp}
            if inventory:
                rec["inventory"] = {**inventory, "collected_at": stamp}
            self._hosts[host_id] = rec
            self.record_event("host", host_id, "HostRegistered",
                              {"host_name": host_name,
                               "tailnet_ip": tailnet_ip,
                               **({"inventory": inventory} if inventory else {})})
            return _copy(rec)
        existing["host_name"] = host_name or existing["host_name"]
        existing["tailnet_ip"] = tailnet_ip or existing["tailnet_ip"]
        existing["status"] = status
        existing["last_seen"] = stamp
        if cpu is not None:
            existing["cpu"] = cpu
        if mem is not None:
            existing["mem"] = mem
        payload = {"host_name": existing["host_name"],
                   "tailnet_ip": existing["tailnet_ip"],
                   "cpu": cpu, "mem": mem}
        if inventory:
            existing["inventory"] = {**inventory, "collected_at": stamp}
            payload["inventory"] = inventory
        self.record_event("host", host_id, "HostHeartbeat", payload)
        return _copy(existing)

    def upsert_session(self, session_id, *, host_id=None, mission_id=None,
                       agent_role="", model="", status="active",
                       cwd=None, branch=None, pid=None, cmdline=None,
                       summary=None, attach=None, source="herdr", pane_id=None,
                       terminal_id=None, herdr_session=None, machine=None,
                       agent_session=None, revision=None, interactive_ready=None,
                       launch_pending=None, state_change_seq=None,
                       repository_id=None, commit_sha=None,
                       transcript=None, parent_session_id=None, name=None):
        if transcript is not None:
            if (not isinstance(transcript, dict) or transcript.get("status") not in (
                    "recording", "unsupported", "identity_unavailable", "unreadable", "unavailable") or
                    not isinstance(transcript.get("truncated", False), bool)):
                raise ValueError("transcript requires a supported status and boolean truncated flag")
            repair = transcript.get("identity_repair")
            if repair is not None and repair not in (
                    "repaired", "binding_changed", "identity_unavailable", "report_rejected", "report_unverified"):
                raise ValueError("transcript identity_repair must be a supported status")
            transcript = {"status": transcript["status"], "truncated": transcript.get("truncated", False),
                          **({"identity_repair": repair} if repair is not None else {})}
        stamp = self._stamp()
        existing = self._sessions.get(session_id)
        if (existing is not None and host_id is not None
                and existing.get("host_id") not in (None, host_id)):
            raise ValueError("session host collision; emit a host-namespaced session ID")
        binding = (_session_mission_identity(mission_id, source)
                   if mission_id is not None or existing is None else
                   {field: existing.get(field) for field in ("mission_key", "mission_source")})
        detail = {"cwd": cwd, "branch": branch, "pid": pid, **binding,
                  "cmdline": cmdline, "summary": summary, "attach": attach,
                  "pane_id": pane_id, "terminal_id": terminal_id,
                  "herdr_session": herdr_session, "machine": machine,
                  "agent_session": agent_session, "revision": revision,
                  "interactive_ready": interactive_ready, "launch_pending": launch_pending,
                  "state_change_seq": state_change_seq,
                  "repository_id": repository_id, "commit_sha": commit_sha,
                  "transcript": transcript, "parent_session_id": _label(parent_session_id, session_id), "name": _label(name)}
        if existing is None:
            rec = {"session_id": session_id, "mission_id": mission_id,
                   "host_id": host_id, "agent_role": agent_role,
                   "model": model, "status": status,
                   "created_at": stamp, "updated_at": stamp,
                   "messages": 0, "tool_calls": 0, **detail}
            self._sessions[session_id] = rec
            if mission_id:
                key = binding["mission_key"]
                if key in self._missions:
                    self._missions[key]["sessions"].append(session_id)
            self.record_event("session", session_id, "SessionStarted",
                              {"host_id": host_id, "mission_id": mission_id,
                               "agent_role": agent_role, "model": model,
                               "status": status, **{k: v for k, v in detail.items() if v is not None}},
                              mission_id=mission_id, session_id=session_id)
            return _copy(rec)
        existing["updated_at"] = stamp
        if host_id is not None:
            existing["host_id"] = host_id
        if mission_id is not None:
            existing["mission_id"] = mission_id
            # Explicit reassignment replaces reverse membership; a heartbeat
            # without a mission identity cannot change its namespace.
            for key, mission in self._missions.items():
                members = mission["sessions"]
                if session_id in members and key != binding["mission_key"]:
                    members.remove(session_id)
                if key == binding["mission_key"] and session_id not in members:
                    members.append(session_id)
        if agent_role:
            existing["agent_role"] = agent_role
        if model:
            existing["model"] = model
        existing["status"] = status
        if repository_id is not None and repository_id != existing.get("repository_id") and commit_sha is None:
            existing["commit_sha"] = None
        for k, v in detail.items():
            if v is not None:
                existing[k] = v
        # Carry the full identity state in the delta so event-sourced
        # hydration (bridge._reconstruct_from_events) can rebuild the record.
        self.record_event("session", session_id, "SessionUpdated",
                          {"commit_sha": existing.get("commit_sha"), **{k: existing[k] for k in (
                              "host_id", "mission_id", "mission_key", "mission_source", "agent_role", "model",
                              "status", "cwd", "branch", "pid", "cmdline",
                              "summary", "attach", "pane_id", "terminal_id", "herdr_session",
                              "machine", "agent_session", "revision", "interactive_ready",
                              "launch_pending", "state_change_seq", "repository_id", "commit_sha",
                              "transcript", "parent_session_id", "name") if existing.get(k) is not None}},
                          mission_id=existing["mission_id"],
                          session_id=session_id)
        return _copy(existing)

    def upsert_mission(self, mission_id, *, title, purpose="", stage="active",
                       cwd=None, source="herdr", repository_id=None, commit_sha=None):
        if ":" in source or ":" in mission_id:
            raise ValueError(
                "mission source and mission_id must not contain ':'")
        key = mission_id if source == "herdr" else f"{source}:{mission_id}"
        stamp = self._stamp()
        existing = self._missions.get(key)
        if existing is None:
            title = title or mission_id  # only default to the id on create
            rec = {"mission_id": mission_id, "title": title,
                   "purpose": purpose, "stage": stage, "status": stage,
                   "cwd": cwd, "source": source, "mission_key": key,
                   "repository_id": repository_id, "commit_sha": commit_sha,
                   "created_at": stamp, "updated_at": stamp,
                   "sessions": [sid for sid, session in self._sessions.items()
                                if session.get("mission_key") == key]}
            self._missions[key] = rec
            self.record_event("mission", key, "MissionStarted",
                              {"title": title, "purpose": purpose,
                               "stage": stage, "cwd": cwd,
                               "source": source, "repository_id": repository_id,
                               "commit_sha": commit_sha},
                              mission_id=mission_id)
            return _copy(rec)
        existing["updated_at"] = stamp
        if title:
            existing["title"] = title
        if purpose:
            existing["purpose"] = purpose
        if cwd:
            existing["cwd"] = cwd
        if repository_id is not None and repository_id != existing.get("repository_id") and commit_sha is None:
            existing["commit_sha"] = None
        for field, value in (("repository_id", repository_id), ("commit_sha", commit_sha)):
            if value is not None:
                existing[field] = value
        if stage:
            existing["stage"] = existing["status"] = stage
        self.record_event("mission", key, "MissionUpdated",
                          {"title": existing["title"],
                           "purpose": existing["purpose"],
                           "stage": existing["stage"],
                           "status": existing["status"],
                           "cwd": existing.get("cwd"), "source": source,
                           "repository_id": existing.get("repository_id"),
                           "commit_sha": existing.get("commit_sha")},
                          mission_id=mission_id)
        return _copy(existing)

    # --------------------------------------------- CI: test runs / builds
    def upsert_test_run(
        self,
        run_id,
        *,
        mission_id=None,
        session_id=None,
        repository=None,
        status=None,
        passed=None,
        failed=None,
        skipped=None,
        total=None,
        started_at=None,
        ended_at=None,
        kind="test",
        results=None,
    ):
        stamp = self._stamp()
        existing = self._test_runs.get(run_id)
        results = None if results is None else [dict(r) for r in results]
        if existing is None:
            rec = {
                "test_run_id": run_id,
                "kind": kind,
                "mission_id": mission_id,
                "session_id": session_id,
                "repository": repository,
                "status": status,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "total": total,
                "started_at": started_at or stamp,
                "ended_at": ended_at,
                "results": [] if results is None else results,
                "created_at": stamp,
                "updated_at": stamp,
            }
            self._test_runs[run_id] = rec
            self.record_event(
                "test_run",
                run_id,
                "TestRunStarted" if kind == "test" else "BuildStarted",
                {
                    k: copy.deepcopy(rec[k])
                    for k in (
                        "kind",
                        "mission_id",
                        "session_id",
                        "repository",
                        "status",
                        "passed",
                        "failed",
                        "skipped",
                        "total",
                        "started_at",
                        "ended_at",
                        "results",
                    )
                },
                mission_id=mission_id,
                session_id=session_id,
            )
            return _deep_copy(rec)
        existing["updated_at"] = stamp
        for k, v in {
            "mission_id": mission_id,
            "session_id": session_id,
            "repository": repository,
            "status": status,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": total,
            "started_at": started_at,
            "ended_at": ended_at,
        }.items():
            if v is not None:
                existing[k] = v
        if results is not None:
            existing["results"] = results
        stored_kind = existing.get("kind") or "test"
        self.record_event(
            "test_run",
            run_id,
            "TestRunUpdated" if stored_kind == "test" else "BuildUpdated",
            {
                k: copy.deepcopy(existing[k])
                for k in (
                    "kind",
                    "mission_id",
                    "session_id",
                    "repository",
                    "status",
                    "passed",
                    "failed",
                    "skipped",
                    "total",
                    "started_at",
                    "ended_at",
                    "results",
                )
            },
            mission_id=existing["mission_id"],
            session_id=existing["session_id"],
        )
        return _deep_copy(existing)

    def upsert_build(self, build_id, **kw):
        return self.upsert_test_run(build_id, kind="build", **kw)

    def get_test_run(self, run_id):
        return _deep_copy(self._test_runs.get(run_id))

    def list_test_runs(self, mission_id=None, kind=None):
        out = []
        for rec in self._test_runs.values():
            if mission_id is not None and rec.get("mission_id") != mission_id:
                continue
            if kind is not None and rec.get("kind") != kind:
                continue
            out.append(_deep_copy(rec))
        return _sort_by(out, "created_at", id_key="test_run_id")

    # ------------------------------------------------------------ reviews
    def upsert_review(
        self,
        review_id,
        *,
        mission_id=None,
        session_id=None,
        reviewer=None,
        status=None,
        verdict=None,
        started_at=None,
        completed_at=None,
    ):
        stamp = self._stamp()
        existing = self._reviews.get(review_id)
        if existing is None:
            rec = {
                "review_id": review_id,
                "mission_id": mission_id,
                "session_id": session_id,
                "reviewer": reviewer,
                "status": status,
                "verdict": verdict,
                "started_at": started_at or stamp,
                "completed_at": completed_at,
                "findings": [],
                "created_at": stamp,
                "updated_at": stamp,
            }
            self._reviews[review_id] = rec
            self.record_event(
                "review",
                review_id,
                "ReviewStarted",
                {
                    k: rec[k]
                    for k in (
                        "mission_id",
                        "session_id",
                        "reviewer",
                        "status",
                        "verdict",
                        "started_at",
                        "completed_at",
                    )
                },
                mission_id=mission_id,
                session_id=session_id,
            )
            return _deep_copy(rec)
        existing["updated_at"] = stamp
        for k, v in {
            "mission_id": mission_id,
            "session_id": session_id,
            "reviewer": reviewer,
            "status": status,
            "verdict": verdict,
            "started_at": started_at,
            "completed_at": completed_at,
        }.items():
            if v is not None:
                existing[k] = v
        self.record_event(
            "review",
            review_id,
            "ReviewUpdated",
            {
                k: existing[k]
                for k in (
                    "mission_id",
                    "session_id",
                    "reviewer",
                    "status",
                    "verdict",
                    "started_at",
                    "completed_at",
                )
            },
            mission_id=existing["mission_id"],
            session_id=existing["session_id"],
        )
        return _deep_copy(existing)

    def add_review_finding(
        self, review_id, *, severity, summary, detail="", status="open", finding_id=None
    ):
        review = self._reviews.get(review_id)
        if review is None:
            raise KeyError(review_id)
        findings = review["findings"]
        if finding_id is None:
            finding_id = f"{review_id}:{len(findings) + 1}"
        finding = {
            "finding_id": finding_id,
            "severity": severity,
            "summary": summary,
            "detail": detail,
            "status": status,
        }
        for i, f in enumerate(findings):
            if f["finding_id"] == finding_id:
                findings[i] = finding
                break
        else:
            findings.append(finding)
        self.record_event(
            "review",
            review_id,
            "ReviewFindingAdded",
            copy.deepcopy(finding),
            mission_id=review["mission_id"],
        )
        return copy.deepcopy(finding)

    def get_review(self, review_id):
        return _deep_copy(self._reviews.get(review_id))

    def list_reviews(self, mission_id=None):
        out = []
        for rec in self._reviews.values():
            if mission_id is not None and rec.get("mission_id") != mission_id:
                continue
            out.append(_deep_copy(rec))
        return _sort_by(out, "created_at", id_key="review_id")

    # ------------------------------------------------------- pull requests
    def upsert_pull_request(
        self,
        pr_id,
        *,
        mission_id=None,
        repository=None,
        number=None,
        url=None,
        status=None,
        branch=None,
        base=None,
        created_at=None,
    ):
        stamp = self._stamp()
        existing = self._pull_requests.get(pr_id)
        if existing is None:
            rec = {
                "pr_id": pr_id,
                "mission_id": mission_id,
                "repository": repository,
                "number": number,
                "url": url,
                "status": status,
                "branch": branch,
                "base": base,
                "created_at": created_at or stamp,
                "updated_at": stamp,
            }
            self._pull_requests[pr_id] = rec
            self.record_event(
                "pull_request",
                pr_id,
                "PullRequestOpened",
                {
                    k: rec[k]
                    for k in (
                        "mission_id",
                        "repository",
                        "number",
                        "url",
                        "status",
                        "branch",
                        "base",
                        "created_at",
                    )
                },
                mission_id=mission_id,
            )
            if rec["status"] == "merged":
                self._record_pr_merged(pr_id, rec)
            return _copy(rec)
        old_status = existing.get("status")
        existing["updated_at"] = stamp
        for k, v in {
            "mission_id": mission_id,
            "repository": repository,
            "number": number,
            "url": url,
            "status": status,
            "branch": branch,
            "base": base,
            "created_at": created_at,
        }.items():
            if v is not None:
                existing[k] = v
        self.record_event(
            "pull_request",
            pr_id,
            "PullRequestUpdated",
            {
                k: existing[k]
                for k in (
                    "mission_id",
                    "repository",
                    "number",
                    "url",
                    "status",
                    "branch",
                    "base",
                    "created_at",
                )
            },
            mission_id=existing["mission_id"],
        )
        if existing["status"] == "merged" and old_status != "merged":
            self._record_pr_merged(pr_id, existing)
        return _copy(existing)

    def _record_pr_merged(self, pr_id, rec):
        self.record_event(
            "pull_request",
            pr_id,
            "PullRequestMerged",
            {
                "pr_id": pr_id,
                "mission_id": rec["mission_id"],
                "repository": rec["repository"],
                "number": rec["number"],
                "status": rec["status"],
            },
            mission_id=rec["mission_id"],
        )

    def get_pull_request(self, pr_id):
        return _copy(self._pull_requests.get(pr_id))

    def list_pull_requests(self, mission_id=None):
        out = []
        for rec in self._pull_requests.values():
            if mission_id is not None and rec.get("mission_id") != mission_id:
                continue
            out.append(_copy(rec))
        return _sort_by(out, "created_at", id_key="pr_id")

    # ------------------------------------------------------------- artifacts
    def register_artifact(self, session_id=None, original_filename="",
                          mime_type="", size_bytes=0, sha256="",
                          storage_key="", mission_id=None, data=None):
        import hashlib
        if data is not None and not sha256:
            sha256 = hashlib.sha256(data).hexdigest()
        aid = new_id("artifact")
        rec = {
            "artifact_id": aid, "session_id": session_id,
            "mission_id": mission_id, "original_filename": original_filename,
            "mime_type": mime_type, "size_bytes": size_bytes, "sha256": sha256,
            "storage_key": storage_key, "status": "available",
            "materializations": 0, "created_at": self._stamp(),
        }
        self._artifacts[aid] = rec
        if data is not None:
            self._blobs[aid] = data
            rec["size_bytes"] = len(data)
        # Persist full metadata so artifact records survive a restart via the
        # event log (payload bytes live in the object store, not the event).
        self.record_event("artifact", aid, "ArtifactRegistered",
                          {"filename": original_filename, "sha256": sha256,
                           "storage_key": storage_key,
                           "size_bytes": rec.get("size_bytes", 0),
                           "mime_type": mime_type,
                           "session_id": session_id,
                           "mission_id": mission_id},
                          mission_id=mission_id, session_id=session_id)
        return aid

    def get_artifact_data(self, artifact_id):
        return self._blobs.get(artifact_id)

    def get_artifact(self, artifact_id):
        return _copy(self._artifacts.get(artifact_id))

    def materialize_artifact(self, artifact_id, host_id, session_id,
                             local_path):
        a = self._artifacts.get(artifact_id)
        if a is None:
            return False
        a["materializations"] += 1
        a["last_materialized"] = {"host_id": host_id,
                                  "session_id": session_id,
                                  "local_path": local_path,
                                  "at": self._stamp()}
        return True

    # --------------------------------------------------------------- leases
    def register_lease(self, host_id, target_port, dev_port):
        rec = {
            "lease_id": new_id("lease"), "host_id": host_id,
            "target_port": target_port, "dev_port": dev_port,
            "state": "active", "created_at": self._stamp(),
        }
        self._leases[rec["lease_id"]] = rec
        return _copy(rec)

    def list_leases(self):
        return [_copy(x) for x in self._leases.values()]

    def close_lease(self, lease_id):
        if lease_id in self._leases:
            del self._leases[lease_id]
            return True
        return False

    # ---------------------------------------------------------- CI history
    def ingest_ci_report(self, body):
        from .ci_reports import normalize_report, reconcile_report_identities
        with self._event_lock:
            report = normalize_report(body, self._stamp())
            existing = self._events.get(("ci_report", report["report_id"]))
            if existing:
                return copy.deepcopy(existing[-1]["payload"])
            history = self.ci_reports()
            projected = copy.deepcopy(report)
            reconcile_report_identities([projected], history["pipelines"])
            attempts = [row.get("attempt", 1) for row in history["reports"]
                        if (row.get("repository"), str(row.get("provider_repo_id")), row.get("run_number")) ==
                           (projected["repository"], str(projected["provider_repo_id"]), projected["run_number"])]
            report["attempt"] = max(attempts, default=0) + 1
            report["ingested_at"] = self._stamp()
            self.record_event("ci_report", report["report_id"], "CITestReportReceived", report,
                              require_durable=True)
            self._trim_ci_history("ci_report")
            return copy.deepcopy(report)

    def observe_ci_pipeline(self, row):
        row = redact_payload(copy.deepcopy(row))
        key = f"{row.get('provider_repo_id') or row.get('repository')}:{row.get('run_number')}"
        with self._event_lock:
            previous = self._events.get(("ci_observation", key), [])
            if previous and previous[-1]["payload"] == row:
                return
            self.record_event("ci_observation", key, "CIPipelineObserved", row,
                              require_durable=True)
            self._trim_ci_history("ci_observation")

    def _trim_ci_history(self, kind=None):
        from .ci_reports import HISTORY_BYTES, PIPELINE_LIMIT, REPORT_LIMIT
        keep = set()
        kinds = {kind} if kind is not None else {"ci_report", "ci_observation"}
        arrivals = {tuple(entry): index for index, entry in enumerate(self._event_order)}
        for kind, limit in (("ci_report", REPORT_LIMIT), ("ci_observation", PIPELINE_LIMIT)):
            if kind not in kinds:
                continue
            keys = [key for key in self._events if key[0] == kind]
            for key in keys:
                for event in self._events[key]:
                    if isinstance(event.get("payload"), str):
                        event["payload"] = json.loads(event["payload"])
            keys.sort(key=lambda key: (self._events[key][-1].get("ingest_timestamp", ""),
                                      arrivals.get((*key, self._events[key][-1]["sequence"]), -1)),
                      reverse=True)
            size = 0
            for key in keys[:limit]:
                latest = self._events[key][-1]
                size += len(json.dumps(latest).encode())
                if size > HISTORY_BYTES:
                    break
                self._events[key] = [latest]
                keep.add(key)
            for key in keys:
                if key not in keep:
                    del self._events[key]
        self._event_order = [entry for entry in self._event_order
                             if entry[0] not in kinds
                             or ((entry[0], entry[1]) in keep and
                                 entry[2] == self._events[(entry[0], entry[1])][-1]["sequence"])]

    def ci_reports(self):
        from .ci_reports import HISTORY_BYTES, PIPELINE_LIMIT, REPORT_LIMIT
        with self._event_lock:
            arrivals = {tuple(entry): index for index, entry in enumerate(self._event_order)}
            def rows(kind):
                events = [stream[-1] for (etype, _), stream in self._events.items()
                          if etype == kind and stream]
                events.sort(key=lambda event: (event.get("ingest_timestamp", ""), arrivals.get(
                    (event["entity_type"], event["entity_id"], event["sequence"]), -1)), reverse=True)
                return [copy.deepcopy(event["payload"]) for event in events]
            reports, pipelines = rows("ci_report"), rows("ci_observation")
            from .ci_reports import reconcile_report_identities
            reconcile_report_identities(reports, pipelines)
            return {"reports": reports, "pipelines": pipelines,
                    "scope": {"kind": "retained_observations", "complete": False,
                              "persistence": "durable_sink" if self._sink is not None else "memory_only",
                              "report_limit": REPORT_LIMIT, "pipeline_limit": PIPELINE_LIMIT,
                              "byte_limit_per_kind": HISTORY_BYTES,
                              "report_count": len(reports), "pipeline_count": len(pipelines),
                              "description": "Uploaded test reports and observed provider runs only; missing reports do not mean zero tests."}}

    # --------------------------------------------------------------- events
    def record_event(self, entity_type, entity_id, event_type, payload=None,
                     mission_id=None, session_id=None, require_durable=False, publish=True):
        with self._event_lock:
            key = (entity_type, entity_id)
            stream = self._events.setdefault(key, [])
            seq = max((e.get("sequence", -1) for e in stream), default=-1) + 1
            if entity_type in {"ci_report", "ci_observation"}:
                seq = max(seq, self._ci_sequence)
            ev = {
                "event_id": new_id("event"),
                "schema_version": 1,
                "source_timestamp": self._stamp(),
                "ingest_timestamp": self._stamp(),
                "entity_type": entity_type, "entity_id": entity_id,
                "mission_id": mission_id, "session_id": session_id,
                "sequence": seq, "event_type": event_type,
                "payload": payload or {},
            }
            if require_durable and self._sink is not None:
                # Command side effects require persistence before submission. A
                # legacy best-effort sink cannot establish this prerequisite.
                try:
                    self._sink.record_required(ev)
                except Exception:
                    if entity_type in {"ci_report", "ci_observation"} and not stream:
                        self._events.pop(key, None)
                    raise
            if entity_type in {"ci_report", "ci_observation"}:
                self._ci_sequence = seq + 1
            stream.append(ev)
            self._event_order.append((entity_type, entity_id, seq))
            if self._sink is not None and not require_durable:
                try:
                    self._sink.record(ev)
                except Exception:
                    pass  # a sink must never break event recording
            if publish:
                for listener in self._listeners:
                    try:
                        listener(_copy(ev))
                    except Exception:
                        pass  # a listener must never break event recording
            return _copy(ev)

    def mission_timeline(self, mission_key):
        """Build a per-mission timeline from mission events, CI runs, reviews
        and pull requests. Items are compact (no payloads), sorted by
        (ts, kind, entity_id)."""
        m = self.get_mission(mission_key)
        if m is None:
            raise KeyError(mission_key)
        key = m.get('mission_key') or m['mission_id']
        items = []
        for ev in self.stream('mission', key):
            et = ev.get('event_type')
            if et == 'MissionCreated':
                continue
            payload = ev.get('payload') or {}
            if et == 'MissionStageChanged':
                status = payload.get('to')
            else:
                status = payload.get('stage')
            items.append({'ts': ev.get('source_timestamp'), 'kind': 'stage',
                          'status': status,
                          'title': f"stage \u2192 {status}", 'ref': None,
                          'entity_id': key})
        for rec in self.list_test_runs(mission_id=key):
            items.append({'ts': rec.get('started_at') or rec.get('created_at'),
                          'kind': rec.get('kind') or 'test',
                          'status': rec.get('status'),
                          'title': f"{rec.get('kind') or 'test'} run {rec.get('test_run_id')}",
                          'ref': None,
                          'entity_id': rec.get('test_run_id')})
        for rec in self.list_reviews(mission_id=key):
            items.append({'ts': rec.get('started_at') or rec.get('created_at'),
                          'kind': 'review',
                          'status': rec.get('verdict') or rec.get('status'),
                          'title': f"review by {rec.get('reviewer')}",
                          'ref': None,
                          'entity_id': rec.get('review_id')})
        for rec in self.list_pull_requests(mission_id=key):
            items.append({'ts': rec.get('created_at'), 'kind': 'pr',
                          'status': rec.get('status'),
                          'title': f"PR #{rec.get('number')}",
                          'ref': rec.get('url'),
                          'entity_id': rec.get('pr_id')})
            if rec.get('status') == 'merged':
                items.append({'ts': rec.get('updated_at') or rec.get('created_at'),
                              'kind': 'merge', 'status': 'merged',
                              'title': f"PR #{rec.get('number')} merged",
                              'ref': rec.get('url'),
                              'entity_id': rec.get('pr_id')})
        return sorted(items, key=lambda i: (i['ts'] or '', i['kind'],
                                            i['entity_id'] or ''))

    def stream(self, entity_type, entity_id):
        return [_copy(e) for e in self._events.get((entity_type, entity_id), [])]

    def recent_events(self, limit=50):
        out = []
        by_sequence = {}
        with self._event_lock:
            for etype, eid, seq in reversed(self._event_order):
                key = (etype, eid)
                if key not in by_sequence:
                    # Restore may contain gaps AND duplicate sequence numbers.
                    # Stable stream order disambiguates each chronological occurrence.
                    buckets = {}
                    for ev in self._events.get(key, []):
                        buckets.setdefault(ev.get("sequence"), []).append(ev)
                    by_sequence[key] = buckets
                candidates = by_sequence[key].get(seq, [])
                if not candidates:
                    continue
                out.append(_copy(candidates.pop()))
                if len(out) >= limit:
                    break
        return out

    # ------------------------------------------- LLM usage, keys, custom models
    # All persisted as events (entity types ``token_usage``, ``llm_key``,
    # ``custom_model``) so the existing durable sink and replay carry them.
    def record_token_usage(self, project, model, input_tokens=0, output_tokens=0, *,
                           reasoning_tokens=0, cache_read_tokens=0, duration_ms=None,
                           success=True, request_id=None, session_id=None,
                           mission_id=None, key_id=None, source="llm-proxy"):
        if not project:
            raise ValueError("project is required")
        payload = {"project": project, "model": model or "unknown",
                   "input_tokens": int(input_tokens or 0),
                   "output_tokens": int(output_tokens or 0),
                   "reasoning_tokens": int(reasoning_tokens or 0),
                   "cache_read_tokens": int(cache_read_tokens or 0),
                   "duration_ms": duration_ms, "success": bool(success),
                   "request_id": request_id, "key_id": key_id, "source": source}
        return self.record_event("token_usage", project, "token_usage.recorded",
                                 payload, mission_id=mission_id, session_id=session_id)

    def token_analytics(self, project=None, since=None):
        """Aggregate token usage by project, model and day (UTC)."""
        total = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                 "reasoning_tokens": 0, "cache_read_tokens": 0, "failed": 0}
        by_project, by_model, by_day = {}, {}, {}

        def add(bucket, key, p):
            b = bucket.setdefault(key, {k: 0 for k in total})
            _accumulate(b, p)

        for (etype, eid), stream in self._events.items():
            if etype != "token_usage" or (project and eid != project):
                continue
            for ev in stream:
                if since and ev["source_timestamp"] < since:
                    continue
                p = _payload(ev)
                _accumulate(total, p)
                add(by_project, p["project"], p)
                add(by_model, p["model"], p)
                add(by_day, ev["source_timestamp"][:10], p)
        return {"total": total, "by_project": by_project, "by_model": by_model,
                "by_day": dict(sorted(by_day.items()))}

    def create_project_key(self, project, label=""):
        """Mint a gateway key for ``project``; the secret is returned once and only
        its SHA-256 is stored."""
        if not project:
            raise ValueError("project is required")
        import hashlib
        import secrets
        key = "hk-" + secrets.token_urlsafe(32)
        key_id = new_id("llmkey")
        self.record_event("llm_key", key_id, "llm_key.created", {
            "key_id": key_id, "project": project, "label": label,
            "key_hash": hashlib.sha256(key.encode()).hexdigest(), "revoked": False})
        return {"key_id": key_id, "project": project, "key": key}

    def revoke_project_key(self, key_id):
        keys = {k["key_id"]: k for k in self._llm_keys()}
        if key_id not in keys:
            raise KeyError(key_id)
        self.record_event("llm_key", key_id, "llm_key.revoked",
                          dict(keys[key_id], revoked=True))

    def _llm_keys(self):
        out = []
        for (etype, _), stream in self._events.items():
            if etype == "llm_key" and stream:
                out.append(dict(_payload(stream[-1])))
        return out

    def list_project_keys(self):
        return [{k: v for k, v in key.items() if k != "key_hash"}
                for key in self._llm_keys()]

    def upsert_custom_model(self, name, base_url, upstream_model=None, project=None):
        if not name or not base_url:
            raise ValueError("name and base_url are required")
        if not str(base_url).startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        payload = {"name": name, "base_url": base_url.rstrip("/"),
                   "upstream_model": upstream_model or name, "project": project,
                   "deleted": False}
        return self.record_event("custom_model", name, "custom_model.upserted", payload)

    def delete_custom_model(self, name):
        cur = self.list_custom_models(include_deleted=True).get(name)
        if cur is None:
            raise KeyError(name)
        self.record_event("custom_model", name, "custom_model.deleted",
                          dict(cur, deleted=True))

    def list_custom_models(self, include_deleted=False):
        out = {}
        for (etype, name), stream in self._events.items():
            if etype == "custom_model" and stream:
                p = _payload(stream[-1])
                if include_deleted or not p.get("deleted"):
                    out[name] = dict(p)
        return out

    def llm_config(self):
        """What the LLM proxy needs: active key hashes and custom models."""
        return {"keys": [{"key_id": k["key_id"], "project": k["project"],
                          "key_hash": k["key_hash"]}
                         for k in self._llm_keys() if not k.get("revoked")],
                "custom_models": list(self.list_custom_models().values())}

    # ------------------------------------------------------------- analytics
    def mission_analytics(self):
        by_stage = {}
        for m in self._missions.values():
            by_stage[m["stage"]] = by_stage.get(m["stage"], 0) + 1
        total = len(self._missions)
        completed = sum(1 for m in self._missions.values()
                        if m["stage"] == "complete")
        return {"total": total, "completed": completed,
                "completion_rate": _pct(completed, total), "by_stage": by_stage}

    def model_analytics(self):
        usage = {}
        for s in self._sessions.values():
            model = s.get("model") or "unknown"
            u = usage.setdefault(model, {"sessions": 0, "messages": 0})
            u["sessions"] += 1
            u["messages"] += s.get("messages", 0)
        return {"models": usage, "total_sessions": len(self._sessions)}

    def host_analytics(self):
        hosts = self._merged_hosts().values()
        healthy = sum(1 for h in hosts if h["status"] == "healthy")
        total = len(hosts)
        return {"total": total, "healthy": healthy,
                "unavailable": total - healthy}

    # --------------------------------------------------- backup / restore
    def export_state(self):
        """Serialize the full structured state for backup/restore (Spec §102)."""
        return {
            "version": 1,
            "missions": _copy(self._missions),
            "sessions": _copy(self._sessions),
            "services": _copy(self._services),
            "hosts": _copy(self._hosts),
            "artifacts": _copy(self._artifacts),
            "blobs": {k: __import__("base64").b64encode(v).decode()
                      for k, v in self._blobs.items()},
            "leases": _copy(self._leases),
            "telemetry": {k: [dict(s) for s in v]
                          for k, v in self._telemetry.items()},
            "events": {f"{t}|{i}": [dict(e) for e in s]
                       for (t, i), s in self._events.items()},
            "event_order": list(self._event_order),
            "ci_sequence": self._ci_sequence,
            "agent_projections": copy.deepcopy(list(self._agent_records.values())),
        }

    def import_state(self, state):
        """Restore state from an export (used by restore tests / recovery)."""
        self._missions = dict(state.get("missions", {}))
        self._sessions = dict(state.get("sessions", {}))
        self._services = dict(state.get("services", {}))
        self._hosts = dict(state.get("hosts", {}))
        self._artifacts = dict(state.get("artifacts", {}))
        self._blobs = {k: __import__("base64").b64decode(v)
                       for k, v in state.get("blobs", {}).items()}
        self._leases = dict(state.get("leases", {}))
        self._telemetry = {k: [dict(s) for s in v]
                          for k, v in state.get("telemetry", {}).items()}
        self._events = {}
        for key, stream in state.get("events", {}).items():
            t, i = key.split("|")
            self._events[(t, i)] = [dict(e) for e in stream]
        self._event_order = [tuple(x) for x in state.get("event_order", [])]
        self._ci_sequence = max(int(state.get("ci_sequence", 0)),
                                max((event.get("sequence", -1) + 1
                                     for key, stream in self._events.items()
                                     if key[0] in {"ci_report", "ci_observation"}
                                     for event in stream), default=0))
        self._trim_ci_history()
        # Replay durable naming reservations before any session is presented.
        # Summary continuity lives outside transcript retention and public feeds.
        projections = list(state.get("agent_projections", []))
        self._agent_records = {}
        self._agent_sequence = 0
        self.agent_identities = AgentIdentityRegistry(persist=self._persist_agent_projection)
        private_types = {"agent_identity", "agent_summary"}
        for key in [k for k in self._events if k[0] in private_types]:
            projections.extend(self._events.pop(key))
        self._event_order = [row for row in self._event_order if row[0] not in private_types]
        for event in projections:
            if event.get("entity_type") not in private_types:
                continue
            event = {**event, "payload": _payload(event)}
            key = (event["entity_type"], event["entity_id"])
            self._agent_records[key] = event
            self._agent_sequence = max(self._agent_sequence, int(event.get("sequence") or 0))
        self.agent_identities.hydrate([event for (kind, _), event in self._agent_records.items()
                                       if kind == "agent_identity"])
        # Inspector summaries ride the durable log as ``inspector`` streams;
        # move them into the bounded store instead of the unbounded raw streams.
        durable = []
        for key in [k for k in self._events if k[0] == "inspector"]:
            durable.extend(self._events.pop(key))
        self._event_order = [t for t in self._event_order if t[0] != "inspector"]
        # HCN events are private to the collaboration service: they must never sit
        # in the shared streams that /activity and the global SSE feed expose.
        for key in [k for k in self._events if k[0] == "collab"]:
            self._collab_replay.extend(self._events.pop(key))
        self._event_order = [t for t in self._event_order if t[0] != "collab"]
        if durable:
            self.inspector.hydrate(durable)

    def take_collab_replay(self):
        """Hand the diverted durable HCN events to the collaboration service (once)."""
        out, self._collab_replay = self._collab_replay, []
        return out

    # --------------------------------------------------------------- health
    def health(self):
        # Repo-level stats only. Deployment context (whether this repo is
        # hydrated from durable Postgres, image version/commit) is overlaid by
        # the HTTP server in api.ControlPlaneServer._health(), because the repo
        # itself cannot know about its source.
        return {
            "status": "ok",
            "events": {"ingested": len(self._event_order)},
            "missions": len(self._missions), "sessions": len(self._sessions),
            "hosts": len(self._hosts), "services": len(self._services),
            "inspector": self.inspector.health(),
            "timestamp": self._stamp(),
        }


# ------------------------------------------------------------ helpers
def _payload(ev):
    """An event's payload as a dict (a hydrated jsonb column arrives as text)."""
    p = ev.get("payload") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    return p if isinstance(p, dict) else {}


def _accumulate(bucket, p):
    bucket["requests"] += 1
    for k in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens"):
        bucket[k] += int(p.get(k) or 0)
    if not p.get("success", True):
        bucket["failed"] += 1


def _copy(d):
    return dict(d) if d is not None else None


def _deep_copy(d):
    """Copy a record including its nested lists (findings, results)."""
    if d is None:
        return None
    return copy.deepcopy(d)


def _iso(ts):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).isoformat()


def _sort_by(items, key, reverse=False, id_key=None):
    """Sort by ``key`` with ``id_key`` as a stable tie-breaker."""
    if id_key is None:
        return sorted(items, key=lambda x: x.get(key) or "", reverse=reverse)
    if reverse:
        return sorted(items, key=lambda x: (x.get(key) or "", x.get(id_key) or ""),
                      reverse=True)
    return sorted(items, key=lambda x: (x.get(key) or "", x.get(id_key) or ""))


def _pct(part, whole):
    return 0.0 if whole == 0 else round(100.0 * part / whole, 1)
