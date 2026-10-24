"""Neighbours of one entity (cross-linking design 2026-10-24, step 2).

Computed from the foreign keys the repo already holds (session -> mission/host/branch/
repository, service -> host/session, test/review/PR -> mission, usage -> session/mission).
The answer carries ids, labels and states only. A source that cannot be read is named in
``partial`` instead of showing an empty list that looks like "none".
"""
from __future__ import annotations

KINDS = ("mission", "session", "host", "service", "ci", "review", "pr", "project")
CAP = 20
_TEXT = 120


def _text(value, limit=_TEXT):
    if not isinstance(value, str):
        return ""
    value = "".join(ch for ch in value if ch.isprintable())
    return value.strip()[:limit]


class _Collector:
    def __init__(self):
        self.groups = {}
        self.more = {}

    def add(self, group, item):
        bucket = self.groups.setdefault(group, [])
        if any(existing["id"] == item["id"] for existing in bucket):
            return
        if len(bucket) >= CAP:
            self.more[group] = self.more.get(group, 0) + 1
            return
        bucket.append(item)

    def out(self):
        return {g: items for g, items in self.groups.items() if items}


def _item(id_, label, state=None, **extra):
    out = {"id": _text(str(id_), 300), "label": _text(label) or _text(str(id_)), "state": _text(state or "", 40) or None}
    out.update({k: v for k, v in extra.items() if v not in (None, "")})
    return out


def _session_item(s, relation=None):
    label = s.get("display_name") or s.get("agent_role") or s.get("session_id")
    return _item(s["session_id"], label, s.get("status"), relation=relation)


def _mission_item(m):
    return _item(m.get("mission_key") or m.get("mission_id"), m.get("title") or m.get("mission_id"), m.get("status"))


def _ci_id(row):
    return f"{row.get('provider_repo_id') or row.get('repository')}:{row.get('run_number')}"


def _ci_item(row):
    label = f"{_text(str(row.get('repository') or ''))} · run #{row.get('run_number')}"
    return _item(_ci_id(row), label, row.get("state") or row.get("status"),
                 repository=_text(str(row.get("repository") or "")), branch=_text(str(row.get("branch") or "")),
                 run_number=row.get("run_number"),
                 repo_filter=_text(str(row.get("provider_repo_id") or row.get("repository") or ""), 200))


def _pr_item(p):
    label = f"{p.get('repository') or 'PR'} #{p.get('number')}" if p.get("number") is not None else (p.get("repository") or p["pr_id"])
    return _item(p["pr_id"], label, p.get("status"), repository=_text(str(p.get("repository") or "")),
                 number=p.get("number"))


def _review_item(r):
    return _item(r["review_id"], r.get("reviewer") or "Review", r.get("verdict") or r.get("status"))


def _repo_match(session, row):
    repo_id = session.get("repository_id")
    if not repo_id:
        return False
    if repo_id not in (row.get("provider_repo_id") and str(row["provider_repo_id"]), row.get("repository")):
        return False
    return not session.get("branch") or not row.get("branch") or session["branch"] == row["branch"]


