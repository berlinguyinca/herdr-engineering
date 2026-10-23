# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Runner and proxy fixes from the final review: no profile fallback, IPv6 requests, claim back-off, the
independent helper watch, fail-closed allow files, live profile refresh, and tunnels cut when a host goes."""
from __future__ import annotations

import json

import pytest

from tests.control_plane.test_collab_egress import load_egress
from tests.control_plane.test_collab_helper_runner import hr  # noqa: F401
from tests.control_plane.test_collab_spawn import WORKER, calls, rig, runner  # noqa: F401
from tests.control_plane.test_collab_tasks import svc  # noqa: F401


@pytest.fixture
def bare(tmp_path):
    def api(method, path, body=None):
        raise AssertionError(f"unexpected call {method} {path}")
    r = runner.Runner("http://127.0.0.1:9", tmp_path / "state", "herdr", "ssh", WORKER, "bender", say=lambda m: None)
    r.api = api
    return r


def test_a_claim_without_a_profile_runs_text_only_and_asks_nobody(bare):
    cmd, _ = bare.helper_command({"family": "claude-code", "room_id": "room_abc"})
    assert cmd.startswith("claude -p") and "--tools ''" in cmd
    assert not hasattr(runner.Runner, "fetch_profile")


V6 = "2606:4700:4700::1111"


def test_an_ipv6_host_the_proxy_blocked_is_requested_bracketed_and_its_approval_reaches_the_proxy(hr, tmp_path):
    r = hr.mk()
    d = tmp_path / "hd"
    d.mkdir()
    # the proxy logs an IPv6 target in canonical form without brackets
    (d / "blocked.jsonl").write_text(json.dumps({"host": V6, "port": 443, "ts": 1}) + "\n")
    st = {"room_id": hr.w.room, "agent_id": "agent_x"}
    r.drain_blocked(d, st)
    assert st["asked"] == [f"[{V6}]:443"]
    req = next(x for x in r.ok("GET", "fleet/egress")["requests"] if x["host_port"] == f"[{V6}]:443")
    r.ok("POST", f"fleet/egress/{req['id']}/decide", {"decision": "approve"})
    r.refresh_allow(d, hr.w.room)
    assert f"[{V6}]:443" in (d / "allow.txt").read_text().splitlines()
    assert (V6, 443) in load_egress().read_allow(str(d / "allow.txt"))     # what the proxy matches a CONNECT on


def test_a_429_at_claim_backs_off_and_is_logged_once_per_distinct_answer(hr, monkeypatch):
    rec = hr.w.s.request_helper(hr.w.root, {"room_id": hr.w.room, "role": "worker", "task": "t"})
    hid = rec["helper_id"]
    claims, answer = [], {"error": "agent limit reached; revoke one you no longer use"}

    def mk():
        r = hr.mk()
        real = r.api

        def api(method, path, body=None):
            if path == f"fleet/helpers/{hid}/claim":
                claims.append(path)
                return 429, dict(answer)
            return real(method, path, body)
        r.api = api
        return r

    for _ in range(3):                                   # every cycle is a new process
        mk().helpers_cycle()
    assert len(claims) == 1
    assert sum(hid in m and "429" in m for m in hr.said) == 1
    monkeypatch.setattr(runner, "CLAIM_BACKOFF", 0.0)
    mk().helpers_cycle()
    mk().helpers_cycle()
    assert len(claims) == 3 and sum(hid in m and "429" in m for m in hr.said) == 1   # same answer: not logged again
    answer["error"] = "global helper limit reached"
    mk().helpers_cycle()
    assert sum(hid in m and "429" in m for m in hr.said) == 2
    assert not (hr.state / hid).exists()
