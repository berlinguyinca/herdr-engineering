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

import time

from .ids import new_id

_STAGES = {
    "planning", "specification", "ready", "implementation", "testing",
    "review", "repair", "validation", "pr", "complete", "failed", "cancelled",
}
_SESSION_ACTIONS = {"steer", "approve", "interrupt", "resume", "stop"}


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
        return _copy(self._missions.get(mission_id))

    def list_missions(self, status=None):
        out = [_copy(m) for m in self._missions.values()]
        if status:
            out = [m for m in out if m["status"] == status]
        return _sort_by(out, "created_at", reverse=True)

    def set_mission_stage(self, mission_id, stage):
        if stage not in _STAGES:
            raise ValueError(f"unknown stage: {stage}")
        m = self._missions.get(mission_id)
        if m is None:
            raise KeyError(mission_id)
        old = m["stage"]
        m["stage"] = stage
        m["status"] = stage
        m["updated_at"] = self._stamp()
        self.record_event("mission", mission_id, "MissionStageChanged",
                          {"from": old, "to": stage}, mission_id=mission_id)

    # -------------------------------------------------------------- sessions
    def create_session(self, mission_id=None, agent_role="", model=""):
        sid = new_id("session")
        rec = {
            "session_id": sid, "mission_id": mission_id, "agent_role": agent_role,
            "model": model, "status": "active", "created_at": self._stamp(),
            "updated_at": self._stamp(), "messages": 0, "tool_calls": 0,
        }
        self._sessions[sid] = rec
        if mission_id and mission_id in self._missions:
            self._missions[mission_id]["sessions"].append(sid)
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
                         session_id=None, status="healthy"):
        svcid = new_id("service")
        rec = {
            "service_id": svcid, "service_name": service_name,
            "host_id": host_id, "port": port, "session_id": session_id,
            "status": status, "created_at": self._stamp(),
            "updated_at": self._stamp(),
            "url": f"http://{host_id}:{port}",
        }
        self._services[svcid] = rec
        self.record_event("service", svcid, "ServiceRegistered",
                          {"service_name": service_name, "host_id": host_id,
                           "port": port}, session_id=session_id)
        return svcid

    def get_service(self, service_id):
        return _copy(self._services.get(service_id))

    def list_services(self):
        return _sort_by([_copy(s) for s in self._services.values()],
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
        self.record_event("artifact", aid, "ArtifactRegistered",
                          {"filename": original_filename, "sha256": sha256},
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
            "timestamp": self._stamp(),
        }


# ------------------------------------------------------------ helpers
def _copy(d):
    return dict(d) if d is not None else None


def _iso(ts):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).isoformat()


def _sort_by(items, key, reverse=False):
    return sorted(items, key=lambda x: x.get(key) or "", reverse=reverse)


def _pct(part, whole):
    return 0.0 if whole == 0 else round(100.0 * part / whole, 1)
