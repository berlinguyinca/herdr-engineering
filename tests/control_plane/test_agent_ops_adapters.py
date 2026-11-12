"""Provider adapters -> real ingest -> /api/v1/agent-ops/snapshot. Real in-process control plane, real node subprocesses."""
import http.client
import json
import os
import shutil
import subprocess
import textwrap
import threading
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

ADAPTERS = Path(__file__).resolve().parents[2] / "deploy" / "agent-ops-adapters"
EMIT = Path(__file__).resolve().parents[2] / "deploy" / "herdr-cp-feed-plugin" / "emit.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node required")
KEYS = {"op-key": "operator", "ing-key": "ingest", "view-key": "viewer"}


class Plane:
    def __init__(self, keys=KEYS):
        self.repo = ControlPlaneRepo()
        self.srv = ControlPlaneServer(self.repo, host="127.0.0.1", port=0, api_keys=keys,
                                      agent_ops_config={"enabled": True})
        threading.Thread(target=self.srv.serve, daemon=True).start()
        self.srv.wait_until_ready()
        self.url = f"http://127.0.0.1:{self.srv.port}"

    def get(self, path, key="op-key"):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        conn.request("GET", path, headers={"X-API-Key": key})
        resp = conn.getresponse()
        body = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, body

    def agents(self, key="op-key"):
        status, snap = self.get("/api/v1/agent-ops/snapshot", key)
        assert status == 200
        return {a["session_id"]: a for a in snap["agents"]}

    def events(self):
        return self.get("/api/v1/agent-ops/events?limit=500")[1]["events"]


@pytest.fixture
def plane():
    p = Plane()
    yield p
    p.srv.shutdown()


def env_for(plane, **extra):
    base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "HERDR_AGENT_OPS": "1",
            "HERDR_AGENT_OPS_URL": plane.url, "HERDR_AGENT_OPS_API_KEY": "ing-key",
            "HERDR_AGENT_OPS_SCOPE": "org", "HOSTNAME": "host-1"}
    base.update(extra)
    return base


def node(args, env, stdin=None, timeout=20):
    return subprocess.run(["node", *args], input=stdin, env=env, capture_output=True, text=True, timeout=timeout)


def node_script(source, env, timeout=20):
    return node(["--input-type=module", "-e", textwrap.dedent(source)], env, timeout=timeout)


# ------------------------------------------------------------------------------------------------ Claude
def claude_hook(plane, payload, **extra):
    r = node([str(ADAPTERS / "claude.js")], env_for(plane, **extra), stdin=json.dumps(payload))
    assert r.returncode == 0, r.stderr


def test_claude_hooks_project_lifecycle_tool_compaction_with_unknown_model_and_context(plane):
    sid = "claude-sess-1"
    claude_hook(plane, {"hook_event_name": "SessionStart", "session_id": sid, "transcript_path": "/Users/x/t.jsonl",
                        "cwd": "/Users/x/proj"})
    claude_hook(plane, {"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": "Bash",
                        "tool_input": {"command": "cat /etc/secret --token=abc"}, "tool_response": "TOPSECRETOUT"})
    claude_hook(plane, {"hook_event_name": "PreCompact", "session_id": sid})
    claude_hook(plane, {"hook_event_name": "SubagentStart", "session_id": sid, "agent_id": "sub1"})
    agents = plane.agents()
    main = agents[sid]
    assert main["provenance"] == "observed" and main["host_id"] == "host-1"
    assert main["model"] is None and main["context"] is None
    assert main["compactions"] == 1 and main["state"] == "idle" and main["label"] == "Bash"
    assert agents[f"{sid}:sub:sub1"]["parent_session_id"] == sid
    blob = json.dumps(plane.events())
    for leak in ("TOPSECRETOUT", "/Users/x", "/etc/secret", "--token", "transcript"):
        assert leak not in blob


