"""Tree projection of the distributed engineering fleet (spec 0090/0110/0150).

Builds a navigable tree that shows how the fleet relates to workspaces
(agents), what each is doing, the dev-service leases each owns, and the
activity/session events tied to it. The tree is a pure projection over the
live adapters (Herdr snapshot, session journal, dev-service registry, fabric
gateway) plus a light live-process enrichment — it never mutates any of them.

Every node has a stable ``id``. Leaf nodes carry a ``detail``-able id that the
web surface can pass to :func:`node_detail` to render a click-through panel.

The projection is fail-closed: if an adapter throws, that subtree degrades to
an empty list rather than blocking the whole view (mirrors ``web.py``).
"""
from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .journal import SessionJournal


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _fleet_machines() -> list[dict[str, Any]]:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        return [m.to_dict() for m in NativeHerdrAdapter().list_machines()]
    except Exception:
        return []


def _workspaces() -> list[dict[str, Any]]:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        return [w.to_dict() for w in NativeHerdrAdapter().list_workspaces()]
    except Exception:
        return []


def _agents() -> list[dict[str, Any]]:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        return [a.to_dict() for a in NativeHerdrAdapter().list_agents()]
    except Exception:
        return []


def _leases() -> list[dict[str, Any]]:
    from .config import load_config
    from .devfabric import DevServiceRegistry
    from .fabric import FabricClient, gateway_url_from_config
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    try:
        leases = [lease.to_dict() for lease in DevServiceRegistry().active_leases()]
    except Exception:
        leases = []
    # Also pull leases from the live fabric gateway (the authoritative source
    # for dev:<port> services across the fleet). Merge, de-duped by lease id.
    base = gateway_url_from_config(cfg) or os.environ.get("HERDR_ENGINEERING_FABRIC_URL")
    if base:
        try:
            gw = FabricClient(base).list().get("leases") or []
            seen = {lease.get("id") for lease in leases}
            for gl in gw:
                if gl.get("id") not in seen:
                    leases.append(gl)
                    seen.add(gl.get("id"))
        except Exception:
            pass
    url_base = ((cfg.get("fabric") or {}).get("url_base") or "").strip()
    if base:
        try:
            live = FabricClient(base).healthz().get("url_base")
            if live:
                url_base = live
        except Exception:
            pass
    if url_base:
        for lease in leases:
            if lease.get("external_port") is not None:
                lease["url"] = f"http://{url_base}:{lease['external_port']}"
    return leases


def _activity(limit: int = 200) -> list[dict[str, Any]]:
    try:
        return [e.to_dict() for e in SessionJournal().all_recent(limit=limit)]
    except Exception:
        return []


def _detected_harnesses() -> list[dict[str, Any]]:
    """Harnesses herdr has detected on this host (pi, opencode, claude, ...)."""
    path = Path(
        os.environ.get("HERDR_STATE",
                       str(Path.home() / ".local/state/herdr"))
    ) / "agent-detection" / "status.toml"
    try:
        raw = path.read_text()
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("[agents."):
            name = line[len("[agents."):-1]
            out.append({"id": f"harness:{name}", "label": name, "type": "harness"})
    return out


