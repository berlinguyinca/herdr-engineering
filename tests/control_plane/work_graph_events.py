"""A realistic, deterministic stream of source events for the Work Graph tests (collab log + Pi sessions + registry)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)


class Stream:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.seq = 0

    def at(self, minutes: float) -> datetime:
        return T0 + timedelta(minutes=minutes)

    def collab(self, minute: float, event_type: str, **payload) -> dict:
        self.seq += 1
        ev = {"event_id": f"ev-collab-{self.seq}", "entity_type": "collab", "entity_id": "log", "sequence": self.seq,
              "event_type": event_type, "source_timestamp": self.at(minute), "payload": payload}
        self.events.append(ev)
        return ev

    def session(self, minute: float, sid: str, event_type: str, *, room_id=None, workspace_id=None,
                agent_instance_id=None, mission_id=None, task_id=None, actor="key:pi1", scope="room", seqno=None, **payload) -> dict:
        self.seq += 1
        ev = {"event_id": f"ev-sess-{self.seq}", "entity_type": "session", "entity_id": sid, "sequence": self.seq,
              "event_type": event_type, "source_timestamp": self.at(minute), "payload": payload, "source": "pi",
              "source_sequence": seqno if seqno is not None else self.seq, "actor": {"kind": "ingest", "id": actor},
              "visibility_scope": scope, "room_id": room_id, "workspace_id": workspace_id, "agent_id": agent_instance_id,
              "mission_id": mission_id, "task_id": task_id}
        self.events.append(ev)
        return ev


def scenario(rooms_per_project: int = 2, tasks_per_room: int = 3) -> Stream:
    """alice manages p1 (rooms r1.., agent a1 owned by alice), bob manages p2.  Tasks are created, updated, finished; a
    Pi session runs and stops; an agent is revoked and a member removed (A07)."""
    s = Stream()
    m = 0.0
    for proj, owner in (("p1", "alice"), ("p2", "bob")):
        m += 1
        s.collab(m, "project_created", project_id=proj, name=f"Project {proj}", actor_user_id=owner)
        for r in range(rooms_per_project):
            rid = f"{proj}-r{r}"
            m += 1
            s.collab(m, "room_created", project_id=proj, room_id=rid, name=f"Room {rid}", actor_user_id=owner)
            s.collab(m + .1, "member_added", project_id=proj, room_id=rid, subject_user_id=owner, role="owner")
            s.collab(m + .2, "member_added", project_id=proj, room_id=rid, subject_user_id="carol", role="member")
        aid = f"{proj}-agent"
        m += 1
        s.collab(m, "agent_enrolled", agent_id=aid, owner_user_id=owner, name=f"{owner}.pi", harness_type="pi",
                 actor_user_id=owner)
        s.collab(m + .1, "agent_granted", project_id=proj, room_id=f"{proj}-r0", agent_id=aid, actions=["execute"],
                 actor_user_id=owner)
        for r in range(rooms_per_project):
            rid = f"{proj}-r{r}"
            for t in range(tasks_per_room):
                m += 1
                tid = f"{rid}-t{t}"
                s.collab(m, "task_created", project_id=proj, room_id=rid, task_id=tid, target_agent_id=aid,
                         text=f"Investigate failure {tid}\nlong body", actor_user_id=owner, created_by_kind="user",
                         created_by_id=owner, depth=0)
                s.collab(m + .3, "task_updated", project_id=proj, room_id=rid, task_id=tid, status="accepted",
                         actor_user_id=owner)
                s.collab(m + .6, "task_updated", project_id=proj, room_id=rid, task_id=tid, status="done", result="ok",
                         actor_user_id=owner)
    # a Pi session in p1-r0 with the new ids
    s.session(100, "sess-1", "SessionStarted", room_id="p1-r0", workspace_id="ws-9", agent_instance_id="p1-agent",
              mission_id="mis-1", task_id="p1-r0-t0", agent_role="worker", model="m1")
    s.session(110, "sess-1", "SessionStopped", room_id="p1-r0", workspace_id="ws-9", agent_instance_id="p1-agent",
              mission_id="mis-1", task_id="p1-r0-t0")
    # artifact events (registry-shaped)
    s.seq += 1
    s.events.append({"event_id": "ev-art-1", "entity_type": "artifact", "entity_id": "art-1", "sequence": s.seq,
                     "event_type": "artifact.version_committed", "source_timestamp": s.at(120), "source": "registry",
                     "source_sequence": 1, "actor": {"kind": "service", "id": "registry"},
                     "payload": {"artifact_id": "art-1", "version_id": "ver-1", "version_number": 1, "title": "CI report",
                                 "artifact_type": "report", "created_by": "alice", "state": "committed",
                                 "bindings": [{"room_id": "p1-r0", "task_id": "p1-r0-t0", "issue_ref": "acme/app#7",
                                               "project_id": "p1"}]}})
    s.seq += 1
    s.events.append({"event_id": "ev-art-2", "entity_type": "artifact", "entity_id": "art-1", "sequence": s.seq,
                     "event_type": "artifact.version_committed", "source_timestamp": s.at(130), "source": "registry",
                     "source_sequence": 2, "actor": {"kind": "service", "id": "registry"},
                     "payload": {"artifact_id": "art-1", "version_id": "ver-2", "version_number": 2, "title": "CI report",
                                 "parent_version_id": "ver-1", "created_by": "alice", "state": "committed",
                                 "bindings": [{"room_id": "p1-r0", "task_id": "p1-r0-t0"}]}})
    # A07: close down (a member leaves, the agent is revoked); nothing historical may disappear
    s.collab(200, "member_removed", project_id="p1", room_id="p1-r0", subject_user_id="carol")
    s.collab(210, "agent_revoked", agent_id="p1-agent", room_ids=["p1-r0"], actor_user_id="alice")
    return s
