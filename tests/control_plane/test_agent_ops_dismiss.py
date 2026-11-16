"""Agent Operations: an operator drops an agent from the map by a durable ``session.dismissed`` tombstone."""
import copy
import http.client
import json
import random
import threading

from herdr_engineering.control_plane import agent_ops
from herdr_engineering.control_plane.agent_ops import AgentOpsService, Viewer, fold
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OPERATOR = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}


def ev(n, event_type, *, agent="a1", source="pi", **fields):
    stamp = f"2026-11-12T10:00:{n:02d}.000000Z"
    base = agent_ops._event(event_id=f"e{n}", source=source, sequence=n, event_type=event_type, occurred_at=stamp,
                            ingested_at=stamp, agent_id=agent, visibility_scope="org", provenance="observed")
    base.update(fields)
    return base


def ingest(repo, n, event_type, **fields):
    body = {"schema_version": 2, "event_id": f"in-{n}", "source": "pi", "source_sequence": n,
            "entity_type": "lifecycle", "entity_id": "a1", "event_type": event_type,
            "occurred_at": f"2026-11-12T10:00:{n:02d}Z", "visibility_scope": "org",
            "agent_id": "a1", "provenance": "observed", "payload": {}, **fields}
    IngestService(repo).ingest(body, OPERATOR)


class Server:
    def __init__(self, repo, **kw):
        self.srv = ControlPlaneServer(repo, host="127.0.0.1", port=0,
                                      api_keys={"op-key": "operator", "view-key": "viewer"}, **kw)
        self.thread = threading.Thread(target=self.srv.serve, daemon=True)
        self.thread.start()
        self.srv.wait_until_ready()

    def call(self, method, path, body=None, key="op-key"):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        headers = {"X-API-Key": key, "Content-Type": "application/json"}
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw else None

    def dismiss(self, body, key="op-key"):
        return self.call("POST", "/api/v1/agent-ops/dismiss", body, key)

    def snapshot(self):
        return self.call("GET", "/api/v1/agent-ops/snapshot")[1]

    def stop(self):
        self.srv.shutdown()
        self.thread.join(timeout=5)


def served(repo, enabled=True):
    return Server(repo, agent_ops_config={"enabled": True} if enabled else None)


def two_agents():
    repo = ControlPlaneRepo()
    ingest(repo, 1, "session.started", session_id="s1", host_id="h1")
    ingest(repo, 2, "tool.started", session_id="s2", agent_id="a2", entity_id="a2")
    return repo


# ----------------------------------------------------------------------------------------------------- fold
def test_dismissed_agent_is_absent_and_a_newer_event_revives_it():
    events = [ev(1, "session.started", session_id="s1"), ev(2, "tool.started", agent="a2", session_id="s2"),
              ev(3, "session.dismissed", session_id="s1")]
    snap = fold(events)
    assert [a["node_id"] for a in snap["agents"]] == ["s2"] and snap["counts"]["agents"] == 1
    revived = fold(events + [ev(4, "tool.started", session_id="s1")])
    assert [a["node_id"] for a in revived["agents"]] == ["s1", "s2"]


def test_dismissal_drops_edges_rooms_and_task_links_of_the_agent():
    events = [ev(1, "session.started", session_id="s1", room_id="r1", visibility_scope="room"),
              ev(2, "session.started", agent="a2", session_id="s2", room_id="r1", visibility_scope="room"),
              ev(3, "delegation.proposed", session_id="s1", task_id="t1", payload={"target_session_id": "s2",
                                                                                  "target_agent_id": "a2"}),
              ev(4, "session.dismissed", agent="a2", session_id="s2")]
    snap = fold(events)
    assert [a["node_id"] for a in snap["agents"]] == ["s1"]
    assert snap["edges"] == [] and snap["rooms"][0]["agent_ids"] == ["s1"]
    assert snap["counts"]["edges"] == 0


def test_agent_only_dismissal_drops_every_session_of_the_agent():
    events = [ev(1, "session.started", session_id="s1"), ev(2, "session.started", session_id="s2"),
              ev(3, "session.started", agent="a2", session_id="s3"), ev(4, "session.dismissed", agent=None)]
    assert [a["node_id"] for a in fold(events)["agents"]] == ["s1", "s2", "s3"]   # no target: nothing to match
    events[-1] = ev(4, "session.dismissed", agent="a1")
    assert [a["node_id"] for a in fold(events)["agents"]] == ["s3"]


def test_tombstone_never_creates_a_node_and_fold_stays_order_independent():
    events = [ev(1, "session.started", session_id="s1"), ev(2, "session.dismissed", session_id="s1"),
              ev(3, "tool.started", session_id="s1"), ev(4, "session.dismissed", agent="ghost")]
    expected = fold(events)
    assert [a["node_id"] for a in expected["agents"]] == ["s1"]
    for seed in range(20):
        shuffled = events + events[:2]
        random.Random(seed).shuffle(shuffled)
        assert fold(shuffled) == expected
    assert fold(copy.deepcopy(events)) == expected


# ------------------------------------------------------------------------------------------------------ HTTP
def test_viewer_key_cannot_dismiss_and_nothing_is_stored():
    repo = two_agents()
    server = served(repo)
    try:
        assert server.dismiss({"agent_id": "a1"}, key="view-key")[0] == 403
        assert server.dismiss({"agent_id": "a1"}, key="bad")[0] in (401, 403)
        assert [a["node_id"] for a in server.snapshot()["agents"]] == ["s1", "s2"]
    finally:
        server.stop()


