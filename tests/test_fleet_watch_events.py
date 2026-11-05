"""Fleet rollout emitter + outbox (spec section 5 emitter, section 8 redaction).

No mocks: the emitter talks to a real ControlPlaneServer over loopback HTTP; "unreachable" is a port nobody listens
on, "reachable" is the same client re-pointed at the live server. Redaction is proven on adversarial real strings.
"""
from __future__ import annotations

import json
import os
import random
import socket
import stat
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.fleet_rollout import source_stream_id, validate_fleet_rollout
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_watch import events, plan
from herdr_engineering.service_metadata_feed import MetadataClient

SHA_A, SHA_C = "a" * 40, "c" * 40
RID = plan.rollout_id({"autospec": SHA_A})
TRIGGER = {"repo": "autospec", "sha": SHA_A, "pr": 7, "pr_title": "Add thing", "merged_at": "2026-11-04T09:00:00+00:00"}
TARGETS = [{"repo": "autospec", "sha": SHA_A, "before": SHA_C}]


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield repo, f"http://127.0.0.1:{svr.port}"
    svr.shutdown()


def get(base: str, path: str):
    with urllib.request.urlopen(base + path) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------- unit: redaction

ADVERSARIAL = [
    "Authorization: Bearer abcdef0123456789",
    "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11AAAAAAA0abcdefghijklmnopqrstuvwxyz_0123456789ABCDEFGHIJ",
    "git clone https://user:hunter2@github.com/acme/x.git",
    "password=hunter2 passwd: abc secret=zzz api_key=qqq",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW",
    "AKIAIOSFODNN7EXAMPLEAKIAIOSFODNN7EXAMPLE",
    "scp /home/wohlgemuth/.ssh/id_ed25519 host:",
    "open /Users/alice/Library/Keychains/login.keychain-db",
    "ls /root/.aws/credentials",
    "bell \x07 and escape \x1b[31mred\x1b[0m and nul \x00 end",
    "tab\tseparated\rcarriage",
]


def test_redaction_strips_every_adversarial_secret_and_the_result_validates():
    text = "\n".join(["ok installed"] + ADVERSARIAL + ["done"])
    out = events.redact_log_tail(text, home="/home/wohlgemuth")
    for needle in ("ghp_", "github_pat_", "hunter2", "Bearer", "BEGIN", "b3BlbnNz", "AKIAIOSFODNN7",
                   "/home/wohlgemuth", "/Users/alice", "/root", "\x07", "\x1b", "\x00"):
        assert needle not in out, needle
    assert out.startswith("ok installed") and out.endswith("done")
    # whatever the redactor emits, the server-side allow-list accepts
    validate_fleet_rollout(RID, "host.step", {"rollout_id": RID, "ts": "2026-11-04T10:00:00+00:00", "seq": 0,
                                              "host": "beast", "repo": "autospec", "status": "applying",
                                              "log_tail": out})


def test_home_directory_becomes_a_tilde_and_keeps_the_rest_of_the_path():
    out = events.redact_log_tail("wrote /home/wohlgemuth/.local/share/herdr/x and /tmp/other", home="/home/wohlgemuth")
    assert out == "wrote ~/.local/share/herdr/x and /tmp/other"


def test_the_real_home_of_a_non_standard_user_is_redacted_too():
    out = events.redact_log_tail("wrote /srv/people/zed/.cache/x", home="/srv/people/zed")
    assert "/srv/people/zed" not in out and "~/.cache/x" in out


def test_log_tail_keeps_the_last_40_lines_and_at_most_2048_chars():
    out = events.redact_log_tail("\n".join(f"line {n}" for n in range(500)), home="/h")
    assert out.splitlines()[-1] == "line 499" and len(out.splitlines()) <= 40
    out = events.redact_log_tail("\n".join(("w" * 100 + f" {n}") for n in range(40)), home="/h")
    assert len(out) <= 2048 and out.splitlines()[-1].endswith(" 39")
    assert events.redact_log_tail("", home="/h") == ""


def test_random_junk_never_produces_something_the_server_would_reject():
    rng = random.Random(7)
    alphabet = list("abcXYZ019_-=:/. \n\t\r\x00\x1b@") + ["token=", "Authorization: ", "/home/u/", "://u:p@", "-----BEGIN ", "ghp_"]
    for _ in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
        out = events.redact_log_tail(text, home="/home/u")
        validate_fleet_rollout(RID, "host.step", {"rollout_id": RID, "ts": "2026-11-04T10:00:00+00:00", "seq": 0,
                                                  "host": "beast", "repo": "autospec", "status": "applying",
                                                  "log_tail": out})


def test_pr_title_is_collapsed_capped_and_dropped_when_it_carries_a_secret():
    assert events.clean_pr_title("  Add \n thing\t ") == "Add thing"
    assert len(events.clean_pr_title("x" * 500)) == 120
    assert events.clean_pr_title("rotate token=abc123") is None
    assert events.clean_pr_title(None) is None


