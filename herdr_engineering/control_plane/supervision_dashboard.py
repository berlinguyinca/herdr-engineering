"""Authorized, read-only dashboard projection for tab supervision.

The caller supplies a collaboration ``room_flow`` already authorized for the
viewer. This module joins only exact collaboration ``agent_id`` values and never
derives ancestry from native sessions, panes, task edges, or layout.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _role(node: dict) -> str | None:
    fabric = node.get("fabric") if isinstance(node.get("fabric"), dict) else {}
    return (_text(fabric.get("agent_role")) or _text(node.get("helper_role"))
            or _text(node.get("family")))


def _unassigned(node: dict, provenance: str) -> dict:
    return {"agent_id": node["agent_id"], "name": _text(node.get("name")) or "removed",
            "role": _role(node), "provenance": provenance}


def _runtime(supervision: dict, node: dict, viewer_owner_id: str | None) -> dict | None:
    """Return owner-only recorded binding plus any already-disclosed fabric facts."""
    if not viewer_owner_id or supervision["owner_id"] != viewer_owner_id:
        return None
    binding = supervision.get("binding")
    runtime = ({"binding_status": "recorded_unconfirmed",
                **{key: value for key, value in binding.items() if _text(value)}}
               if isinstance(binding, dict) else {})
    observed_instance = _text(node.get("instance_id"))
    recorded_instance = _text(binding.get("runtime_instance_id")) if isinstance(binding, dict) else None
    same_instance = recorded_instance is None or observed_instance == recorded_instance
    if (observed_instance and same_instance and isinstance(node.get("fabric"), dict)):
        fabric = node["fabric"]
        runtime.update({key: fabric[key] for key in ("session_id", "status", "host")
                        if _text(fabric.get(key))})
        runtime["binding_status"] = "fabric_observed"
    return runtime or None


def _responsibilities(agent_id: str, tasks: list[dict], include_text: bool) -> list[dict]:
    result = []
    target = "a:" + agent_id
    for task in tasks:
        if not isinstance(task, dict) or task.get("to") != target:
            continue
        task_id, status = _text(task.get("task_id")), _text(task.get("status"))
        if task_id and status:
            item = {"task_id": task_id, "status": status}
            text = _text(task.get("text"))
            if include_text and text:
                item["text"] = text
            result.append(item)
    return result


def _agent(supervision: dict, collaboration: dict, tasks: list[dict],
           viewer_owner_id: str | None) -> dict:
    policy = supervision["policy"]
    owner_view = viewer_owner_id == supervision["owner_id"]
    node = {
        "agent_id": supervision["agent_id"],
        "supervisor_id": supervision["supervisor_id"],
        "name": _text(collaboration.get("name")) or "removed",
        "role": _role(collaboration),
        "status": supervision["status"],
        "health": "unconfirmed" if supervision["status"] == "running" else supervision["status"],
        "responsibilities": _responsibilities(supervision["agent_id"], tasks, owner_view),
        "budget": {"used_tokens": supervision["used_tokens"],
                   "token_limit": policy["token_limit"]},
        "checkpoint": {"progress": supervision["progress"]},
    }
    if owner_view:
        node["checkpoint"]["evidence_ref"] = supervision["evidence_ref"]
    runtime = _runtime(supervision, collaboration, viewer_owner_id)
    if runtime:
        node["runtime"] = runtime
    return node


def project_supervision(supervision, room_flow: dict, *, room_id: str,
                        workspace_id: str | None = None,
                        viewer_owner_id: str | None = None) -> dict:
    """Project supervision jobs visible in one authorized collaboration room.

    ``room_flow`` is the visibility boundary. Agents absent from it are omitted,
    including their IDs and runtime bindings. If a visible agent's recorded
    supervisor is hidden, the agent is reported as unresolved and never promoted
    into a fabricated root.
    """
    if not isinstance(room_flow, dict):
        raise ValueError("authorized room flow must be an object")
    if room_flow.get("room_id") != room_id:
        return {"room_id": room_id, "workspace_id": workspace_id,
                "jobs": [], "unassigned": []}
    visible = {}
    for node in room_flow.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "agent":
            continue
        agent_id = _text(node.get("agent_id"))
        owner_id = _text(node.get("owner_user_id"))
        if agent_id and owner_id and agent_id not in visible:
            visible[agent_id] = node
    records = {node["agent_id"]: node for node in supervision.agents()}
    linked_workspaces = {node.get("workspace_id") for node in records.values()
                         if node.get("room_id") == room_id and node.get("workspace_id")}
    if workspace_id is None:
        if len(linked_workspaces) > 1:
            raise ValueError("collaboration room has ambiguous workspace links")
        workspace_id = next(iter(linked_workspaces), None)
    tasks = [task for task in room_flow.get("tasks", []) if isinstance(task, dict)]
    assigned = set()
    jobs = []
    for root in sorted((node for node in records.values()
                        if node["supervisor_id"] is None
                        and node.get("room_id") == room_id
                        and node.get("workspace_id") == workspace_id),
                       key=lambda node: (node["tab_id"], node["job_id"])):
        collab_root = visible.get(root["agent_id"])
        if not collab_root or collab_root["owner_user_id"] != root["owner_id"]:
            continue
        included = {root["agent_id"]}
        changed = True
        while changed:
            changed = False
            for record in records.values():
                collab = visible.get(record["agent_id"])
                if (record["job_id"] == root["job_id"] and collab
                        and collab["owner_user_id"] == record["owner_id"]
                        and record["supervisor_id"] in included
                        and record["agent_id"] not in included):
                    included.add(record["agent_id"])
                    changed = True
        agents = [_agent(records[agent_id], visible[agent_id], tasks, viewer_owner_id)
                  for agent_id in sorted(included,
                                         key=lambda aid: (aid != root["agent_id"], aid))]
        edges = [{"from": records[aid]["supervisor_id"], "to": aid,
                  "type": "supervisor"}
                 for aid in sorted(included)
                 if records[aid]["supervisor_id"] in included]
        assigned.update(included)
        jobs.append({"job_id": root["job_id"], "tab_id": root["tab_id"],
                     "root_agent_id": root["agent_id"], "agents": agents,
                     "supervision_edges": edges})
    unassigned = []
    for agent_id, collab in sorted(visible.items()):
        if agent_id in assigned:
            continue
        record = records.get(agent_id)
        if (record and record.get("room_id") == room_id
                and record.get("workspace_id") == workspace_id
                and record["owner_id"] == collab["owner_user_id"]
                and record["supervisor_id"] is not None):
            provenance = "supervisor_hidden_or_unavailable"
        else:
            provenance = "not_registered_in_supervision"
        unassigned.append(_unassigned(collab, provenance))
    return deepcopy({"room_id": room_id, "workspace_id": workspace_id,
                     "jobs": jobs, "unassigned": unassigned})


class SupervisionDashboardProvider:
    """Small optional integration surface for the collaboration/API boundary."""

    def __init__(self, supervision):
        self._supervision = supervision

    @property
    def supervision(self):
        """Expose the projection for one-time authoritative service hydration."""
        return self._supervision

    def project(self, room_id: str, flow: dict, viewer_owner_id: str | None,
                workspace_id: str | None = None) -> dict:
        return project_supervision(self._supervision, flow, room_id=room_id,
                                   workspace_id=workspace_id,
                                   viewer_owner_id=viewer_owner_id)

    def link(self, actor, job_id: str, room_id: str, workspace_id: str) -> dict:
        self._supervision.link_job(actor, job_id, room_id, workspace_id)
        return {"job_id": job_id, "room_id": room_id, "workspace_id": workspace_id}

    def links(self) -> list[dict]:
        return self._supervision.job_links()

    def agent_ids(self) -> list[str]:
        return sorted(node["agent_id"] for node in self._supervision.agents())

    def job_agent_ids(self, job_id: str) -> list[str]:
        return self._supervision.job_agent_ids(job_id)