def _live_processes() -> list[dict[str, Any]]:
    """Light enrichment: running engineering processes on this host."""
    import shutil
    out = []
    if not shutil.which("ps"):
        return out
    try:
        rows = subprocess.run(
            ["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True,
            timeout=5).stdout.splitlines()
    except Exception:
        return out
    keywords = ("pi -p", "pi -", "orchestrator/main.py", "herdr-eng",
                "super-ws", "mission", "augur")
    for row in rows[1:]:
        parts = row.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, args = parts[0], parts[1], parts[2]
        if not any(k in args for k in keywords):
            continue
        out.append({"pid": pid, "ppid": ppid, "args": args[:220],
                    "id": f"proc:{pid}", "type": "process"})
    return out


def _machine_node(machine: dict[str, Any]) -> dict[str, Any]:
    mid = machine.get("id") or machine.get("label") or "machine"
    return {
        "id": f"machine:{mid}",
        "label": machine.get("label") or mid,
        "subtitle": machine.get("target") or "",
        "type": "machine",
        "reachable": machine.get("reachable"),
        "children": [],  # workspaces attached below
    }


def _workspace_children(ws: dict[str, Any], leases: list[dict[str, Any]],
                        events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    ws_id = ws.get("workspace_id") or ws.get("label") or ""
    # dev-service lease(s) owned by this workspace (match on label/machine)
    for lease in leases:
        label = (lease.get("label") or "").lower()
        if ws_id.lower() in label or (ws.get("label") or "").lower() in label:
            children.append({
                "id": f"lease:{lease.get('id')}",
                "label": lease.get("label") or lease.get("id"),
                "subtitle": (lease.get("url") or ""),
                "type": "dev",
                "url": lease.get("url"),
            })
    # activity events tied to this workspace
    count = 0
    for ev in events:
        if (ev.get("herdr_session_id") and ws_id in ev.get("herdr_session_id", "")) or \
           (ev.get("worktree_id") and ws_id in ev.get("worktree_id", "")):
            children.append({
                "id": f"evt:{ev.get('id')}",
                "label": ev.get("summary") or ev.get("type", "event"),
                "subtitle": f"{ev.get('occurred_at', '')} · {ev.get('source', '')}",
                "type": "activity",
            })
            count += 1
            if count >= 8:
                break
    return children


def build_tree() -> dict[str, Any]:
    machines = _fleet_machines()
    workspaces = _workspaces()
    leases = _leases()
    events = _activity()
    harnesses = _detected_harnesses()
    procs = _live_processes()

    # Root: fleet of machines
    roots: list[dict[str, Any]] = []
    for m in machines:
        node = _machine_node(m)
        roots.append(node)

    # If herdr exposes no machines, still show a single host node so the tree
    # is never empty.
    if not roots:
        roots = [{"id": "machine:local", "label": "local",
                  "subtitle": "no fleet snapshot", "type": "machine",
                  "children": []}]

    # Attach workspaces (agents) to machines; if there are no machine ids to
    # match, attach all workspaces to the first machine node.
    ws_nodes: list[dict[str, Any]] = []
    for ws in workspaces:
        ws_nodes.append({
            "id": f"ws:{ws.get('workspace_id') or ws.get('label') or 'ws'}",
            "label": ws.get("label") or ws.get("workspace_id") or "workspace",
            "subtitle": _workspace_subtitle(ws),
            "type": "workspace",
            "status": ws.get("agent_status"),
            "children": _workspace_children(ws, leases, events),
        })

    # Attach all dev services and live processes as top-level branches under
    # the machine so every lease/process is a clickable leaf.
    dev_children = [{
        "id": f"lease:{lease.get('id')}",
        "label": lease.get("label") or lease.get("id"),
        "subtitle": lease.get("url") or lease.get("id"),
        "type": "dev",
        "url": lease.get("url"),
    } for lease in leases]
    proc_children = [{
        "id": p["id"],
        "label": (p.get("args") or "")[:80],
        "subtitle": "pid " + (p.get("pid") or ""),
        "type": "process",
    } for p in procs]

    if ws_nodes:
        roots[0]["children"] = list(ws_nodes)  # copy: appends below must not grow ws_nodes
    roots[0]["children"].append({
        "id": "branch:dev", "label": f"Dev services ({len(dev_children)})",
        "type": "branch", "children": dev_children})
    roots[0]["children"].append({
        "id": "branch:procs", "label": f"Live processes ({len(proc_children)})",
        "type": "branch", "children": proc_children})

    return {
        "generated_at": _now_iso(),
        "root": {
            "id": "root",
            "label": "Fleet",
            "type": "root",
            "children": roots,
        },
        "harnesses": harnesses,
        "dev_services": [{
            "id": f"lease:{lease.get('id')}",
            "label": lease.get("label") or lease.get("id"),
            "url": lease.get("url") or "",
            "target": f"{lease.get('target', {}).get('machine_id')}:{lease.get('target', {}).get('port')}"
            if isinstance(lease.get("target"), dict) else "",
            "type": "dev",
        } for lease in leases],
        "processes": procs,
        "counts": {
            "machines": len(roots),
            "workspaces": len(ws_nodes),
            "dev_services": len(leases),
            "harnesses": len(harnesses),
            "processes": len(procs),
        },
    }


def _workspace_subtitle(ws: dict[str, Any]) -> str:
    label = ws.get("label") or ""
    status = ws.get("agent_status") or ""
    parts = [p for p in (status, label) if p]
    return " · ".join(parts) if parts else "workspace"


def node_detail(node_id: str) -> dict[str, Any]:
    """Resolve a clickable leaf/any node id into a detail payload."""
    if not node_id:
        return {"error": "no id"}
    kind, _, rest = node_id.partition(":")

    if kind == "machine":
        for m in _fleet_machines():
            if (m.get("id") or m.get("label")) == rest:
                return {"id": node_id, "kind": "machine", **m}
        return {"id": node_id, "kind": "machine", "error": "not found"}

    if kind == "ws":
        for ws in _workspaces():
            if rest in ((ws.get("workspace_id") or ""), (ws.get("label") or "")):
                events = _activity()
                tied = [e for e in events if
                        (e.get("herdr_session_id") and rest in e.get("herdr_session_id", ""))
                        or (e.get("worktree_id") and rest in e.get("worktree_id", ""))]
                return {
                    "id": node_id, "kind": "workspace", **ws,
                    "activity": tied[:20],
                    "dev": [lease for lease in _leases()
                            if rest.lower() in (lease.get("label") or "").lower()],
                }
        return {"id": node_id, "kind": "workspace", "error": "not found"}

    if kind == "lease":
        for lease in _leases():
            if lease.get("id") == rest:
                return {"id": node_id, "kind": "dev_service", **lease}
        return {"id": node_id, "kind": "dev_service", "error": "not found"}

    if kind == "evt":
        for e in _activity(limit=2000):
            if e.get("id") == rest:
                return {"id": node_id, "kind": "activity", **e}
        return {"id": node_id, "kind": "activity", "error": "not found"}

    if kind == "proc":
        for p in _live_processes():
            if p.get("pid") == rest:
                return {"id": node_id, "kind": "process", **p}
        return {"id": node_id, "kind": "process", "error": "not found"}

    return {"id": node_id, "kind": "unknown", "error": "unrecognised id"}