# --------------------------------------------------------------------------- unit: emitter queueing

def test_emit_validates_before_queueing(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    with pytest.raises(ValueError, match="unknown"):
        em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, environment={"A": "b"})
    with pytest.raises(ValueError, match="status"):
        em.emit(RID, "host.finished", host="beast", repo="autospec", status="exploded")
    assert em.pending() == []
    assert not (tmp_path / "seq.json").exists() or json.loads((tmp_path / "seq.json").read_text()) == {}


def test_emit_redacts_log_tail_and_title_itself(tmp_path):
    em = events.Emitter(tmp_path, client=None, home="/home/wohlgemuth")
    em.emit(RID, "host.finished", host="beast", repo="autospec", status="failed", reason="apply_failed",
            log_tail="pip: /home/wohlgemuth/venv failed\nAuthorization: Bearer zzzzzzzz")
    em.emit(RID, "rollout.observed", trigger={**TRIGGER, "pr_title": "set password=abc"}, targets=TARGETS)
    stored = json.dumps(em.pending())
    assert "wohlgemuth" not in stored and "Bearer" not in stored and "password" not in stored
    assert em.pending()[1]["payload"]["trigger"]["pr_title"] is None


def test_seq_is_per_stream_monotonic_and_survives_a_restart(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.planned", trigger=TRIGGER, targets=TARGETS)
    other = plan.rollout_id({"autospec": "b" * 40})
    em.emit(other, "rollout.started", targets=TARGETS)
    again = events.Emitter(tmp_path, client=None)
    again.emit(RID, "rollout.started", targets=TARGETS)
    seqs = [(e["entity_id"], e["payload"]["seq"]) for e in again.pending()]
    assert seqs == [(RID, 0), (RID, 1), (other, 0), (RID, 2)]
    assert [e["event_id"] for e in again.pending()][0] == f"{RID}:0"


def test_state_files_are_private(tmp_path):
    root = tmp_path / "nested" / "fleet-watch"
    events.Emitter(root, client=None).emit(RID, "rollout.started", targets=TARGETS)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "outbox.jsonl").stat().st_mode) == 0o600


def test_source_events_use_the_repo_stream_id(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    em.source_event("autospec", "source.degraded", reason="rate_limited")
    [queued] = em.pending()
    assert queued["entity_id"] == source_stream_id("autospec")


# --------------------------------------------------------------------------- integration: outbox + live server

def test_unreachable_then_reachable_delivers_everything_once_in_order(tmp_path, live):
    repo, base = live
    client = MetadataClient(f"http://127.0.0.1:{closed_port()}")
    em = events.Emitter(tmp_path, client=client)
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.planned", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.started", targets=TARGETS)
    down = em.flush()
    assert down == {"sent": 0, "pending": 3, "rejected": 0, "error": down["error"]} and down["error"]
    assert len(em.pending()) == 3 and repo.stream("fleet_rollout", RID) == []

    client.retarget(base, None)                                  # the control plane comes back
    up = em.flush()
    assert up == {"sent": 3, "pending": 0, "rejected": 0, "error": None}
    assert [e["payload"]["seq"] for e in repo.stream("fleet_rollout", RID)] == [0, 1, 2]
    assert em.pending() == []
    assert em.flush()["sent"] == 0


def test_a_replayed_outbox_is_idempotent(tmp_path, live):
    repo, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, flush=False)
    saved = (tmp_path / "outbox.jsonl").read_text()
    assert em.flush()["sent"] == 1
    (tmp_path / "outbox.jsonl").write_text(saved)                # crash after delivery, before the outbox was trimmed
    assert em.flush() == {"sent": 1, "pending": 0, "rejected": 0, "error": None}
    assert len(repo.stream("fleet_rollout", RID)) == 1


def test_a_server_side_rejection_is_parked_and_does_not_block_later_events(tmp_path, live):
    repo, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, flush=False)
    poisoned = {"source": "fleet-watch", "entity_type": "fleet_rollout", "entity_id": RID,
                "event_type": "rollout.started",
                "payload": {"rollout_id": RID, "ts": "2026-11-04T10:00:00+00:00", "seq": 9, "targets": TARGETS,
                            "env": {"HOME": "x"}}}
    with (tmp_path / "outbox.jsonl").open("a") as f:
        f.write(json.dumps(poisoned) + "\n")
    em.emit(RID, "rollout.succeeded", flush=False)
    result = em.flush()
    assert (result["sent"], result["rejected"], result["pending"]) == (2, 1, 0)
    assert [e["payload"]["seq"] for e in repo.stream("fleet_rollout", RID)] == [0, 1]
    parked = (tmp_path / "outbox.rejected.jsonl").read_text()
    assert "env" in parked and "400" in parked


def test_a_torn_trailing_line_is_parked_not_fatal(tmp_path, live):
    repo, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, flush=False)
    with (tmp_path / "outbox.jsonl").open("a") as f:
        f.write('{"source": "fleet-wat')                          # power cut mid-write
    result = em.flush()
    assert (result["sent"], result["rejected"]) == (1, 1)
    assert len(repo.stream("fleet_rollout", RID)) == 1


