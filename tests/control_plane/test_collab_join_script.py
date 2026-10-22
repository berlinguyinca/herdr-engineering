# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`curl .../join.sh | HERDR_JOIN_TOKEN=... bash`: served by the site, run for real against a live listener."""
from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_public import main_api, stack  # noqa: F401
from tests.control_plane.test_collab_scripts import shim  # noqa: F401
from tests.control_plane.test_collab_spawn import FAKE_HERDR

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not (shutil.which("curl") and shutil.which("bash") and shutil.which("python3")),
                                reason="needs curl, bash and python3")


@pytest.fixture
def site(stack, shim, tmp_path):
    """The collab service answering on a loopback address that the script templates accept as their base."""
    base = f"http://127.0.0.1:{stack.srv.public_port}"
    stack.svc.public_config = {**stack.svc.public_config, "public_url": base}
    shutil.copytree(ROOT / "packages/collab-scripts", stack.srv.web_root / "collab-scripts")
    home = tmp_path / "home"
    (home / ".config/herdr-engineering").mkdir(parents=True)
    (home / ".config/herdr-engineering/host.json").write_text('{"host_id": "bender"}')
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for name, text in (("claude", '#!/bin/sh\ncat >/dev/null\necho "claude-says-hi"\n'), ("herdr", FAKE_HERDR)):
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(stat.S_IRWXU)
    log = tmp_path / "fakelog"
    log.mkdir()
    env = {**shim, "HOME": str(home), "PATH": f"{bin_}:{shim['PATH']}", "FAKE_LOG": str(log)}
    env.pop("HERDR_AGENT_DIR", None)
    return type("Site", (), dict(base=base, env=env, home=home, log=log, api=main_api(stack), svc=stack.svc))


def one_liner(site, token, *opts):
    cmd = f"curl -fsSL {site.base}/api/v1/collab/scripts/join.sh | HERDR_JOIN_TOKEN='{token}' bash -s -- {' '.join(opts)}"
    return subprocess.run(["bash", "-c", cmd], env=site.env, capture_output=True, text=True, timeout=90)


def make_room(site):
    s, p = site.api.call("POST", f"{BASE}/projects", {"name": "p"}, user="alice")
    s, r = site.api.call("POST", f"{BASE}/projects/{p['project_id']}/rooms", {"name": "r"}, user="alice")
    return r["room_id"]


def token(site, room=None, count=2, prefix="fleet", **extra):
    body = {"harness_type": "claude-code", "name_prefix": prefix, "count": count, **extra,
            **({"room_id": room, "actions": ["read", "post", "execute"]} if room else {})}
    s, tok = site.api.call("POST", f"{BASE}/agents/join-tokens", body, user="alice")
    assert s == 201, tok
    return tok["token"]


def test_the_site_serves_templates_with_its_own_address_and_nothing_else(site):
    for name in ("join.sh", "worker.sh", "sandbox.sh"):
        r = subprocess.run(["curl", "-fsS", f"{site.base}/api/v1/collab/scripts/{name}"], env=site.env, capture_output=True, text=True)
        assert r.returncode == 0 and "__HERDR_BASE__" not in r.stdout and not re.search(r"hcj_[A-Za-z0-9_-]{8,}", r.stdout)
        subprocess.run(["bash", "-n"], input=r.stdout, text=True, check=True)
    join = subprocess.run(["curl", "-fsS", f"{site.base}/api/v1/collab/scripts/join.sh"], env=site.env, capture_output=True, text=True).stdout
    assert f"BASE='{site.base}'" in join
    for bad in ("evil.sh", "..%2f..%2fetc%2fpasswd", "join.sh%00"):
        code = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"{site.base}/api/v1/collab/scripts/{bad}"],
                              env=site.env, capture_output=True, text=True).stdout
        assert code == "404", bad


def test_the_packaged_copies_match_the_ones_the_host_runner_installs():
    for packaged, deployed in (("worker.sh", "collab-worker.sh"), ("sandbox.sh", "collab-sandbox.sh")):
        assert (ROOT / "packages/collab-scripts" / packaged).read_text() == (ROOT / "deploy/control-plane" / deployed).read_text(), packaged


def test_one_line_registers_starts_and_the_agents_do_work(site):
    room = make_room(site)
    tok = token(site, room)
    r = one_liner(site, tok, "--count", "2", "--start", "--no-sandbox")
    assert r.returncode == 0, r.stderr + r.stdout
    agents = site.home / "herdr-agents"
    assert sorted(p.name for p in agents.glob("fleet-*.json")) == ["fleet-1.json", "fleet-2.json"]
    assert "started fleet-1" in r.stdout and "approves them" in r.stdout
    assert tok not in r.stdout + r.stderr
    s, reqs = site.api.call("GET", f"{BASE}/rooms/{room}/agent-requests", user="alice")
    assert len(reqs["requests"]) == 2
    site.api.call("POST", f"{BASE}/rooms/{room}/agent-requests/approve-all",
                  {"request_ids": [x["request_id"] for x in reqs["requests"]]}, user="alice")
    cred = json.loads((agents / "fleet-1.json").read_text())
    s, t = site.api.call("POST", f"{BASE}/rooms/{room}/tasks", {"target_agent_id": cred["agent_id"], "text": "say hi"}, user="alice")
    assert s == 201
    done = []
    for _ in range(60):
        s, tasks = site.api.call("GET", f"{BASE}/rooms/{room}/tasks", user="alice")
        done = [x for x in tasks["tasks"] if x["task_id"] == t["task_id"] and x["status"] == "done"]
        if done:
            break
        time.sleep(0.5)
    assert done and done[0]["result"].strip() == "claude-says-hi"
    status = subprocess.run(["bash", "-c", f"curl -fsSL {site.base}/api/v1/collab/scripts/join.sh | bash -s -- --status"],
                            env=site.env, capture_output=True, text=True, timeout=30).stdout
    assert "fleet-1  running" in status and "fleet-2  running" in status
    stop = subprocess.run(["bash", "-c", f"curl -fsSL {site.base}/api/v1/collab/scripts/join.sh | bash -s -- --stop"],
                          env=site.env, capture_output=True, text=True, timeout=30)
    assert "stopped fleet-1" in stop.stdout and not list((agents / "run").glob("*.pid"))


