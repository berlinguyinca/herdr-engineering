import asyncio

from herdr_engineering.control_plane import models


class _MemConn:
    """In-memory double faithful to the stores' SQL for missions/sessions/services."""

    def __init__(self):
        self.missions = {}
        self.sessions = {}
        self.services = {}

    async def fetchrow(self, sql, *a):
        if "FROM missions" in sql:
            return self.missions.get(a[0])
        if "FROM sessions" in sql:
            return self.sessions.get(a[0])
        if "FROM services" in sql:
            return self.services.get(a[0])
        return None

    async def fetch(self, sql, *a):
        if "FROM missions" in sql:
            rows = list(self.missions.values())
            if "WHERE status" in sql:
                rows = [r for r in rows if r["status"] == a[0]]
            return rows
        if "FROM sessions" in sql:
            return list(self.sessions.values())
        if "FROM services" in sql:
            rows = list(self.services.values())
            return rows
        return []

    async def execute(self, sql, *a):
        s = sql.strip()
        if s.startswith("INSERT INTO missions"):
            self.missions[a[0]] = {
                "mission_id": a[0], "title": a[1], "purpose": a[2],
                "repository": a[3], "branch": a[4], "worktree": a[5],
                "owner": a[6], "status": "active",
            }
        elif s.startswith("INSERT INTO sessions"):
            self.sessions[a[0]] = {
                "session_id": a[0], "mission_id": a[1], "title": a[2],
                "agent_role": a[3], "model": a[4], "host_id": a[5],
                "status": "active",
            }
        elif s.startswith("INSERT INTO services"):
            self.services[a[0]] = {
                "service_id": a[0], "service_name": a[1], "route": a[2],
                "port": a[3], "host_id": a[4], "mission_id": a[5],
                "session_id": a[6], "owner": a[7], "purpose": a[8],
                "health": "unknown",
            }
        elif s.startswith("UPDATE missions") and "status=" in s:
            self.missions[a[1]]["status"] = a[0]
        elif s.startswith("UPDATE services") and "health=" in s:
            self.services[a[1]]["health"] = a[0]
        elif s.startswith("UPDATE services") and "status='stopped'" in s:
            self.services[a[0]]["status"] = "stopped"
        return 1


def _run(coro):
    return asyncio.run(coro)


def test_mission_crud_and_list_filter():
    conn = _MemConn()
    ms = models.MissionStore(conn)
    mid = _run(ms.create("Dynamic Model Aliases", purpose="alias resolution"))
    assert mid.startswith("mission_")
    assert _run(ms.get(mid))["title"] == "Dynamic Model Aliases"
    assert len(_run(ms.list(status="active"))) == 1
    _run(ms.update(mid, status="complete"))
    assert _run(ms.get(mid))["status"] == "complete"
    assert len(_run(ms.list(status="active"))) == 0


def test_session_touch_and_service_lifecycle():
    conn = _MemConn()
    sid = _run(models.SessionStore(conn).create(mission_id="mission_x",
                                                agent_role="implementer"))
    assert sid.startswith("session_")
    svc = models.ServiceStore(conn)
    svcid = _run(svc.register(service_name="web", host_id="host_h", port=8080,
                              mission_id="mission_x", session_id=sid))
    assert svcid.startswith("service_")
    _run(svc.update_health(svcid, "healthy"))
    assert _run(svc.get(svcid))["health"] == "healthy"
    _run(svc.mark_stopped(svcid))
    assert _run(svc.get(svcid))["status"] == "stopped"