def test_emit_flushes_by_default_and_never_raises_when_the_plane_is_down(tmp_path, live):
    repo, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    assert len(repo.stream("fleet_rollout", RID)) == 1
    down = events.Emitter(tmp_path / "other", client=MetadataClient(f"http://127.0.0.1:{closed_port()}"))
    down.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)       # must not raise
    assert len(down.pending()) == 1


def test_a_transient_server_error_keeps_the_queue_and_its_order(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Busy(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Busy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        em = events.Emitter(tmp_path, client=MetadataClient(f"http://127.0.0.1:{httpd.server_address[1]}"))
        em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, flush=False)
        em.emit(RID, "rollout.started", targets=TARGETS, flush=False)
        assert em.flush() == {"sent": 0, "pending": 2, "rejected": 0, "error": "http 503"}
        assert [e["payload"]["seq"] for e in em.pending()] == [0, 1]
    finally:
        httpd.shutdown()


def test_an_oversize_envelope_is_parked_and_a_client_less_emitter_just_queues(tmp_path, live):
    repo, base = live
    em = events.Emitter(tmp_path, client=None)
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    assert em.flush()["error"] == "no control plane client" and len(em.pending()) == 1
    em.client = MetadataClient(base)
    with (tmp_path / "outbox.jsonl").open("a") as f:
        f.write(json.dumps({"source": "fleet-watch", "entity_type": "fleet_rollout", "entity_id": RID,
                            "event_type": "rollout.started", "payload": {"pad": "x" * 20000}}) + "\n")
    result = em.flush()
    assert (result["sent"], result["rejected"], result["pending"]) == (1, 1, 0)
    assert "too large" in (tmp_path / "outbox.rejected.jsonl").read_text()


# --------------------------------------------------------------------------- e2e: emitter -> control plane -> read API

def test_a_whole_rollout_round_trips_through_the_read_endpoints(tmp_path, live):
    _, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base), home="/home/wohlgemuth")
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.planned", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.started", targets=TARGETS)
    for host, status in (("beast", "ok"), ("bender", "failed")):
        em.emit(RID, "host.started", host=host, repo="autospec")
        em.emit(RID, "host.finished", host=host, repo="autospec", status=status,
                reason=None if status == "ok" else "health_failed", before_sha=SHA_C,
                after_sha=SHA_A if status == "ok" else None,
                started_at="2026-11-04T10:00:00+00:00", finished_at="2026-11-04T10:01:00+00:00", duration_ms=60000,
                checks=[{"name": "parity", "ok": status == "ok"}],
                log_tail="installed into /home/wohlgemuth/venv\nGITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345")
    em.emit(RID, "rollout.halted", halted_at_host="bender", reason="health_failed")
    assert em.pending() == []
    [summary] = get(base, "/api/v1/fleet-rollouts")["rollouts"]
    assert (summary["status"], summary["halted_at_host"]) == ("halted", "bender")
    detail = get(base, f"/api/v1/fleet-rollouts/{RID}")
    assert [(h["host"], h["status"]) for h in detail["hosts"]] == [("beast", "ok"), ("bender", "failed")]
    assert all("wohlgemuth" not in h["log_tail"] and "ghp_" not in h["log_tail"] for h in detail["hosts"])
    assert get(base, "/api/v1/fleet-rollouts/versions")["hosts"][0]["repos"]["autospec"]["sha"] == SHA_A


# --------------------------------------------------------------------------- smoke: CLI in a subprocess

def run_cli(tmp_path: Path, *args: str):
    env = {k: v for k, v in os.environ.items() if k not in ("HERDR_FLEET_WATCH_API", "HERDR_FLEET_WATCH_GIT_BASE")}
    env.update(HOME=str(tmp_path / "home"), XDG_STATE_HOME=str(tmp_path / "state"))
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run([sys.executable, "-c", "import sys; from herdr_engineering.cli import main; sys.exit(main())",
                           "fleet-watch", *args], capture_output=True, text=True, env=env, timeout=60)


def test_cli_outbox_status_and_flush(tmp_path, live):
    repo, base = live
    state_dir = tmp_path / "state" / "herdr-engineering" / "fleet-watch"
    events.Emitter(state_dir, client=None).emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    status = run_cli(tmp_path, "outbox", "status", "--json")
    assert status.returncode == 0 and json.loads(status.stdout)["pending"] == 1
    down = run_cli(tmp_path, "outbox", "flush", "--control-plane", f"http://127.0.0.1:{closed_port()}", "--json")
    assert down.returncode == 1 and json.loads(down.stdout)["pending"] == 1
    up = run_cli(tmp_path, "outbox", "flush", "--control-plane", base, "--json")
    assert up.returncode == 0, up.stderr
    assert json.loads(up.stdout) == {"sent": 1, "pending": 0, "rejected": 0, "error": None}
    assert len(repo.stream("fleet_rollout", RID)) == 1