def test_a_bad_token_is_refused_cleanly_and_codex_needs_tools(site):
    r = one_liner(site, "hcj_nope", "--count", "1")
    assert r.returncode != 0 and "refused" in r.stderr
    tok = token(site, count=1, prefix="cx")
    s, t = site.api.call("POST", f"{BASE}/agents/join-tokens", {"harness_type": "codex", "name_prefix": "cx", "count": 1}, user="alice")
    r = one_liner(site, t["token"], "--count", "1", "--start", "--no-sandbox")
    assert r.returncode != 0 and "--tools" in r.stderr
    assert tok != t["token"]


def test_in_herdr_a_fleet_gets_a_workspace_with_a_tab_per_agent_and_helpers_split_their_parents_tab(site):
    r = one_liner(site, token(site, count=3), "--count", "3", "--herdr", "--no-sandbox")
    assert r.returncode == 0, r.stderr + r.stdout
    st = json.loads((site.log / "state-local.json").read_text())
    assert [w["label"] for w in st["ws"].values()] == ["herd-fleet"]
    assert sorted(t["label"] for t in st["tabs"].values()) == ["fleet-1", "fleet-2", "fleet-3"]
    assert sorted(p["label"] for p in st["panes"].values()) == ["fleet-1", "fleet-2", "fleet-3"]
    links = {a["name"]: a for a in site.svc.agent_links(_alice(site))["agents"]}
    assert all(links[n]["instance_id"].startswith("pane:bender:") for n in ("fleet-1", "fleet-2", "fleet-3"))
    # helpers: one more fleet, started under fleet-1
    r = one_liner(site, token(site, count=2, prefix="helper", parent_agent_id=links["fleet-1"]["agent_id"]),
                  "--count", "2", "--herdr", "--no-sandbox")
    assert r.returncode == 0, r.stderr + r.stdout
    st = json.loads((site.log / "state-local.json").read_text())
    tab1 = next(t for t in st["tabs"].values() if t["label"] == "fleet-1")
    assert len(st["tabs"]) == 3 and tab1["pane_count"] == 3                          # no new tab, two splits
    links = {a["name"]: a for a in site.svc.agent_links(_alice(site))["agents"]}
    assert links["helper-1"]["parent_agent_id"] == links["fleet-1"]["agent_id"] == links["helper-2"]["parent_agent_id"]


def _alice(site):
    from tests.control_plane.test_collab_tasks import user
    return user(site.svc, "alice")


def _redeem(site, tok, name):
    s, a = site.api.call("POST", f"{BASE}/agents/join", {"token": tok, "device_id": "d-" + name, "name": name}, user=None)
    assert s in (200, 201), a
    return a


def test_join_link_only_labels_agents_the_token_made_while_it_is_valid(site):
    tok, other = token(site, count=2, prefix="a"), token(site, count=1, prefix="b")
    mine, theirs = _redeem(site, tok, "a-1"), _redeem(site, other, "b-1")
    link = lambda t, aid, inst: site.api.call("POST", f"{BASE}/agents/join-link", {"token": t, "agent_id": aid, "instance_id": inst}, user=None)  # noqa: E731
    assert link(tok, mine["agent_id"], "pane:bender:p_1")[0] == 200
    assert link(tok, theirs["agent_id"], "pane:bender:p_2")[0] == 401           # another token's agent
    assert link("hcj_bogus", mine["agent_id"], "pane:bender:p_1")[0] == 401
    for bad in ("herdr:x:y:z", "pane:bender:p 1;rm", "pane::p", "x" * 300):
        assert link(tok, mine["agent_id"], bad)[0] == 400, bad
    links = {a["name"]: a for a in site.svc.agent_links(_alice(site))["agents"]}
    assert links["a-1"]["instance_id"] == "pane:bender:p_1" and "b-1" not in links
    s, toks = site.api.call("GET", f"{BASE}/agents/join-tokens", user="alice")
    site.api.call("POST", f"{BASE}/agents/join-tokens/{next(t['join_id'] for t in toks['tokens'] if t['name_prefix'] == 'a')}/revoke", {}, user="alice")
    assert link(tok, mine["agent_id"], "pane:bender:p_3")[0] == 401              # revoked token labels nothing


def test_agent_links_lists_only_the_callers_own_agents(site):
    tok = token(site, count=1, prefix="mine")
    a = _redeem(site, tok, "mine-1")
    site.api.call("POST", f"{BASE}/agents/join-link", {"token": tok, "agent_id": a["agent_id"], "instance_id": "pane:bender:p_9"}, user=None)
    s, t = site.api.call("POST", f"{BASE}/agents/join-tokens", {"harness_type": "claude-code", "name_prefix": "bobs", "count": 1}, user="bob")
    if s == 201:
        _redeem(site, t["token"], "bobs-1")
    from tests.control_plane.test_collab_tasks import user
    assert {a["name"] for a in site.svc.agent_links(_alice(site))["agents"]} == {"mine-1"}
    assert "mine-1" not in {a["name"] for a in site.svc.agent_links(user(site.svc, "bob"))["agents"]}