def test_claude_model_only_when_payload_has_one(plane):
    claude_hook(plane, {"hook_event_name": "SessionStart", "session_id": "c2", "model": "claude-opus-4"})
    assert plane.agents()["c2"]["model"] == "claude-opus-4"


def test_duplicate_delivery_dedupes(plane):
    payload = {"hook_event_name": "SessionStart", "session_id": "c3", "timestamp": "2026-11-12T10:00:00Z"}
    for _ in range(2):
        claude_hook(plane, payload)
    assert len([e for e in plane.events() if e["session_id"] == "c3"]) == 1


def test_disabled_unless_configured_and_fail_open(plane):
    payload = {"hook_event_name": "SessionStart", "session_id": "c4"}
    off = env_for(plane)
    del off["HERDR_AGENT_OPS"]
    assert node([str(ADAPTERS / "claude.js")], off, stdin=json.dumps(payload)).returncode == 0
    assert "c4" not in plane.agents()
    dead = env_for(plane, HERDR_AGENT_OPS_URL="http://127.0.0.1:1")
    assert node([str(ADAPTERS / "claude.js")], dead, stdin=json.dumps(payload)).returncode == 0
    assert node([str(ADAPTERS / "claude.js")], env_for(plane), stdin="not json{").returncode == 0


def test_private_scope_is_the_default_and_hidden_from_viewers(plane):
    env = env_for(plane)
    del env["HERDR_AGENT_OPS_SCOPE"]
    node([str(ADAPTERS / "claude.js")], env, stdin=json.dumps({"hook_event_name": "SessionStart", "session_id": "c5"}))
    assert "c5" not in plane.agents("view-key") and "c5" in plane.agents("op-key")


# --------------------------------------------------------------------------------------------------- Pi
PI_DRIVER = """
import { createPiAdapter } from "%s/pi.js";
const handlers = {};
const pi = { on: (name, fn) => { handlers[name] = fn; } };
const adapter = createPiAdapter(pi, { sessionIdOf: () => "pi-sess-1" });
let usage = { tokens: 1200, contextWindow: 200000 };
const ctx = { getContextUsage: () => usage };
handlers.session_start({}, ctx);
handlers.model_select({ model: { id: "claude-sonnet", provider: "anthropic" } }, ctx);
handlers.tool_execution_start({ toolName: "bash" }, ctx);
handlers.tool_execution_end({ toolName: "bash", isError: false }, ctx);
handlers.message_end({}, ctx);
%s
await adapter.sink.flush();
console.log(JSON.stringify(adapter.sink.stats));
"""


def run_pi(plane, tail=""):
    r = node_script(PI_DRIVER % (ADAPTERS, tail), env_for(plane))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_pi_hooks_emit_session_model_tool_and_measured_context(plane):
    stats = run_pi(plane)
    assert stats["failed"] == 0
    pi = plane.agents()["pi-sess-1"]
    assert pi["model"] == "anthropic/claude-sonnet" and pi["provenance"] == "observed" and pi["state"] == "idle"
    assert pi["context"] == {"used": 1200, "limit": 200000, "source": "pi"}


def test_pi_null_context_after_compaction_emits_compaction_not_a_metric(plane):
    run_pi(plane, "usage = null; handlers.message_end({}, ctx);")
    pi = plane.agents()["pi-sess-1"]
    assert pi["compactions"] == 1
    kinds = [e["event_type"] for e in plane.events()]
    assert kinds.count("context.metric") == 1


def test_pi_never_invents_context_when_usage_unknown(plane):
    r = node_script("""
        import { createPiAdapter } from "%s/pi.js";
        const h = {}; const a = createPiAdapter({ on: (n, f) => { h[n] = f; } }, { sessionIdOf: () => "pi-2" });
        const ctx = { getContextUsage: () => null };
        h.session_start({}, ctx); h.message_end({}, ctx);
        await a.sink.flush();
    """ % ADAPTERS, env_for(plane))
    assert r.returncode == 0, r.stderr
    agent = plane.agents()["pi-2"]
    assert agent["context"] is None and agent["compactions"] == 0 and agent["model"] is None