def related(repo, kind, entity_id, *, now_llm=True):
    """``None`` for an unknown entity, else ``{kind, id, relations, more, partial}``."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(entity_id, str) or not entity_id or len(entity_id) > 300:
        raise ValueError("id is required")
    partial = []
    c = _Collector()

    try:
        sessions = repo.list_sessions()
    except Exception:  # noqa: BLE001 - one unreadable source must not hide the others
        sessions, partial = [], partial + ["sessions"]
    try:
        missions = repo.list_missions()
    except Exception:  # noqa: BLE001
        missions, partial = [], partial + ["missions"]
    by_session = {s["session_id"]: s for s in sessions}
    by_mission = {m.get("mission_key") or m["mission_id"]: m for m in missions}

    def pipelines():
        try:
            return repo.ci_reports()["pipelines"]
        except Exception:  # noqa: BLE001
            if "ci" not in partial:
                partial.append("ci")
            return []

    def mission_extras(mission_id):
        for r in repo.list_reviews(mission_id):
            c.add("review", _review_item(r))
        for p in repo.list_pull_requests(mission_id):
            c.add("pr", _pr_item(p))

    def llm(**flt):
        if not now_llm:
            return None
        try:
            groups = repo.llm_stats("24h", "all", source="llm-proxy", **flt)["groups"]
            problems = repo.llm_errors("24h", **flt)["problems"]
        except Exception:  # noqa: BLE001
            if "llm" not in partial:
                partial.append("llm")
            return None
        g = groups[0] if groups else None
        return {"requests": g["requests"] if g else 0, "tokens_out": g["tokens"]["output"] if g else 0,
                "problems": sum(p.get("count", 0) for p in problems)}

    extra = {}
    if kind == "mission":
        m = repo.get_mission(entity_id)
        if m is None:
            return None
        key = m.get("mission_key") or m["mission_id"]
        mine = [s for s in sessions if s.get("mission_key") == key or s["session_id"] in (m.get("sessions") or [])]
        for s in mine:
            c.add("agents", _session_item(s))
            if s.get("host_id"):
                c.add("host", _item(s["host_id"], s["host_id"]))
        for row in pipelines():
            if any(_repo_match(s, row) for s in mine):
                c.add("ci", _ci_item(row))
        mission_extras(m["mission_id"])
        extra["llm"] = llm(mission=key)
    elif kind == "session":
        s = by_session.get(entity_id)
        if s is None:
            return None
        mkey = s.get("mission_key")
        if mkey and mkey in by_mission:
            c.add("mission", _mission_item(by_mission[mkey]))
            mission_extras(by_mission[mkey]["mission_id"])
        if s.get("host_id"):
            c.add("host", _item(s["host_id"], s["host_id"]))
        for other in sessions:
            if other["session_id"] == entity_id:
                continue
            if other.get("parent_session_id") == entity_id:
                c.add("agents", _session_item(other, "child"))
            elif other["session_id"] == s.get("parent_session_id"):
                c.add("agents", _session_item(other, "parent"))
        try:
            for svc in repo.list_services():
                if svc.get("session_id") == entity_id:
                    c.add("service", _item(svc["service_id"], svc.get("service_name") or svc["service_id"], svc.get("status")))
        except Exception:  # noqa: BLE001
            partial.append("services")
        for row in pipelines():
            if _repo_match(s, row):
                c.add("ci", _ci_item(row))
        for r in repo.list_reviews():
            if r.get("session_id") == entity_id:
                c.add("review", _review_item(r))
        extra["llm"] = llm(session=entity_id)
    elif kind == "host":
        host = repo.get_host(entity_id)
        if host is None:
            return None
        for s in sessions:
            if s.get("host_id") == host["host_id"]:
                c.add("agents", _session_item(s))
        try:
            for svc in repo.list_services():
                if svc.get("host_id") == host["host_id"]:
                    c.add("service", _item(svc["service_id"], svc.get("service_name") or svc["service_id"], svc.get("status")))
        except Exception:  # noqa: BLE001
            partial.append("services")
    elif kind == "service":
        svc = repo.get_service(entity_id)
        if svc is None:
            return None
        if svc.get("host_id"):
            c.add("host", _item(svc["host_id"], svc["host_id"]))
        s = by_session.get(svc.get("session_id"))
        if s is not None:
            c.add("agents", _session_item(s))
    elif kind == "ci":
        row = next((r for r in pipelines() if _ci_id(r) == entity_id), None)
        if row is None:
            return None
        for s in sessions:
            if _repo_match(s, row):
                c.add("agents", _session_item(s))
                mkey = s.get("mission_key")
                if mkey in by_mission:
                    c.add("mission", _mission_item(by_mission[mkey]))
                    mission_extras(by_mission[mkey]["mission_id"])
                if s.get("host_id") and row.get("host_id") == s["host_id"]:
                    c.add("host", _item(s["host_id"], s["host_id"]))
        for p in repo.list_pull_requests():
            if p.get("repository") == row.get("repository") and p.get("branch") and p.get("branch") == row.get("branch"):
                c.add("pr", _pr_item(p))
    elif kind == "review":
        r = repo.get_review(entity_id)
        if r is None:
            return None
        if r.get("session_id") in by_session:
            c.add("agents", _session_item(by_session[r["session_id"]]))
        mission = repo.get_mission(r["mission_id"]) if r.get("mission_id") else None
        if mission is not None:
            c.add("mission", _mission_item(mission))
            for p in repo.list_pull_requests(r["mission_id"]):
                c.add("pr", _pr_item(p))
            for s in sessions:
                if s.get("mission_key") == (mission.get("mission_key") or mission["mission_id"]):
                    for row in pipelines():
                        if _repo_match(s, row):
                            c.add("ci", _ci_item(row))
    elif kind == "pr":
        p = repo.get_pull_request(entity_id)
        if p is None:
            return None
        mission = repo.get_mission(p["mission_id"]) if p.get("mission_id") else None
        if mission is not None:
            c.add("mission", _mission_item(mission))
            for r in repo.list_reviews(p["mission_id"]):
                c.add("review", _review_item(r))
            for s in sessions:
                if s.get("mission_key") == (mission.get("mission_key") or mission["mission_id"]):
                    c.add("agents", _session_item(s))
        for row in pipelines():
            if row.get("repository") == p.get("repository") and row.get("branch") and row.get("branch") == p.get("branch"):
                c.add("ci", _ci_item(row))
    else:  # project: the repository id
        mine = [s for s in sessions if s.get("repository_id") == entity_id]
        rows = [r for r in pipelines() if entity_id in (str(r.get("provider_repo_id")), r.get("repository"))]
        prs = [p for p in repo.list_pull_requests() if p.get("repository") == entity_id]
        if not (mine or rows or prs):
            return None
        for s in mine:
            c.add("agents", _session_item(s))
            mkey = s.get("mission_key")
            if mkey in by_mission:
                c.add("mission", _mission_item(by_mission[mkey]))
        for row in rows:
            c.add("ci", _ci_item(row))
        for p in prs:
            c.add("pr", _pr_item(p))
            for r in repo.list_reviews(p.get("mission_id")) if p.get("mission_id") else []:
                c.add("review", _review_item(r))

    out = {"kind": kind, "id": entity_id, "relations": c.out(), "more": c.more, "partial": sorted(set(partial))}
    if extra.get("llm") is not None:
        out["relations"]["llm"] = extra["llm"]
    return out
