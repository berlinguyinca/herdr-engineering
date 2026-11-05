"""Fleet rollout emitter + outbox (spec section 5 emitter, section 8 redaction).

No mocks: the emitter talks to a real ControlPlaneServer over loopback HTTP; "unreachable" is a port nobody listens
on, "reachable" is the same client re-pointed at the live server. Redaction is proven on adversarial real strings.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.fleet_rollout import source_stream_id
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_watch import events, plan
from herdr_engineering.service_metadata_feed import MetadataClient

SHA_A, SHA_C = "a" * 40, "c" * 40
RID = plan.rollout_id({"autospec": SHA_A})
TRIGGER = {"repo": "autospec", "sha": SHA_A, "pr": 7, "merged_at": "2026-11-04T09:00:00+00:00"}
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


# --------------------------------------------------------------------------- unit: emitter queueing

def test_emit_validates_before_queueing(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    with pytest.raises(ValueError, match="unknown"):
        em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, environment={"A": "b"})
    with pytest.raises(ValueError, match="status"):
        em.emit(RID, "host.finished", host="beast", repo="autospec", status="exploded")
    assert em.pending() == []
    assert not (tmp_path / "seq.json").exists() or json.loads((tmp_path / "seq.json").read_text()) == {}


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
    # the poisoned line used seq 9, so the next event continues after it: no seq is ever reused
    assert [e["payload"]["seq"] for e in repo.stream("fleet_rollout", RID)] == [0, 10]
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
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.planned", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.started", targets=TARGETS)
    for host, status in (("beast", "ok"), ("bender", "failed")):
        em.emit(RID, "host.started", host=host, repo="autospec")
        em.emit(RID, "host.finished", host=host, repo="autospec", status=status,
                reason=None if status == "ok" else "health_failed", before_sha=SHA_C,
                after_sha=SHA_A if status == "ok" else None,
                started_at="2026-11-04T10:00:00+00:00", finished_at="2026-11-04T10:01:00+00:00", duration_ms=60000,
                checks=[{"name": "parity_ok", "status": "ok" if status == "ok" else "failed"}],
                log=em.record_log(RID, host, "updater", "installed into /home/wohlgemuth/venv\nGITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz012345\n"))
    em.emit(RID, "rollout.halted", halted_at_host="bender", reason="health_failed")
    assert em.pending() == []
    [summary] = get(base, "/api/v1/fleet-rollouts")["rollouts"]
    assert (summary["status"], summary["halted_at_host"]) == ("halted", "bender")
    detail = get(base, f"/api/v1/fleet-rollouts/{RID}")
    assert [(h["host"], h["status"]) for h in detail["hosts"]] == [("beast", "ok"), ("bender", "failed")]
    blob = json.dumps(detail)
    assert "wohlgemuth" not in blob and "ghp_" not in blob                 # the log text never left the host
    assert all(h["log"]["bytes"] > 0 and len(h["log"]["sha256"]) == 64 for h in detail["hosts"])
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


# --------------------------------------------------------------------------- seq never reused

def seqs_of(em):
    return [e["payload"]["seq"] for e in em.pending()]


def test_seq_continues_after_seq_json_is_deleted(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    for _ in range(3):
        em.emit(RID, "rollout.started", targets=TARGETS)
    for name in ("seq.json", "seq.json.bak"):
        (tmp_path / name).unlink(missing_ok=True)
    events.Emitter(tmp_path, client=None).emit(RID, "rollout.halted", halted_at_host="bender", reason="health_failed")
    assert seqs_of(em) == [0, 1, 2, 3]


def test_seq_continues_after_seq_json_is_corrupted(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    for _ in range(2):
        em.emit(RID, "rollout.started", targets=TARGETS)
    for name in ("seq.json", "seq.json.bak"):
        (tmp_path / name).write_text("{not json")
    em.emit(RID, "rollout.succeeded")
    assert seqs_of(em) == [0, 1, 2]


def test_seq_accounts_for_parked_events_and_the_backup(tmp_path, live):
    repo, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.planned", trigger=TRIGGER, targets=TARGETS)
    assert em.pending() == []                                        # everything delivered and trimmed
    (tmp_path / "seq.json").unlink()                                 # the backup still knows
    em.emit(RID, "rollout.started", targets=TARGETS)
    assert [e["payload"]["seq"] for e in repo.stream("fleet_rollout", RID)] == [0, 1, 2]
    # parked events count too
    with (tmp_path / "outbox.rejected.jsonl").open("a") as f:
        f.write(json.dumps({"reason": "x", "line": json.dumps({"entity_id": RID, "payload": {"seq": 40}})}) + "\n")
    for name in ("seq.json", "seq.json.bak"):
        (tmp_path / name).unlink(missing_ok=True)
    assert events.Emitter(tmp_path, client=None).emit(RID, "rollout.succeeded")["payload"]["seq"] == 41


def test_a_failed_seq_write_queues_nothing_and_reuses_no_seq(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    blocker = tmp_path / "seq.json.tmp"
    blocker.mkdir()                                                  # the atomic write cannot open its temp file
    with pytest.raises(OSError):
        em.emit(RID, "rollout.halted", halted_at_host="bender", reason="health_failed")
    assert seqs_of(em) == [0]                                        # nothing half-queued
    blocker.rmdir()
    em.emit(RID, "rollout.halted", halted_at_host="bender", reason="health_failed")
    assert seqs_of(em) == [0, 1]


def test_a_failed_outbox_append_burns_the_seq_but_never_reuses_it(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    saved = (tmp_path / "outbox.jsonl").read_bytes()
    (tmp_path / "outbox.jsonl").unlink()
    (tmp_path / "outbox.jsonl").mkdir()                              # the append fails after seq was reserved
    with pytest.raises(OSError):
        em.emit(RID, "rollout.started", targets=TARGETS)
    (tmp_path / "outbox.jsonl").rmdir()
    (tmp_path / "outbox.jsonl").write_bytes(saved)
    em.emit(RID, "rollout.halted", halted_at_host="bender", reason="health_failed")
    assert seqs_of(em) == [0, 2]                                     # a gap is harmless, a duplicate would lose events


# --------------------------------------------------------------------------- bounded growth

def test_a_full_outbox_parks_the_oldest_and_counts_the_drop(tmp_path):
    em = events.Emitter(tmp_path, client=None, max_pending=3)
    for _ in range(5):
        em.emit(RID, "rollout.started", targets=TARGETS)
    assert seqs_of(em) == [2, 3, 4]
    assert em.dropped() == 2
    parked = (tmp_path / "outbox.rejected.jsonl").read_text()
    assert parked.count("outbox full") == 2                          # visible, not silent
    assert events.Emitter(tmp_path, client=None).dropped() == 2      # persisted


def test_the_rejected_file_rotates(tmp_path):
    em = events.Emitter(tmp_path, client=None, max_pending=1, max_rejected_bytes=2000)
    for _ in range(40):
        em.emit(RID, "rollout.started", targets=TARGETS)
    assert (tmp_path / "outbox.rejected.jsonl").stat().st_size <= 2000 + 1500
    assert (tmp_path / "outbox.rejected.jsonl.1").exists()
    assert not (tmp_path / "outbox.rejected.jsonl.2").exists()


def test_status_reports_dropped(tmp_path):
    state_dir = tmp_path / "state" / "herdr-engineering" / "fleet-watch"
    em = events.Emitter(state_dir, client=None, max_pending=1)
    em.emit(RID, "rollout.started", targets=TARGETS)
    em.emit(RID, "rollout.succeeded")
    out = run_cli(tmp_path, "outbox", "status", "--json")
    assert json.loads(out.stdout)["dropped"] == 1


# --------------------------------------------------------------------------- flush does not block emitters; 200 not ok parks

def test_emit_is_not_blocked_while_a_flush_is_waiting_on_the_network(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    started, release = threading.Event(), threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            started.set()
            release.wait(20)
            body = json.dumps({"ok": True, "entity": {"entity_id": RID}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        em = events.Emitter(tmp_path, client=MetadataClient(f"http://127.0.0.1:{httpd.server_address[1]}"))
        em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, flush=False)
        flusher = threading.Thread(target=em.flush, daemon=True)
        flusher.start()
        assert started.wait(10)
        done = threading.Event()
        late = threading.Thread(target=lambda: (em.emit(RID, "rollout.started", targets=TARGETS, flush=False), done.set()),
                                daemon=True)
        late.start()
        assert done.wait(5), "emit blocked behind a flush that is waiting on the network"
        release.set()
        flusher.join(10)
        assert seqs_of(em) == [1]                                    # the late event survived the flush's trim
    finally:
        release.set()
        httpd.shutdown()


def test_a_200_that_is_not_ok_is_parked_and_does_not_block_the_queue(tmp_path, live):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    repo, base = live

    class NotOk(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'{"ok": false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), NotOk)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        em = events.Emitter(tmp_path, client=MetadataClient(f"http://127.0.0.1:{httpd.server_address[1]}"))
        em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS, flush=False)
        result = em.flush()
        assert (result["sent"], result["rejected"], result["pending"], result["error"]) == (0, 1, 0, None)
        assert "not accepted" in (tmp_path / "outbox.rejected.jsonl").read_text()
    finally:
        httpd.shutdown()


# --------------------------------------------------------------------------- source-stream counters are permanent

def test_source_stream_counters_survive_the_idle_prune(tmp_path):
    from datetime import UTC, datetime, timedelta
    clock = {"now": datetime(2026, 11, 4, tzinfo=UTC)}
    sent = []

    class Recorder:
        def request(self, route, body):
            sent.append((body["entity_id"], body["payload"]["seq"]))
            return {"ok": True, "entity": {"entity_id": body["entity_id"]}}

    em = events.Emitter(tmp_path, client=Recorder(), clock=lambda: clock["now"])
    em.source_event("autospec", "source.degraded", reason="timeout")
    clock["now"] += timedelta(days=31)
    em.emit(RID, "rollout.resumed")
    em.source_event("autospec", "source.recovered")
    sid = source_stream_id("autospec")
    assert [seq for entity, seq in sent if entity == sid] == [0, 1]


def test_losing_every_counter_after_a_full_delivery_still_cannot_collide(tmp_path, live):
    repo, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "rollout.planned", trigger=TRIGGER, targets=TARGETS)
    assert em.pending() == []
    for name in ("seq.json", "seq.json.bak"):
        (tmp_path / name).unlink()
    em.emit(RID, "rollout.started", targets=TARGETS)                 # a counter restart would be a 409 and a lost event
    seqs = [e["payload"]["seq"] for e in repo.stream("fleet_rollout", RID)]
    assert len(seqs) == 3 and seqs[:2] == [0, 1] and 2 <= seqs[2] < 2**31
    assert em.pending() == [] and not (tmp_path / "outbox.rejected.jsonl").exists()


# --------------------------------------------------------------------------- decision 2026-11-05: logs stay on the host

from tests.control_plane.test_fleet_rollout import CORPUS  # noqa: E402  (strings legitimate for no structured field)


def test_the_emitter_cannot_carry_free_text_in_any_field(tmp_path):
    em = events.Emitter(tmp_path, client=None)
    for text in CORPUS:
        for fields in ({"log_tail": text}, {"pr_title": text}, {"note": text}, {"host": text}, {"reason": text},
                       {"log": {"host": "beast", "path_id": text, "sha256": "ab" * 32, "bytes": 1, "lines": 1}},
                       {"checks": [{"name": text, "status": "ok"}]}, {"links": {"pi_session_rollout": text}}):
            with pytest.raises(ValueError):
                em.emit(RID, "host.finished", **{"host": "beast", "repo": "autospec", "status": "ok", **fields})
    assert em.pending() == []


def test_log_text_is_stored_locally_privately_and_only_a_reference_is_emitted(tmp_path, live):
    import hashlib
    repo, base = live
    em = events.Emitter(tmp_path, client=MetadataClient(base))
    secret_log = "pip install failed\nTOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n/home/wohlgemuth/venv\n"
    ref = em.record_log(RID, "beast", "installer", secret_log)
    assert ref == {"host": "beast", "path_id": "installer", "sha256": hashlib.sha256(secret_log.encode()).hexdigest(),
                   "bytes": len(secret_log.encode()), "lines": 3}
    stored = tmp_path / "logs" / RID / "beast-installer.log"
    assert stored.read_text() == secret_log and stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
    em.emit(RID, "rollout.observed", trigger=TRIGGER, targets=TARGETS)
    em.emit(RID, "host.finished", host="beast", repo="autospec", status="failed", reason="apply_failed", log=ref)
    wire = json.dumps(get(base, f"/api/v1/fleet-rollouts/{RID}")) + json.dumps([e["payload"] for e in repo.stream("fleet_rollout", RID)])
    assert "ghp_" not in wire and "wohlgemuth" not in wire and "pip install" not in wire
    assert ref["sha256"] in wire


def test_the_log_store_keeps_only_the_tail_and_prunes_old_logs(tmp_path):
    from datetime import UTC, datetime, timedelta

    from herdr_engineering.fleet_watch import logs
    store = logs.LogStore(tmp_path)
    big = "x" * 1_500_000
    ref = store.write(RID, "bender", "updater", big)
    assert ref["bytes"] == logs.MAX_BYTES == len(store.read_bytes(RID, "bender", "updater"))
    old = tmp_path / RID / "bender-updater.log"
    os.utime(old, (0, 0))
    other = plan.rollout_id({"autospec": "d" * 40})
    store.write(other, "beast", "healthcheck", "ok\n", now=datetime.now(UTC) + timedelta(days=1))
    assert not old.exists() and (tmp_path / other / "beast-healthcheck.log").exists()
    for bad in (("../x", "beast", "updater"), (RID, "evil", "updater"), (RID, "beast", "../../etc")):
        with pytest.raises(ValueError):
            store.write(*bad, "x")


def test_cli_log_show_prints_this_hosts_log_and_refuses_bad_input(tmp_path):
    import hashlib
    state_dir = tmp_path / "state" / "herdr-engineering" / "fleet-watch"
    em = events.Emitter(state_dir, client=None)
    ref = em.record_log(RID, "bender", "healthcheck", "line one\nline two\n")
    shown = run_cli(tmp_path, "log", "show", RID, "--host", "bender")
    assert shown.returncode == 0 and "line two" in shown.stdout and f"sha256={ref['sha256']}" in shown.stdout
    assert hashlib.sha256(b"line one\nline two\n").hexdigest() == ref["sha256"]
    assert run_cli(tmp_path, "log", "show", RID, "--host", "bender", "--path-id", "installer").returncode == 1
    assert run_cli(tmp_path, "log", "show", RID, "--host", "beast").returncode == 1
    assert run_cli(tmp_path, "log", "show", "../etc", "--host", "bender").returncode == 2
    assert run_cli(tmp_path, "log", "show", RID, "--host", "evil").returncode == 2


def test_per_rollout_counters_are_never_pruned_so_a_resumed_rollout_never_reuses_a_seq(tmp_path):
    from datetime import UTC, datetime, timedelta
    clock = {"now": datetime(2026, 11, 4, tzinfo=UTC)}
    sent = []

    class Recorder:
        def request(self, route, body):
            sent.append((body["entity_id"], body["payload"]["seq"]))
            return {"ok": True, "entity": {"entity_id": body["entity_id"]}}

    em = events.Emitter(tmp_path, client=Recorder(), clock=lambda: clock["now"])
    em.emit(RID, "rollout.started", targets=TARGETS)
    clock["now"] += timedelta(days=31)
    em.emit(plan.rollout_id({"autospec": "d" * 40}), "rollout.resumed")
    em.emit(RID, "rollout.resumed")                                  # a halted rollout resumed after 31 idle days
    assert [seq for entity, seq in sent if entity == RID] == [0, 1]
    clock["now"] = datetime(2038, 1, 1, tzinfo=UTC)                  # the clock floor stays below the 2**31 cap
    for name in ("seq.json", "seq.json.bak"):
        (tmp_path / name).unlink()
    em2 = events.Emitter(tmp_path / "other", client=None, clock=lambda: clock["now"])
    em2.emit(RID, "rollout.resumed")
    for name in ("seq.json", "seq.json.bak"):
        (tmp_path / "other" / name).unlink()
    assert em2.emit(RID, "rollout.resumed")["payload"]["seq"] < 2**31


# --------------------------------------------------------------------------- round 4

def test_log_show_digest_equals_the_events_sha256_for_crlf_and_invalid_utf8(tmp_path):
    state_dir = tmp_path / "state" / "herdr-engineering" / "fleet-watch"
    em = events.Emitter(state_dir, client=None)
    ref = em.record_log(RID, "bender", "updater", "one\r\ntwo\r\n")                  # CRLF
    shown = run_cli(tmp_path, "log", "show", RID, "--host", "bender", "--path-id", "updater")
    assert shown.returncode == 0 and f"sha256={ref['sha256']}" in shown.stdout
    raw = b"bad \xff\xfe bytes\n"                                                     # not valid UTF-8
    (state_dir / "logs" / RID / "bender-installer.log").write_bytes(raw)
    import hashlib
    shown = run_cli(tmp_path, "log", "show", RID, "--host", "bender", "--path-id", "installer")
    assert shown.returncode == 0 and f"sha256={hashlib.sha256(raw).hexdigest()}" in shown.stdout


def test_a_preexisting_world_readable_logs_root_is_made_private(tmp_path):
    from herdr_engineering.fleet_watch import logs
    root = tmp_path / "logs"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    logs.LogStore(root).write(RID, "beast", "updater", "x\n")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / RID).stat().st_mode) == 0o700