def test_pi_handler_failures_and_dead_control_plane_never_raise_into_the_host():
    r = node_script("""
        import { createPiAdapter } from "%s/pi.js";
        const h = {}; const a = createPiAdapter({ on: (n, f) => { h[n] = f; } }, { sessionIdOf: () => { throw new Error("boom"); } });
        h.session_start({}, { getContextUsage: () => { throw new Error("x"); } });
        h.message_end(undefined, { getContextUsage: () => { throw new Error("x"); } });
        h.tool_execution_start(null, null);
        await a.sink.flush();
        console.log("host-ok", a.sink.stats.sent);
    """ % ADAPTERS, {"PATH": os.environ["PATH"], "HERDR_AGENT_OPS": "1", "HERDR_AGENT_OPS_URL": "http://127.0.0.1:1"})
    assert r.returncode == 0 and "host-ok 0" in r.stdout


def test_pi_inert_without_config():
    r = node_script("""
        import { createPiAdapter } from "%s/pi.js";
        console.log(createPiAdapter({ on() { throw new Error("must not register"); } }));
    """ % ADAPTERS, {"PATH": os.environ["PATH"]})
    assert r.stdout.strip() == "null"


def test_sink_queue_is_bounded():
    r = node_script("""
        import { createSink } from "%s/core.js";
        const s = createSink({ url: "http://127.0.0.1:1", timeoutMs: 100 }, { maxQueue: 3 });
        for (let i = 0; i < 10; i++) s.enqueue({ i });
        console.log(s.size, s.stats.dropped);
    """ % ADAPTERS, {"PATH": os.environ["PATH"]})
    assert r.stdout.split() == ["3", "7"]


