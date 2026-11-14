"""Playwright fixture: the REAL control-plane server with Agent Operations enabled, seeded through the real POST /api/v1/ingest.

    python tests/playwright/agent-ops-server.py            # prints PORT=<n> once ready, then serves until killed
    EMPTY=1 ...                                            # enabled but nothing ingested (empty state)
    WORK_CENTER=1 ...                                      # also advertise work_center_v1 (the Overview shows the task-lineage card)
    OFF=1 ...                                              # capability off (the route and the nav entry must stay hidden)

No mocks: events travel through the HTTP ingest endpoint, the projection folds them, and the page reads /api/v1/agent-ops/*.
Events are org-scoped (an API key alone sees no rooms), so the map shows the "No room" cluster.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

T0 = "2026-11-12T10:00:%02dZ"
_n = [0]


def post(base, event_type, agent, **fields):
    _n[0] += 1
    n = _n[0]
    body = {"schema_version": 2, "event_id": f"ao-{n:03d}", "source": "pi", "source_sequence": n, "entity_type": "lifecycle",
            "entity_id": agent, "event_type": event_type, "occurred_at": T0 % min(n, 59), "visibility_scope": "org",
            "agent_id": agent, "provenance": "observed", "payload": {}, **fields}
    req = urllib.request.Request(f"{base}/api/v1/ingest", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def seed(base):
    pl, ca, cb, rv, ts = "planner", "coder-a", "coder-b", "reviewer", "tester"
    post(base, "session.started", pl, session_id="s-planner", host_id="mac-studio", model_id="claude-opus-5", sanitized_label="Planner")
    post(base, "context.metric", pl, session_id="s-planner", payload={"context_used": 62000, "context_limit": 200000, "context_source": "pi"})
    post(base, "session.started", ca, session_id="s-coder-a", host_id="mac-studio", model_id="claude-sonnet-5", sanitized_label="Coder A")
    post(base, "context.metric", ca, session_id="s-coder-a", payload={"context_used": 183000, "context_limit": 200000, "context_source": "pi"})
    post(base, "tool.started", ca, session_id="s-coder-a")
    post(base, "session.started", cb, session_id="s-coder-b", host_id="farm-gpu-01", model_id="gpt-5.5", sanitized_label="Coder B", payload={"provider": "openai"})
    post(base, "context.metric", cb, session_id="s-coder-b", payload={"context_used": 148000, "context_limit": 200000, "context_source": "codex"})
    post(base, "session.started", rv, session_id="s-reviewer", host_id="farm-gpu-01", model_id="claude-opus-5", sanitized_label="Reviewer")
    post(base, "session.started", ts, session_id="s-tester", host_id="mac-studio", sanitized_label="Tester")
    post(base, "tool.started", pl, session_id="s-planner", task_id="task-42")
    post(base, "delegation.proposed", pl, session_id="s-planner", task_id="task-42", payload={"target_session_id": "s-coder-a"})
    post(base, "delegation.accepted", pl, session_id="s-planner", task_id="task-42", payload={"target_session_id": "s-coder-a"})
    post(base, "delegation.proposed", pl, session_id="s-planner", task_id="task-43", payload={"target_session_id": "s-coder-b"})
    post(base, "message.sent", ca, session_id="s-coder-a", payload={"target_session_id": "s-coder-b"})
    post(base, "message.delivered", cb, session_id="s-coder-b", payload={"target_session_id": "s-coder-a"})
    post(base, "review.requested", ca, session_id="s-coder-a", task_id="task-42", payload={"target_session_id": "s-reviewer"})
    post(base, "handoff.state", cb, session_id="s-coder-b", handoff_id="ho-7", status="queued", payload={"target_session_id": "s-tester"})
    post(base, "owner.transferred", pl, session_id="s-planner", task_id="task-42", payload={"new_owner_agent_id": "planner"})
    post(base, "tests.completed", ts, session_id="s-tester", task_id="task-42")
    post(base, "model.switched", ca, session_id="s-coder-a", model_id="claude-opus-5")
    post(base, "tool.failed", cb, session_id="s-coder-b")


def main():
    repo = ControlPlaneRepo()
    cfg = None if os.environ.get("OFF") == "1" else {"enabled": True}
    srv = ControlPlaneServer(repo, host="127.0.0.1", port=0, web_root=_REPO / "packages", agent_ops_config=cfg,
                              work_center_config={"enabled": True} if os.environ.get("WORK_CENTER") == "1" else None)
    import threading
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    base = f"http://127.0.0.1:{srv.port}"
    if os.environ.get("EMPTY") != "1" and cfg:
        seed(base)
    print(f"PORT={srv.port}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
