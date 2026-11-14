"""Safety contract for the Woodpecker agent prune verdicts."""
import http.server
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("wpci_agent_prune", str(ROOT / "deploy/ci-agent-hygiene/wpci-agent-prune"))
_spec = importlib.util.spec_from_loader("wpci_agent_prune", _loader)
prune = importlib.util.module_from_spec(_spec)
_loader.exec_module(prune)

NOW = 1_000_000_000
HOUR, DAY = 3600, 86400


def agent(**kw):
    base = {"id": 1, "owner_id": -1, "org_id": -1, "created": NOW - 10 * DAY,
            "last_contact": 0, "last_work": 0}
    return {**base, **kw}


def verdict(a, busy=frozenset()):
    return prune.verdict(a, NOW, busy, HOUR, DAY)


def test_old_never_contacted_is_removed():
    assert verdict(agent()) == "never-contacted"


def test_fresh_never_contacted_is_kept():
    assert verdict(agent(created=NOW - 600)) is None


def test_stale_agent_is_removed():
    assert verdict(agent(last_contact=NOW - 2 * DAY, last_work=NOW - 3 * DAY)) == "stale"


def test_recent_contact_or_work_is_kept():
    assert verdict(agent(last_contact=NOW - 60)) is None
    assert verdict(agent(last_contact=NOW - 2 * DAY, last_work=NOW - HOUR)) is None


def test_agent_with_queued_workflow_is_never_removed():
    assert verdict(agent(), busy={1}) is None
    assert verdict(agent(last_contact=NOW - 9 * DAY), busy={1}) is None


def test_user_or_org_agents_are_kept():
    assert verdict(agent(owner_id=7)) is None
    assert verdict(agent(org_id=3)) is None


def test_missing_owner_or_org_fields_are_kept():
    a = agent()
    del a["owner_id"]
    assert verdict(a) is None
    b = agent()
    del b["org_id"]
    assert verdict(b) is None


# ---- end-to-end against a real local HTTP server standing in for Woodpecker ----

SCRIPT = str(ROOT / "deploy/ci-agent-hygiene/wpci-agent-prune")
TOKEN = "s3cr3t-token-value"


class FakeWoodpecker:
    """Real HTTP server. queue_fn(n) returns (status, body) for the n-th queue call."""

    def __init__(self, agents, queue_fn=None, delete_status=None):
        self.agents = agents
        self.queue_fn = queue_fn or (lambda n: (200, {"pending": [], "running": []}))
        self.delete_status = delete_status or {}
        self.deleted, self.queue_calls, self.auth = [], 0, set()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.auth.add(self.headers.get("Authorization"))
                url = urllib.parse.urlparse(self.path)
                if url.path == "/api/agents":
                    q = urllib.parse.parse_qs(url.query)
                    per, page = int(q["perPage"][0]), int(q["page"][0])
                    return self._send(200, outer.agents[(page - 1) * per:page * per])
                if url.path == "/api/queue/info":
                    outer.queue_calls += 1
                    status, body = outer.queue_fn(outer.queue_calls)
                    return self._send(status, body)
                self._send(404)

            def do_DELETE(self):
                aid = int(self.path.rsplit("/", 1)[1])
                status = outer.delete_status.get(aid, 204)
                if status < 300:
                    outer.deleted.append(aid)
                self._send(status)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def wall():
    return int(time.time())


def dead(i, **kw):
    return agent(id=i, created=wall() - 10 * DAY, last_contact=wall() - 3 * DAY, **kw)


def live(i):
    return agent(id=i, created=wall() - 10 * DAY, last_contact=wall() - 60)


def run(srv, tmp_path, *extra, mode=0o600, apply=True):
    tok = tmp_path / "token"
    tok.write_text(TOKEN + "\n")
    tok.chmod(mode)
    cmd = [sys.executable, SCRIPT, "--server", srv.url, "--token-file", str(tok)]
    if apply:
        cmd.append("--apply")
    return subprocess.run(cmd + list(extra), capture_output=True, text=True, timeout=60)


def healthy(n_dead, n_live=None):
    n_live = n_dead + 2 if n_live is None else n_live
    return [dead(i) for i in range(1, n_dead + 1)] + [live(i) for i in range(100, 100 + n_live)]


