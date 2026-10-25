"""T16 UI gates: the REAL integrated control plane on loopback with a seeded collaboration room.

Unlike the mocked-API fixtures, this serves the assembled collab page (collab.html + modules) from the real public
listener, with its real Content-Security-Policy, backed by a real CollabService, graph projection, ingest path and
workbench / contextual / insights services. Nothing is configured through the environment: the argument is a JSON
mapping on argv (all of it optional) and every credential is generated at start-up.

    python tests/playwright/real_server.py '{"graph_nodes": 0}'

Prints one JSON line on stdout once ready: {"base": ..., "ingest": ..., "tokens": {...}, "room": ..., ...} and then
serves until it is killed. Tokens are random and live only in this process and the test that started it.
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import secrets
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.collab import (  # noqa: E402
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
    _digest,
)
from herdr_engineering.control_plane.collab_attention import normalize_insights  # noqa: E402
from herdr_engineering.control_plane.collab_context import normalize_contextual  # noqa: E402
from herdr_engineering.control_plane.collab_workbench import normalize_steering  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

HOSTILE = '<img src=x onerror="window.__xss=1">"onmouseover="window.__xss=2"><script>window.__xss=3</script>'
T0 = "2026-10-22T10:00:00Z"
FIXED = 1792749600.0        # 2026-10-23T10:00:00Z: the clock of the deterministic (screenshot) server; token expiry is relative to it


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def make_jwt(now: float | None = None) -> str:
    """JWT-shaped, unsigned, random: the pilot authenticator compares a digest; the page only reads `exp`."""
    # With a fixed clock (the screenshot server) the page still compares `exp` with the REAL time when it loads, so a day past the fixed
    # instant is a time bomb: the baseline check failed the first time it ran more than 24 hours after FIXED. Fifty years, then.
    life = 50 * 365 * 86400 if now else 86400
    return f"{_b64({'alg': 'none'})}.{_b64({'exp': int((now or time.time())) + life, 'n': secrets.token_hex(8)})}.{secrets.token_hex(16)}"


class GateAuthenticator(StaticTokenAuthenticator):
    """The pilot token authenticator plus the one extra call the page makes at sign-in (id_token -> verified email).
    The Cognito sign-in redirect itself is NOT exercised by the UI gates (documented gap)."""

    def verify_id_token(self, token, user=None):
        found = self.user_for(token)
        return f"{found}@ui-gates.invalid" if found and found == user else None


class Seeder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.fixed = bool(cfg.get("fixed_clock"))
        self.tokens = {n: make_jwt(FIXED if self.fixed else None) for n in ("alice", "bob", "carol")}
        self.ingest_key = "ik-" + secrets.token_hex(12)
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = CollabService(GateAuthenticator({u: _digest(t) for u, t in self.tokens.items()}),
                                 sink=FileEventLog(str(Path(self.tmp.name) / "log")), max_agents_per_user=20,
                                 max_log_events=2_000_000, delegation_enabled=True,
                                 now=(lambda: FIXED) if self.fixed else None,
                                 public_config={"public_url": "http://127.0.0.1:0/", "client_id": "ui-gates-client", "hosted_ui_domain": "auth.ui-gates.invalid"})
        self.svc.replay_history = "full"
        self.svc.steering = normalize_steering({"enabled": True, "actions": ["pause", "resume", "stop_after_step", "redirect", "approve",
                                                                            "reject", "fork", "promote"],
                                                "forks": {"enabled": True, "require_validation": False}})
        self.svc.contextual = normalize_contextual({"enabled": True})
        self.svc.insights = normalize_insights({"enabled": True, **({"min_group_size": 2} if cfg.get("rich") else {})})
        pr = {n: self.svc.authenticate(f"Bearer {t}", None) for n, t in self.tokens.items()}
        self.pr = pr
        proj = self.svc.create_project(pr["alice"], {"name": "T16 gates"})["project_id"]
        self.room = self.svc.create_room(pr["alice"], proj, {"name": "Review room"})["room_id"]
        self.room2 = self.svc.create_room(pr["alice"], proj, {"name": "Second room"})["room_id"]
        self.svc.add_member(pr["alice"], self.room, {"user_id": "bob", "role": "contributor"})
        self.svc.add_member(pr["alice"], self.room, {"user_id": "carol", "role": "viewer"})
        self.svc.add_member(pr["alice"], self.room2, {"user_id": "bob", "role": "viewer"})
        scope = {self.ingest_key: {"rooms": [self.room, self.room2], "missions": []}}
        # the deterministic server also stamps every ingest with a stepping fixed clock (the gantt axis is drawn from these stamps)
        ticks = itertools.count()
        self.repo = ControlPlaneRepo(now=(lambda: FIXED - 600 + next(ticks)) if self.fixed else None)
        self.srv = ControlPlaneServer(self.repo, host="127.0.0.1", port=0, collab=self.svc,
                                      api_keys={self.ingest_key: "ingest"}, lineage_key_scopes=scope,
                                      public_port=0, public_host="127.0.0.1")
        self.thread = threading.Thread(target=self.srv.serve, daemon=True)
        self.thread.start()
        self.srv.wait_until_ready()
        self.base = f"http://127.0.0.1:{self.srv.public_port}"
        self.svc.public_config["public_url"] = self.base + "/"
        self.n = 0
        import hashlib
        self.principal = {"kind": "ingest", "id": "key:" + hashlib.sha256(self.ingest_key.encode()).hexdigest()[:12],
                          "authenticated_by": "api_key"}

    # ------------------------------------------------------------------ lineage through the real ingest service
    def send(self, event_type, payload, room=None):
        self.n += 1
        body = {"schema_version": 2, "event_id": f"seed-{self.n}", "source": "seed", "source_sequence": self.n,
                "entity_type": "lineage", "entity_id": payload.get("node_id") or payload.get("from_id"),
                "event_type": event_type, "payload": payload, "visibility_scope": "room",
                "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1792749600 - 600 + self.n)), "room_id": room or self.room}
        return self.srv.ingest.ingest(body, self.principal)

    def node(self, nid, state=None, label=None, attrs=None, room=None):
        p = {"node_type": nid.split(":")[0], "node_id": nid, "label": label or nid.split(":", 1)[1]}
        if state:
            p["state"] = state
        if attrs:
            p["attrs"] = attrs
        return self.send("node.upsert", p, room)

    def edge(self, kind, a, b, attrs=None, room=None):
        p = {"edge_type": kind, "from_id": a, "to_id": b}
        if attrs:
            p["attrs"] = attrs
        return self.send("edge.add", p, room)

    def seed(self):
        s, a, b = self.svc, self.pr["alice"], self.pr["bob"]
        self.node("agent:lead", "running", "Lead agent")
        self.node("agent:helper", "done", HOSTILE)
        self.node("attempt:a1", "running", "Attempt one", {"head_sha": "1" * 40})
        self.node("attempt:a2", "failed", "Attempt two")
        self.node("task:parse", "running", "Parser task")
        self.node("gate:g1", "pending", "Plan gate", {"plan_digest": "d1"})
        self.node("pr:github.com/o/r#1", "open", "PR one", {"head": "c1"})
        self.node("review:r1", "done", "Review one")
        self.node("approach:alt1", "running", "Alternative one", {"base_sha": "1" * 40, "head_sha": "2" * 40, "branch": "alt/one",
                                                                "files_changed": 2, "insertions": 5, "deletions": 1,
                                                                "touched_paths": ["src/parser/a.py", "src/parser/b.py"]})
        self.edge("fork", "attempt:a1", "approach:alt1")
        self.edge("spawn", "agent:lead", "agent:helper")
        self.edge("spawn", "agent:lead", "attempt:a1")
        self.edge("spawn", "agent:helper", "attempt:a2")
        self.edge("dependency", "task:parse", "attempt:a1")
        self.edge("reviewed", "review:r1", "pr:github.com/o/r#1", {"commit": "c0"})
        self.edge("reviewed", "review:r1", "pr:github.com/o/r#1", {"commit": "c1"})      # the PR head moved: the c0 review is now stale
        self.send("node.state", {"node_id": "attempt:a2", "state": "failed", "reason": "boom"})
        # comments, including a hostile one
        s.add_comment(b, self.room, {"anchor": {"kind": "node", "node_id": "attempt:a1"}, "text": "why did this retry?",
                                     "idempotency_key": "seed-c1"})
        s.add_comment(a, self.room, {"anchor": {"kind": "node", "node_id": "attempt:a1"}, "text": HOSTILE,
                                     "idempotency_key": "seed-c2"})
        # a pending steering proposal by bob that alice can confirm
        t = s.steering_target(b, self.room, "attempt:a1")
        self.steer = s.propose_steering(b, self.room, {"action": "pause", "node_id": "attempt:a1",
                                                       "expected_fence": t["fence"], "idempotency_key": "seed-s1"})[1]["steering"]
        # workspace link (T07)
        s.register_host(self._admin(), "bender", {"owners": ["alice"]})
        s.link_workspace(a, self.room, {"host": "bender", "workspace_id": "ws-1", "is_primary": True})
        # an external agent (T10) invited for proposals, with a bounded task and a quarantined proposal (T11)
        inv = s.create_external_invite(a, self.room, {"label": "Dana's Claude", "proposals": True, "actions": ["read", "post"], "lineage": True})
        out = s.redeem_join_token({"token": inv["code"]})
        agent = s.authenticate(f"Agent {out['credential']}", out["device_id"])
        self.agent_principal = agent
        self.agent_id = agent.id
        tid = s.create_delegation(a, self.room, {
            "target_agent_id": agent.id, "text": "Review the parser and propose a fix",
            "scope": {"paths": ["src/parser/"], "nodes": [], "questions": [{"id": "q1", "text": "why does it crash?"}]},
            "budget": {"max_proposals": 3, "max_steps": 6, "max_artifact_bytes": 4096, "max_tokens": 1000},
            "expires_in_seconds": 3600, "output_kinds": ["patch", "doc", "analysis", "link"]})["task_id"]
        auth, dev = f"Agent {out['credential']}", out["device_id"]

        def inbox_version():
            return next(d["version"] for d in s.delegations_inbox(agent)["delegations"] if d["task_id"] == tid)
        s.delegation_respond(agent, tid, {"action": "accept", "expected_version": inbox_version()})
        s.submit_proposal(agent, tid, {"kind": "doc", "title": "Parser notes <b>x</b>", "content": "notes\n" + HOSTILE,
                                       "expected_version": inbox_version()})
        self.agent_auth = (auth, dev)
        if self.cfg.get("mention_agent"):
            # an agent that may be handed a task: it is what the composer offers when a person types "@"
            out = s.connect_agent(a, {"harness_type": "claude-code", "name": "helper-bot"})
            s.grant_agent(a, self.room, {"agent_id": out["agent_id"], "actions": ["read", "post", "execute"]})
        if self.cfg.get("rich"):
            self.rich()
        if self.cfg.get("long_labels"):
            self.long_labels()
        return self

    def rich(self):
        """Text in every field the page can render: a redirect with an instruction, a consultation question, a second
        workspace link. Used by the hostile-text gate, which taints each of them."""
        s, a, b = self.svc, self.pr["alice"], self.pr["bob"]
        t = s.steering_target(b, self.room, "attempt:a1")
        s.propose_steering(b, self.room, {"action": "redirect", "node_id": "attempt:a1", "instruction": "prefer the smaller patch",
                                          "expected_fence": t["fence"], "idempotency_key": "seed-rich-s"})
        s.ask_consultation(a, self.room, {"target": {"kind": "user", "id": "bob"}, "subject": {"kind": "node", "node_id": "attempt:a1"},
                                          "question": "which approach is safer for the parser?", "idempotency_key": "seed-rich-q"})
        s.external_heartbeat(self.agent_principal, {"status": "working", "activity": "reading parser.py"})
        # lineage the external agent reports about itself: shown under its tab ("Lineage reported by <label>")
        s.external_lineage(self.agent_principal, {"event_id": "seed-rich-l1", "source_sequence": 1, "event_type": "node.upsert",
                                                  "payload": {"node_type": "agent", "node_id": f"agent:{self.agent_id}.worker",
                                                              "label": "Reported worker", "state": "running"}})

        # round 3: put text in the fields the small room never showed. Each is one renderer the hostile-text gate must now reach.
        # a task that finished and was reopened, and one that finished: two lanes are enough for the analytics table (min_group_size 2),
        # and a table row is where the server's "basis" sentence is printed
        for tid in ("task:rw1", "task:rw2"):
            self.node(tid, "running", f"Rework {tid[-1]}")
            self.send("node.state", {"node_id": tid, "state": "done"})
        self.send("node.state", {"node_id": "task:rw1", "state": "running", "reason": "needs rework"})
        # an agent that reports itself as external but is not on the room's roster: a "virtual tab" naming `reported_by`
        self.node("agent:virt", "running", "Virtual agent", {"external": True, "reported_by": "remote-harness"})
        # an agent asking to join: its request names the owner's e-mail address
        out = s.connect_agent(a, {"harness_type": "claude-code", "name": "asker-bot"})
        with s._lock:
            s._file_request(a.id, self.room, out["agent_id"], ["read", "post"])

    def long_labels(self):
        """Unbroken 300-character text in every place a person can type, so the 320px reflow gate meets what real users paste
        (a URL, a hash, a path). Nothing here can wrap at a space."""
        word = "x" * 300
        self.node("task:long", "running", "L" + word)
        self.edge("dependency", "task:long", "attempt:a1")
        self.edge("dependency", "attempt:a1", "task:long")
        self.svc.add_comment(self.pr["bob"], self.room, {"anchor": {"kind": "node", "node_id": "attempt:a1"}, "text": "C" + word,
                                                         "idempotency_key": "seed-long-c"})
        t = self.svc.steering_target(self.pr["bob"], self.room, "task:long")
        self.svc.propose_steering(self.pr["bob"], self.room, {"action": "redirect", "node_id": "task:long", "instruction": "R" + word,
                                                              "expected_fence": t["fence"], "idempotency_key": "seed-long-s"})

    def close(self) -> None:
        """Remove everything this process wrote (the event log's temp directory)."""
        try:
            self.srv.shutdown()
        except Exception:      # noqa: BLE001 - best effort on the way out
            pass
        self.tmp.cleanup()

    def _admin(self):
        p = self.svc.authenticate(f"Bearer {self.tokens['alice']}", None)
        p.admin = True
        return p


def bulk_graph(seed: Seeder, n: int) -> float:
    """Add `n` more nodes (and a spine of edges) through the real ingest service. Returns seconds taken."""
    t0 = time.time()
    prev = "agent:lead"
    for i in range(n):
        nid = f"task:bulk{i}"
        seed.node(nid, "running" if i % 3 else "done", f"Bulk task {i}")
        if i % 2 == 0:
            seed.edge("dependency", prev, nid)
        if i % 10 == 0:
            prev = nid
    return time.time() - t0


def _die(*_):
    """SIGTERM from the test that started us: leave through the normal path so the temp directory is removed."""
    raise SystemExit(0)


def _watch_parent(parent: int) -> None:
    """If the test runner is killed without a chance to stop us (SIGKILL, a crash, a CI timeout) the process would run on
    for ever with its temp directory. Poll the parent and leave when it is gone."""
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(0)


def main():
    cfg = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    signal.signal(signal.SIGTERM, _die)
    threading.Thread(target=_watch_parent, args=(os.getppid(),), daemon=True).start()
    seeder = Seeder(cfg)
    if not cfg.get("empty"):
        seeder.seed()
    took = bulk_graph(seeder, int(cfg.get("graph_nodes", 0))) if cfg.get("graph_nodes") else 0.0
    print(json.dumps({"base": seeder.base, "room": seeder.room, "room2": seeder.room2, "tokens": seeder.tokens,
                      "ingest_key": seeder.ingest_key, "agent_id": getattr(seeder, "agent_id", None),
                      "steering_id": (getattr(seeder, "steer", None) or {}).get("steering_id"),
                      "ingest_seconds": round(took, 2), "main_port": seeder.srv.port}), flush=True)
    try:
        while True:
            time.sleep(3600)
    finally:
        seeder.close()


if __name__ == "__main__":
    main()
