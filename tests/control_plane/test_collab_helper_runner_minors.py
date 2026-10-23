# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Runner and proxy fixes from the final review: no profile fallback, IPv6 requests, claim back-off, the
independent helper watch, fail-closed allow files, live profile refresh, and tunnels cut when a host goes."""
from __future__ import annotations

import json
import time

import pytest

from tests.control_plane.test_collab_egress import (  # noqa: F401
    Echo,
    Proxy,
    echo,
    load_egress,
    short_dir,
)
from tests.control_plane.test_collab_helper_runner import (  # noqa: F401
    ask,
    hr,
    needs_box,
    procs_with,
    running,
    until,
)
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


def test_a_successor_starts_from_the_handoff_note():
    p = runner.helper_prompt("successor", [])
    assert "Continue the work from the handoff below" in p and p.endswith("--- handoff from the agent you replace ---\n")
    assert runner.helper_prompt("worker", []).endswith("--- task ---\n")


# ------------------------------------------------------------------ the proxy cuts tunnels that lost approval (item 1)
def closed_within(s, seconds):
    s.settimeout(seconds)
    try:
        return s.recv(10) == b""
    except ConnectionResetError:
        return True
    except TimeoutError:
        return False


def test_an_open_tunnel_is_cut_when_its_host_leaves_the_allow_file(short_dir, echo):
    p = Proxy(short_dir, args=["--recheck", "0.2"])
    try:
        other = Echo()
        p.allow.write_text(f"127.0.0.1:{echo.port}\n127.0.0.1:{other.port}\n")
        a, sa = p.connect(f"127.0.0.1:{echo.port}")
        b, sb = p.connect(f"127.0.0.1:{other.port}")
        assert sa == sb == 200
        a.sendall(b"x")
        assert a.recv(1) == b"x"
        p.allow.write_text(f"127.0.0.1:{other.port}\n")             # Remove / expiry of the first host
        assert closed_within(a, 5)
        b.sendall(b"y")
        assert b.recv(1) == b"y"                                   # the still-approved tunnel keeps working
        b.close()
        other.close()
    finally:
        p.stop()


def test_a_tunnel_by_name_is_cut_when_its_listed_internal_address_goes(short_dir, echo):
    p = Proxy(short_dir, args=["--recheck", "0.2"])
    try:
        p.allow.write_text(f"localhost:{echo.port}\n127.0.0.1:{echo.port}\n")
        a, st = p.connect(f"localhost:{echo.port}")
        assert st == 200
        p.allow.write_text(f"localhost:{echo.port}\n")             # the name stays, its loopback address does not
        assert closed_within(a, 5)
    finally:
        p.stop()


def test_helper_proxies_get_an_idle_timeout(tmp_path):
    argv = runner.proxy_argv({"egress": "/x/collab-egress.py", "sock": "/s/egress.sock"}, tmp_path)
    i = argv.index("--idle-timeout")
    assert float(argv[i + 1]) == runner.HELPER_TUNNEL_IDLE and 300 <= runner.HELPER_TUNNEL_IDLE <= 3600
    assert "--allow-file" in argv and str(tmp_path / "allow.txt") in argv


# ------------------------------------------------------------------ the helper watch does not wait for fleets (item 2)


@needs_box
def test_pause_stops_a_running_helper_while_the_fleet_path_is_still_busy(hr, monkeypatch):
    h = ask(hr, task="SLEEP until stopped")
    until(hr, running(hr, h["helper_id"]))
    monkeypatch.setattr(runner, "WATCH_EVERY", 0.3)
    r = hr.mk()
    monkeypatch.setattr(r, "installed_families", lambda target: ["claude-code"])
    seen = {}

    def slow_fleet_path(up):                        # stands in for a fleet start that takes minutes (handle())
        r.ok("POST", "fleet/helpers/pause", {"paused": True})
        end = time.monotonic() + 20
        while (hr.state / h["helper_id"]).exists() and time.monotonic() < end:
            time.sleep(0.1)
        seen["gone_while_busy"] = not (hr.state / h["helper_id"]).exists()
    monkeypatch.setattr(r, "sync_names", slow_fleet_path)
    r.once()
    assert seen["gone_while_busy"] and procs_with(str(hr.fake / "claude")) == []


def fake_running(hr, hid="h_fake", deadline=1e12, **st):
    d = hr.state / hid
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"helper_id": hid, "phase": "running", "room_id": hr.w.room,
                                              "deadline": deadline, "pid": None, **st}))
    (d / "allow.txt").write_text("api.anthropic.com:443\nold.example.org:443\n")
    return d


def failing(r, path_prefix):
    real = r.api

    def api(method, path, body=None):
        if path.startswith(path_prefix):
            raise runner.Failure(f"{method} {path}: unreachable")
        return real(method, path, body)
    r.api = api
    return r


def test_without_the_helper_list_the_allow_file_is_emptied_after_a_short_grace(hr):
    d = fake_running(hr)
    r = failing(hr.mk(), "fleet/helpers")
    runner.write_private(hr.state.parent / "helpers-seen.json", json.dumps({"at": time.time()}))
    with pytest.raises(runner.Failure):
        r.helpers_cycle(claim=False)
    assert "old.example.org" in (d / "allow.txt").read_text()          # within the grace: kept
    runner.write_private(hr.state.parent / "helpers-seen.json",
                         json.dumps({"at": time.time() - runner.HELPER_LIST_GRACE - 1}))
    with pytest.raises(runner.Failure):
        r.helpers_cycle(claim=False)
    assert (d / "allow.txt").read_text() == ""                         # fail closed
    assert (d / "state.json").exists()                                 # not torn down: the service may come back


def test_a_failed_egress_fetch_empties_that_helpers_allow_file_after_the_grace(hr, monkeypatch):
    d = fake_running(hr, allow_ok=time.time() - runner.HELPER_LIST_GRACE - 1)
    r = failing(hr.mk(), "fleet/egress/")
    monkeypatch.setattr(r, "alive", lambda st: True)
    with pytest.raises(runner.Failure):                               # helpers_cycle logs it per helper
        r.watch_helper(d, {"helper_id": "h_fake", "status": "running"}, False)
    assert (d / "allow.txt").read_text() == ""


def test_the_local_deadline_still_ends_a_helper_when_the_service_is_unreachable(hr, monkeypatch):
    fake_running(hr, deadline=time.time() - 60)
    r = failing(hr.mk(), "fleet/helpers")
    killed = []
    monkeypatch.setattr(r, "kill", lambda st: killed.append(st["helper_id"]))
    with pytest.raises(runner.Failure):
        r.helpers_cycle(claim=False)
    assert killed == ["h_fake"]


# ------------------------------------------------------------------ profile changes reach running helpers (item 6)
SVC = {"name": "gh", "upstream_url": "https://api.github.com/v3", "header": "Authorization", "secret_name": "gh_token"}
ROW_PROFILE = {"allowed_families": ["claude-code"], "tools_allowed": True, "services": [], "ssh_secret_names": ["k1"]}


def watched(hr, monkeypatch, st=None, profile=None):
    d = fake_running(hr, **{"family": "claude-code", "tools": True, "ssh_keys": ["k1"], **(st or {})})
    (d / "services.json").write_text("[]")
    (d / "secrets.json").write_text("{}")
    r = hr.mk(allow_tools=True)
    monkeypatch.setattr(r, "alive", lambda st: True)
    ended = []
    monkeypatch.setattr(r, "end_helper", lambda d, st, status, error: ended.append((status, error, st.get("stopped"))))
    r.watch_helper(d, {"helper_id": "h_fake", "status": "running", "profile": {**ROW_PROFILE, **(profile or {})}}, False)
    return d, ended


def test_new_services_and_rotated_keys_reach_a_running_helpers_proxy(hr, monkeypatch):
    from tests.control_plane.test_collab_helper_runner import write_secrets
    write_secrets(hr, hr.w.room, {"gh_token": {"value": "Bearer NEW", "host": "api.github.com"}})
    d, ended = watched(hr, monkeypatch, profile={"services": [SVC]})
    assert ended == []
    assert json.loads((d / "services.json").read_text()) == [SVC]
    assert json.loads((d / "secrets.json").read_text()) == {"gh_token": {"value": "Bearer NEW", "host": "api.github.com"}}
    r = hr.mk(allow_tools=True)
    monkeypatch.setattr(r, "alive", lambda st: True)
    r.watch_helper(d, {"helper_id": "h_fake", "status": "running", "profile": ROW_PROFILE}, False)
    assert json.loads((d / "services.json").read_text()) == [] and json.loads((d / "secrets.json").read_text()) == {}


@pytest.mark.parametrize("change,why", [({"allowed_families": ["codex"]}, "family"),
                                        ({"tools_allowed": False}, "tools"),
                                        ({"ssh_secret_names": []}, "ssh key")])
def test_tightening_the_profile_stops_a_running_helper_within_one_watch(hr, monkeypatch, change, why):
    _, ended = watched(hr, monkeypatch, profile=change)
    assert len(ended) == 1 and ended[0][:2] == ("failed", "profile-changed") and why in ended[0][2]


def test_an_unchanged_profile_leaves_the_helper_running(hr, monkeypatch):
    _, ended = watched(hr, monkeypatch)
    assert ended == []


def test_a_text_only_helper_is_not_stopped_when_tools_go_off(hr, monkeypatch):
    _, ended = watched(hr, monkeypatch, st={"tools": False}, profile={"tools_allowed": False})
    assert ended == []


def test_the_stop_reason_is_in_the_result(hr, tmp_path):
    r = hr.mk()
    d = tmp_path / "x"
    d.mkdir()
    text = r.helper_result_text(d, {"stopped": "tools are no longer allowed in this room"})
    assert "[stopped: the room profile changed: tools are no longer allowed in this room]" in text


def test_the_watch_thread_logs_a_repeated_failure_once(hr, monkeypatch):
    import threading
    r = failing(hr.mk(), "fleet/helpers")
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(stop, "wait", lambda t: (calls.append(t), len(calls) >= 4)[1])
    r.watch_loop(stop, threading.Lock())
    assert len(calls) == 4 and sum("helpers:" in m for m in hr.said) == 1