# ------------------------------------------------------------------------------------------------- Codex
def synthetic_rollout(path):
    """Real rollout shape ({timestamp, type, payload}); content fields hold sentinels that must never be forwarded."""
    rows = [
        {"timestamp": "2026-11-12T10:00:00.000Z", "type": "session_meta", "ordinal": 0,
         "payload": {"id": "codex-sess-1", "context_window": 272000, "model_provider": "openai",
                     "cwd": "/Users/x/proj", "base_instructions": "SECRET-INSTRUCTIONS"}},
        {"timestamp": "2026-11-12T10:00:01.000Z", "type": "turn_context", "ordinal": 1,
         "payload": {"model": "gpt-5-codex", "cwd": "/Users/x/proj", "summary": "auto"}},
        {"timestamp": "2026-11-12T10:00:02.000Z", "type": "response_item", "ordinal": 2,
         "payload": {"type": "message", "role": "user", "content": [{"text": "SECRET-PROMPT"}]}},
        {"timestamp": "2026-11-12T10:00:03.000Z", "type": "response_item", "ordinal": 3,
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "call_1",
                     "arguments": "{\"cmd\": \"cat /etc/SECRET-ARG\"}"}},
        {"timestamp": "2026-11-12T10:00:04.000Z", "type": "response_item", "ordinal": 4,
         "payload": {"type": "function_call_output", "call_id": "call_1", "output": "SECRET-OUTPUT"}},
        {"timestamp": "2026-11-12T10:00:05.000Z", "type": "event_msg", "ordinal": 5,
         "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 5000},
                                                     "model_context_window": 272000}}},
        {"timestamp": "2026-11-12T10:00:06.000Z", "type": "event_msg", "ordinal": 6,
         "payload": {"type": "task_complete", "last_agent_message": "SECRET-REPLY"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")


def run_codex(plane, *args, **extra):
    r = node([str(ADAPTERS / "codex.js"), *args], env_for(plane, **extra))
    assert r.returncode == 0, r.stderr


def test_codex_rollout_metadata_only(plane, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    synthetic_rollout(rollout)
    before = rollout.read_bytes()
    run_codex(plane, "rollout", str(rollout))
    run_codex(plane, "rollout", str(rollout))          # re-parse: dedupes
    agent = plane.agents()["codex-sess-1"]
    assert agent["model"] == "gpt-5-codex" and agent["provenance"] == "observed" and agent["provider"] == "codex"
    assert agent["context"] == {"used": 5000, "limit": 272000, "source": "codex-rollout"}
    assert agent["state"] == "idle" and agent["label"] == "exec_command"
    events = plane.events()
    assert [e["event_type"] for e in events if e["session_id"] == "codex-sess-1"].count("tool.started") == 1
    blob = json.dumps(events)
    for leak in ("SECRET", "/Users/x", "/etc"):
        assert leak not in blob
    assert rollout.read_bytes() == before              # read-only


def test_codex_notify_program(plane):
    run_codex(plane, "notify", json.dumps({"type": "agent-turn-complete", "thread-id": "codex-notify-1",
                                           "turn-id": "t1", "last-assistant-message": "SECRET-REPLY"}))
    agent = plane.agents()["codex-notify-1"]
    assert agent["model"] is None and agent["context"] is None and agent["provenance"] == "observed"
    assert "SECRET" not in json.dumps(plane.events())


def test_codex_failures_are_silent(plane, tmp_path):
    run_codex(plane, "rollout", str(tmp_path / "missing.jsonl"))
    run_codex(plane, "notify", "{bad json")


# ------------------------------------------------------------------------------------------------- Herdr
def run_emit(plane, tmp_path, event, event_json, pane="p1", **extra):
    home = tmp_path / "home"
    (home / ".config" / "herdr-engineering").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "herdr-engineering" / "host.json").write_text(
        json.dumps({"control_plane_url": plane.url, "host_id": "host-1", "token": "t"}))
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "HOSTNAME": "host-1", "HERDR_AGENT_OPS": "1",
           "HERDR_AGENT_OPS_SCOPE": "org", "HERDR_PLUGIN_EVENT": event, "HERDR_PANE_ID": pane,
           "HERDR_WORKSPACE_ID": "w1", "HERDR_SESSION_NAME": "srv", "HERDR_PLUGIN_EVENT_JSON": json.dumps(event_json),
           "HERDR_PLUGIN_CONFIG_DIR": str(tmp_path / "cfg"), "HERDR_PLUGIN_STATE_DIR": str(tmp_path / "state"),
           **extra}
    r = subprocess.run(["node", str(EMIT)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.fixture
def open_plane():
    p = Plane(keys=None)
    yield p
    p.srv.shutdown()


def test_herdr_cp_feed_maps_detected_status_model_and_ctx_tokens(open_plane, tmp_path):
    run_emit(open_plane, tmp_path, "pane.agent_detected", {"agent": "pi"})
    run_emit(open_plane, tmp_path, "pane.agent_status_changed",
             {"agent": "pi", "agent_status": "working", "tokens": {"model": "opus", "ctx": "42%"}})
    agent = open_plane.agents(key="")["herdr:host-1:srv:p1"]
    assert agent["provenance"] == "observed" and agent["host_id"] == "host-1" and agent["state"] == "working"
    assert agent["model"] == "opus" and agent["provider"] == "pi"
    assert agent["context"] == {"used": 42, "limit": 100, "source": "herdr-ctx-pct"}


def test_herdr_missing_fields_stay_absent_and_off_by_default(open_plane, tmp_path):
    run_emit(open_plane, tmp_path, "pane.agent_status_changed", {"agent_status": "blocked"}, pane="p2")
    assert "herdr:host-1:srv:p2" not in open_plane.agents(key="")
    run_emit(open_plane, tmp_path, "pane.agent_detected", {}, pane="p3")
    agent = open_plane.agents(key="")["herdr:host-1:srv:p3"]
    assert agent["model"] is None and agent["context"] is None and agent["provider"] is None
    run_emit(open_plane, tmp_path, "pane.agent_detected", {}, pane="p4", HERDR_AGENT_OPS="0")
    assert "herdr:host-1:srv:p4" not in open_plane.agents(key="")
