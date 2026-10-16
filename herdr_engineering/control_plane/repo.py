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

import json
import time

from .ids import new_id
from .inspector import InspectorStore

_STAGES = {
    "planning", "specification", "ready", "implementation", "testing",
    "review", "repair", "validation", "pr", "complete", "failed", "cancelled",
}
_SESSION_ACTIONS = {"steer", "approve", "interrupt", "resume", "stop"}


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


class ControlPlaneRepo:
    """In-memory structured control-plane state with event sequencing."""

    def __init__(self, *, now=None, sink=None):
        """``sink`` is an optional durable writer (e.g. DurableEventSink) called
        with each recorded event so ingestion survives a restart."""
        self._sink = sink
        self._now = now or (lambda: time.time())
        self._missions = {}
        self._sessions = {}
        self._services = {}
        self._hosts = {}
        self._artifacts = {}
        self._blobs = {}  # artifact_id -> bytes (in-memory RustFS stand-in)
        self._leases = {}
        self._telemetry = {}  # host_id -> [sample, ...]
        self._events = {}  # (entity_type, entity_id) -> [event, ...]
        self._event_order = []  # (entity_type, entity_id, seq) chronological
        self._listeners = set()  # pub/sub for the SSE live stream (Spec §52)
        # Agent inspector (spec 0200): bounded per-session detail projection.
        self.inspector = InspectorStore.from_env(persist=self._persist_inspector)

    def _persist_inspector(self, ev):
        """Mirror low-volume inspector summaries to the existing durable sink only
        (never into the in-memory event streams or the global SSE feed)."""
        if self._sink is None:
            return
        ev = dict(ev)
        ev.setdefault("event_id", new_id("event"))
        ev.setdefault("schema_version", 1)
        ev["ingest_timestamp"] = self._stamp()
        ev["sequence"] = int(time.time() * 1000)
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

        For a non-herdr source the namespaced key (e.g. ``pi:MSN-1``) wins so
        a session for a pi-sourced mission never attaches to a herdr mission
        that happens to share the same raw id. An exact stored key is honoured
        (e.g. a fully-qualified ``pi:MSN-1`` passed with the default source),
        and the raw ``mission_id`` is the fallback so sessions/stages can
        attach to missions that are not yet present.
        """
        if source != "herdr":
            namespaced = f"{source}:{mission_id}"
            if namespaced in self._missions:
                return namespaced
        if mission_id in self._missions:
            return mission_id
        return mission_id

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
        rec = {
            "session_id": sid, "mission_id": mission_id, "agent_role": agent_role,
            "model": model, "status": "active", "created_at": self._stamp(),
            "updated_at": self._stamp(), "messages": 0, "tool_calls": 0,
        }
        self._sessions[sid] = rec
        if mission_id:
            key = self._resolve_mission_key(mission_id, source=source)
            if key in self._missions:
                self._missions[key]["sessions"].append(sid)
        self.record_event("session", sid, "SessionCreated",
                          {"mission_id": mission_id, "agent_role": agent_role},
                          mission_id=mission_id, session_id=sid)
        return sid

    def get_session(self, session_id):
        return _copy(self._sessions.get(session_id))

    def list_sessions(self, status=None):
        out = [_copy(s) for s in self._sessions.values()]
        if status:
            out = [s for s in out if s["status"] == status]
        return _sort_by(out, "created_at", reverse=True)

    def session_action(self, session_id, action):
        if action not in _SESSION_ACTIONS:
            raise ValueError(f"unknown session action: {action}")
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(session_id)
        s["status"] = "active" if action != "stop" else "stopped"
        s["updated_at"] = self._stamp()
        self.record_event("session", session_id, "SessionAction",
                          {"action": action}, session_id=session_id)

    def send_message(self, session_id, message, artifact_ids=None):
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(session_id)
        s["messages"] += 1
        s["updated_at"] = self._stamp()
        self.record_event(
            "session", session_id, "SessionMessageSent",
            {"text": message, "artifact_ids": artifact_ids or []},
            session_id=session_id)

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
                         description=None, owner=None):
        svcid = new_id("service")
        rec = {
            "service_id": svcid, "service_name": service_name,
            "host_id": host_id, "port": port, "session_id": session_id,
            "status": status, "route": route, "owner": owner,
            "created_at": self._stamp(), "updated_at": self._stamp(),
            "url": route or f"http://{host_id}:{port}",
        }
        rec["description"] = (description or "").strip() or describe_service(
            service_name=service_name, host_id=host_id, port=port,
            owner=owner, route=rec["url"])
        self._services[svcid] = rec
        self.record_event("service", svcid, "ServiceRegistered",
                          {"service_name": service_name, "host_id": host_id,
                           "port": port, "route": route, "status": status,
                           "description": rec["description"],
                           "owner": owner},
                          session_id=session_id)
        return svcid

    def upsert_service(self, host_id, service_name, port, *,
                       session_id=None, status="healthy", route=None,
                       description=None, owner=None):
        """Idempotent service registration keyed by ``(host_id, service_name)``.

        Re-invoking (e.g. on every web start or repeated ingest) updates the
        record in place instead of creating a duplicate, so restarts and
        heartbeats never accumulate service rows. Records ``ServiceRegistered``
        the first time and ``ServiceUpdated`` thereafter. A human ``description``
        is always present: an explicit one wins, otherwise it is derived from
        the name/host/port/process so every service is self-explanatory.
        """
        for svcid, rec in self._services.items():
            if (rec.get("host_id") == host_id
                    and rec.get("service_name") == service_name):
                rec["port"] = port
                rec["status"] = status
                rec["session_id"] = session_id
                rec["route"] = route if route is not None else rec.get("route")
                rec["url"] = route or f"http://{host_id}:{port}"
                if owner is not None:
                    rec["owner"] = owner
                provided = (description or "").strip()
                rec["description"] = provided or rec.get("description") \
                    or describe_service(
                        service_name=service_name, host_id=host_id,
                        port=port, owner=owner or rec.get("owner"),
                        route=rec["route"])
                rec["updated_at"] = self._stamp()
                self.record_event("service", svcid, "ServiceUpdated",
                                  {"service_name": service_name,
                                   "host_id": host_id, "port": port,
                                   "route": rec["route"], "status": status,
                                   "description": rec["description"],
                                   "owner": rec.get("owner")},
                                  session_id=session_id)
                return svcid
        return self.register_service(service_name, host_id, port,
                                     session_id=session_id, status=status,
                                     route=route, description=description,
                                     owner=owner)

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
        return _copy(self._ensure_service_desc(s))

    def list_services(self):
        return _sort_by([_copy(self._ensure_service_desc(s))
                         for s in self._services.values()],
                        "created_at", reverse=True)

    def mark_service_unavailable(self, service_id):
        s = self._services.get(service_id)
        if s is None:
            return False
        s["status"] = "unavailable"
        s["updated_at"] = self._stamp()
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
        return _copy(self._hosts.get(host_id))

    def list_hosts(self):
        return _sort_by([_copy(h) for h in self._hosts.values()],
                        "host_name")

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
        self._telemetry.setdefault(host_id, []).append(rec)
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
                    cpu=None, mem=None, status="healthy"):
        stamp = self._stamp()
        existing = self._hosts.get(host_id)
        if existing is None:
            rec = {"host_id": host_id, "host_name": host_name,
                   "tailnet_ip": tailnet_ip, "status": status,
                   "cpu": cpu, "mem": mem,
                   "last_seen": stamp, "created_at": stamp}
            self._hosts[host_id] = rec
            self.record_event("host", host_id, "HostRegistered",
                              {"host_name": host_name,
                               "tailnet_ip": tailnet_ip})
            return _copy(rec)
        existing["host_name"] = host_name or existing["host_name"]
        existing["tailnet_ip"] = tailnet_ip or existing["tailnet_ip"]
        existing["status"] = status
        existing["last_seen"] = stamp
        if cpu is not None:
            existing["cpu"] = cpu
        if mem is not None:
            existing["mem"] = mem
        self.record_event("host", host_id, "HostHeartbeat",
                          {"host_name": existing["host_name"],
                           "tailnet_ip": existing["tailnet_ip"],
                           "cpu": cpu, "mem": mem})
        return _copy(existing)

    def upsert_session(self, session_id, *, host_id=None, mission_id=None,
                       agent_role="", model="", status="active",
                       cwd=None, branch=None, pid=None, cmdline=None,
                       summary=None, attach=None, source="herdr"):
        stamp = self._stamp()
        existing = self._sessions.get(session_id)
        detail = {"cwd": cwd, "branch": branch, "pid": pid,
                  "cmdline": cmdline, "summary": summary, "attach": attach}
        if existing is None:
            rec = {"session_id": session_id, "mission_id": mission_id,
                   "host_id": host_id, "agent_role": agent_role,
                   "model": model, "status": status,
                   "created_at": stamp, "updated_at": stamp,
                   "messages": 0, "tool_calls": 0, **detail}
            self._sessions[session_id] = rec
            if mission_id:
                key = self._resolve_mission_key(mission_id, source=source)
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
        if agent_role:
            existing["agent_role"] = agent_role
        if model:
            existing["model"] = model
        existing["status"] = status
        for k, v in detail.items():
            if v is not None:
                existing[k] = v
        # Carry the full identity state in the delta so event-sourced
        # hydration (bridge._reconstruct_from_events) can rebuild the record.
        self.record_event("session", session_id, "SessionUpdated",
                          {k: existing[k] for k in (
                              "host_id", "mission_id", "agent_role", "model",
                              "status", "cwd", "branch", "pid", "cmdline",
                              "summary", "attach") if existing.get(k) is not None},
                          mission_id=existing["mission_id"],
                          session_id=session_id)
        return _copy(existing)

    def upsert_mission(self, mission_id, *, title, purpose="", stage="active",
                       cwd=None, source="herdr"):
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
                   "created_at": stamp, "updated_at": stamp, "sessions": []}
            self._missions[key] = rec
            self.record_event("mission", key, "MissionStarted",
                              {"title": title, "purpose": purpose,
                               "stage": stage, "cwd": cwd,
                               "source": source},
                              mission_id=mission_id)
            return _copy(rec)
        existing["updated_at"] = stamp
        if title:
            existing["title"] = title
        if purpose:
            existing["purpose"] = purpose
        if cwd:
            existing["cwd"] = cwd
        if stage:
            existing["stage"] = existing["status"] = stage
        self.record_event("mission", key, "MissionUpdated",
                          {"title": existing["title"],
                           "purpose": existing["purpose"],
                           "stage": existing["stage"],
                           "status": existing["status"],
                           "cwd": existing.get("cwd")},
                          mission_id=mission_id)
        return _copy(existing)

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

    # --------------------------------------------------------------- events
    def record_event(self, entity_type, entity_id, event_type, payload=None,
                     mission_id=None, session_id=None):
        key = (entity_type, entity_id)
        stream = self._events.setdefault(key, [])
        seq = len(stream)
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
        stream.append(ev)
        self._event_order.append((entity_type, entity_id, seq))
        if self._sink is not None:
            try:
                self._sink.record(ev)
            except Exception:
                pass  # a sink must never break event recording
        for listener in self._listeners:
            try:
                listener(_copy(ev))
            except Exception:
                pass  # a listener must never break event recording
        return _copy(ev)

    def stream(self, entity_type, entity_id):
        return [_copy(e) for e in self._events.get((entity_type, entity_id), [])]

    def recent_events(self, limit=50):
        out = []
        for etype, eid, seq in reversed(self._event_order):
            ev = self._events[(etype, eid)][seq]
            out.append(_copy(ev))
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
        healthy = sum(1 for h in self._hosts.values()
                      if h["status"] == "healthy")
        total = len(self._hosts)
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
        # Inspector summaries ride the durable log as ``inspector`` streams;
        # move them into the bounded store instead of the unbounded raw streams.
        durable = []
        for key in [k for k in self._events if k[0] == "inspector"]:
            durable.extend(self._events.pop(key))
        self._event_order = [t for t in self._event_order if t[0] != "inspector"]
        if durable:
            self.inspector.hydrate(durable)

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


def _iso(ts):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).isoformat()


def _sort_by(items, key, reverse=False):
    return sorted(items, key=lambda x: x.get(key) or "", reverse=reverse)


def _pct(part, whole):
    return 0.0 if whole == 0 else round(100.0 * part / whole, 1)