def test_operator_dismisses_by_agent_and_by_session_and_replay_keeps_the_tombstone():
    repo = two_agents()
    server = served(repo)
    try:
        status, out = server.dismiss({"agent_id": "a1", "reason": "stale"})
        assert status == 200 and out["dismissed"] == "a1" and out["cursor"]
        assert [a["node_id"] for a in server.snapshot()["agents"]] == ["s2"]
        status, out = server.dismiss({"session_id": "s2"})
        assert status == 200 and out["dismissed"] == "s2"
        assert server.snapshot()["agents"] == []
        _, page = server.call("GET", "/api/v1/agent-ops/events?limit=100")
        kinds = [e["event_type"] for e in page["events"]]
        assert kinds.count("session.dismissed") == 2 and "session.started" in kinds
        tomb = [e for e in page["events"] if e["event_type"] == "session.dismissed"][0]
        assert tomb["provenance"] == "observed" and tomb["source"] == "operator" and "stale" not in json.dumps(page)
        _, replay = server.call("GET", f"/api/v1/agent-ops/replay?until={page['events'][1]['cursor']}")
        assert [a["node_id"] for a in replay["agents"]] == ["s1", "s2"]      # before the tombstones
        assert out["cursor"] == page["events"][-1]["cursor"]
    finally:
        server.stop()


def test_a_newer_event_from_the_dismissed_agent_revives_it_over_http():
    repo = two_agents()
    server = served(repo)
    try:
        assert server.dismiss({"agent_id": "a2"})[0] == 200
        assert [a["node_id"] for a in server.snapshot()["agents"]] == ["s1"]
        IngestService(repo).ingest({"schema_version": 2, "event_id": "later", "source": "pi", "source_sequence": 50,
                                    "entity_type": "lifecycle", "entity_id": "a2", "event_type": "tool.ended",
                                    "visibility_scope": "org", "agent_id": "a2", "session_id": "s2",
                                    "provenance": "observed", "payload": {}}, OPERATOR)
        assert [a["node_id"] for a in server.snapshot()["agents"]] == ["s1", "s2"]
    finally:
        server.stop()


def test_dismissal_survives_a_restart_from_exported_state():
    repo = two_agents()
    server = served(repo)
    try:
        assert server.dismiss({"agent_id": "a1"})[0] == 200
    finally:
        server.stop()
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    snap = AgentOpsService(restored).snapshot(Viewer(operator=True))
    assert [a["node_id"] for a in snap["agents"]] == ["s2"]


def test_unknown_agent_session_and_people_are_404():
    server = served(two_agents())
    try:
        assert server.dismiss({"agent_id": "nobody"})[0] == 404
        assert server.dismiss({"session_id": "nope"})[0] == 404
        assert server.dismiss({"agent_id": "a1", "session_id": "s2"})[0] == 404      # not that agent's session
        assert server.dismiss({"agent_id": "user:alice"})[0] == 404
        assert len(server.snapshot()["agents"]) == 2
    finally:
        server.stop()


def test_dismissal_keeps_the_visibility_of_the_agent_it_hides():
    repo = ControlPlaneRepo()
    ingest(repo, 1, "session.started", session_id="s1", visibility_scope="private")
    server = served(repo)
    try:
        assert server.dismiss({"agent_id": "a1"})[0] == 200
        service = AgentOpsService(repo)
        assert service.events(Viewer())["events"] == []                   # private tombstone: not leaked to org readers
        assert service.snapshot(Viewer(operator=True))["agents"] == []
    finally:
        server.stop()


def test_validation_errors_are_400():
    server = served(two_agents())
    try:
        for body in ({}, {"reason": "x"}, {"agent_id": ""}, {"agent_id": 5}, {"session_id": ["s1"]},
                     {"agent_id": "a b/c"}, {"agent_id": "a" * 161}, {"agent_id": "a1", "reason": 3},
                     {"agent_id": "a1", "reason": "r" * 201}, [], "a1"):
            assert server.dismiss(body)[0] == 400, body
        assert server.call("POST", "/api/v1/agent-ops/dismiss")[0] == 400       # no body at all
        assert server.call("POST", "/api/v1/agent-ops/other", {"agent_id": "a1"})[0] == 404
        assert len(server.snapshot()["agents"]) == 2
    finally:
        server.stop()


def test_disabled_capability_is_the_same_404():
    server = served(two_agents(), enabled=False)
    try:
        assert server.dismiss({"agent_id": "a1"})[0] == 404
        assert server.dismiss({"agent_id": "a1"}, key="view-key")[0] == 403
    finally:
        server.stop()


# ------------------------------------------------------------------------------------------------------- CLI
def run_cli(server, *args, key="op-key"):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update({"HERDR_CP_URL": f"http://127.0.0.1:{server.srv.port}", "HERDR_CP_API_KEY": key})
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "agent-ops", *args], env=env,
                          capture_output=True, text=True, timeout=30)


def test_cli_dismisses_by_agent_and_by_session_and_reports_failures():
    server = served(two_agents())
    try:
        done = run_cli(server, "dismiss", "a1", "--reason", "stale")
        assert done.returncode == 0 and "dismissed a1" in done.stdout
        done = run_cli(server, "dismiss", "--session", "s2")
        assert done.returncode == 0 and "dismissed s2" in done.stdout
        assert server.snapshot()["agents"] == []
        missing = run_cli(server, "dismiss", "nobody")
        assert missing.returncode == 1 and "404" in missing.stderr
        denied = run_cli(server, "dismiss", "a1", key="view-key")
        assert denied.returncode == 1 and "403" in denied.stderr
        assert run_cli(server, "dismiss").returncode == 2                      # neither an agent nor --session
    finally:
        server.stop()