def test_apply_deletes_only_dead_and_paginates(tmp_path):
    agents = [dead(i) for i in range(1, 41)] + [live(i) for i in range(1000, 1130)]
    with FakeWoodpecker(agents) as srv:
        r = run(srv, tmp_path)
    assert r.returncode == 0, r.stderr
    assert sorted(srv.deleted) == list(range(1, 41))
    assert "removed=40" in r.stdout and "scanned=170" in r.stdout


def test_dry_run_sends_no_delete(tmp_path):
    with FakeWoodpecker(healthy(3)) as srv:
        r = run(srv, tmp_path, apply=False)
    assert r.returncode == 0 and srv.deleted == []
    assert r.stdout.count("would-remove") == 3


def test_token_sent_but_never_printed(tmp_path):
    with FakeWoodpecker(healthy(2)) as srv:
        r = run(srv, tmp_path)
    assert "Bearer " + TOKEN in srv.auth
    assert TOKEN not in r.stdout and TOKEN not in r.stderr


def test_queue_recheck_before_each_delete_skips_newly_busy(tmp_path):
    def queue(n):  # call 1 = initial scan; agent 2 becomes busy afterwards
        return 200, {"pending": [{"agent_id": 2}] if n >= 2 else []}
    with FakeWoodpecker(healthy(3), queue_fn=queue) as srv:
        r = run(srv, tmp_path)
    assert r.returncode == 0, r.stderr
    assert 2 not in srv.deleted and sorted(srv.deleted) == [1, 3]


def test_api_error_mid_run_aborts_with_nothing_further_deleted(tmp_path):
    def queue(n):  # initial ok, first re-check ok, second re-check 500
        return (500, {}) if n >= 3 else (200, {"pending": []})
    with FakeWoodpecker(healthy(4), queue_fn=queue) as srv:
        r = run(srv, tmp_path)
    assert r.returncode != 0
    assert srv.deleted == [1]
    assert TOKEN not in r.stdout + r.stderr


def test_delete_409_skipped_and_run_continues(tmp_path):
    with FakeWoodpecker(healthy(3), delete_status={2: 409}) as srv:
        r = run(srv, tmp_path)
    assert r.returncode == 0, r.stderr
    assert srv.deleted == [1, 3]
    assert "skipped=1" in r.stdout and "removed=2" in r.stdout and "summary" in r.stdout


def test_missing_owner_fields_via_api_deletes_nothing(tmp_path):
    a = dead(1)
    del a["owner_id"]
    del a["org_id"]
    with FakeWoodpecker([a, live(100), live(101)]) as srv:
        r = run(srv, tmp_path)
    assert srv.deleted == []


def test_outage_recovery_all_stale_deletes_nothing(tmp_path):
    agents = [dead(i) for i in range(1, 9)]
    with FakeWoodpecker(agents) as srv:
        r = run(srv, tmp_path)
    assert r.returncode != 0 and srv.deleted == []
    assert "no agent has contacted" in r.stderr


def test_over_half_candidates_aborts(tmp_path):
    agents = [dead(i) for i in range(1, 7)] + [live(i) for i in range(100, 105)]
    with FakeWoodpecker(agents) as srv:
        r = run(srv, tmp_path)
    assert r.returncode != 0 and srv.deleted == []
    assert "50%" in r.stderr


def test_max_deletions_caps_run(tmp_path):
    with FakeWoodpecker(healthy(10, 30)) as srv:
        r = run(srv, tmp_path, "--max-deletions", "4")
    assert r.returncode == 0, r.stderr
    assert srv.deleted == [1, 2, 3, 4]
    assert "removed=4" in r.stdout


def test_max_deletions_default_is_50(tmp_path):
    with FakeWoodpecker(healthy(60, 200)) as srv:
        r = run(srv, tmp_path)
    assert len(srv.deleted) == 50


def test_loose_token_permissions_refused(tmp_path):
    with FakeWoodpecker(healthy(2)) as srv:
        r = run(srv, tmp_path, mode=0o644)
    assert r.returncode != 0 and srv.deleted == [] and srv.queue_calls == 0
    assert "0600" in r.stderr and TOKEN not in r.stderr
