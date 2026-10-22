# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""The host runner, end to end: the real collab service behind a tiny HTTP front, fake herdr and ssh
programs that record what they were asked, and two "hosts" (this machine and `beast`) that are just
directories."""
from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from herdr_engineering.control_plane.collab import CollabError
from tests.control_plane.test_collab_tasks import W, svc  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "deploy/control-plane/collab-spawn.py"
WORKER = ROOT / "deploy/control-plane/collab-worker.sh"
spec = importlib.util.spec_from_file_location("collab_spawn", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

FAKE_HERDR = r"""#!/usr/bin/env bash
# records every call; `machine list` shows one machine, `workspace create` hands out pane ids
echo "$*" >> "$FAKE_LOG/herdr.log"
args=("$@"); machine=""
if [ "${args[0]:-}" = "--machine" ]; then machine="${args[1]}"; args=("${args[@]:2}"); fi
[ -n "$machine" ] && [ -f "$FAKE_LOG/down-$machine" ] && { echo "ssh: no route" >&2; exit 1; }
case "${args[0]} ${args[1]:-}" in
  "machine list") echo '[{"id":"12f6","label":"beast","target":"beast","session":"default","enabled":true},{"id":"99aa","label":"old","target":"old","session":"default","enabled":false}]' ;;
  "workspace list") echo '{"result":{"type":"workspace_list","workspaces":[]}}' ;;
  "workspace create") n=$(( $(cat "$FAKE_LOG/n" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_LOG/n"
     echo "{\"result\":{\"root_pane\":{\"pane_id\":\"w$n:p1\"}}}" ;;
  "pane run") : ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""
# an "ssh" that runs the remote command locally, with HOME pointing at the pretend remote host
FAKE_SSH = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG/ssh.log"
while [ "$1" != "--" ]; do shift; done; shift   # options, then "--"
shift                                            # the target
HOME="$FAKE_REMOTE_HOME" exec bash -c "$1"
"""


class Front(BaseHTTPRequestHandler):
    svc = None
    ingested: list = []

    def log_message(self, *a):
        pass

    def handle_any(self):
        u = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if u.path == "/api/v1/ingest":                                       # the fabric, not the collab service
            Front.ingested.append(body)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        parts = [p for p in u.path.split("/") if p][3:]                      # after api/v1/collab
        try:
            status, out = self.svc.dispatch(self.command, parts, dict(parse_qsl(u.query)), body, "", None, operator="alice")
        except CollabError as e:
            status, out = e.status, {"error": e.message}
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = handle_any


@pytest.fixture
def rig(svc, tmp_path, monkeypatch):
    svc.max_agents_per_user = 60
    svc.public_config = {"public_url": "https://localdev.example.org"}
    w = W(svc)
    Front.svc = svc
    Front.ingested = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Front)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home, remote, log = tmp_path / "home", tmp_path / "remote", tmp_path / "fakelog"
    for d in (home, remote, log):
        d.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, text in (("herdr", FAKE_HERDR), ("ssh", FAKE_SSH)):
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(remote))
    r = runner.Runner(f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "state", str(bin_ / "herdr"),
                      str(bin_ / "ssh"), WORKER, "bender", say=lambda m: None)
    have = {None: ["claude-code", "codex", "pi"], "beast": ["claude-code", "codex", "pi"]}     # per ssh target
    monkeypatch.setattr(r, "installed_families", lambda target: list(have[target]))
    yield type("Rig", (), dict(w=w, r=r, home=home, remote=remote, log=log, have=have))
    srv.shutdown()


def ask(rig, **kw):
    return rig.w.s.request_spawn(rig.w.alice, {"harness_type": "claude-code", "name_prefix": "fleet", "count": 4,
                                               "room_id": rig.w.room, "actions": ["read", "execute"], **kw})


def calls(rig, which="herdr"):
    p = rig.log / f"{which}.log"
    return p.read_text().splitlines() if p.exists() else []


def test_a_request_is_spread_round_robin_and_workers_start_in_herdr(rig):
    sp = ask(rig)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert done["status"] == "done" and {p["status"] for p in done["placements"]} == {"started"}
    hosts = {p["name"]: p["host"] for p in done["placements"]}
    assert sorted(hosts.values()) == ["beast", "beast", "bender", "bender"]
    assert hosts["fleet-1"] != hosts["fleet-2"] and hosts["fleet-2"] != hosts["fleet-3"]       # alternating
    assert sp["spawn_id"] == done["spawn_id"]
    # the credential sits in a 0600 file on the host that runs the agent, nowhere on a command line
    for name, host in hosts.items():
        base = rig.home if host == "bender" else rig.remote
        f = base / "herdr-agents" / f"{name}.json"
        cred = json.loads(f.read_text())
        assert cred["credential"].startswith("hca_") and stat.S_IMODE(f.stat().st_mode) == 0o600
        assert (base / "herdr-agents/worker.sh").stat().st_mode & 0o700 == 0o700
        assert all(cred["credential"] not in line for line in calls(rig) + calls(rig, "ssh"))
    assert "BASE='https://localdev.example.org'" in (rig.home / "herdr-agents/worker.sh").read_text()
    runs = [line for line in calls(rig) if " pane run " in f" {line} "]
    # text-only by default: no built-in tools and no connectors; the quoting survives the shell untouched
    want = runner.FAMILY_CMD_TEXT_ONLY["claude-code"]
    assert len(runs) == 4 and all("worker.sh" in line for line in runs)
    for line in runs:
        env = shlex.split(line.split("pane run ", 1)[1].split(" ", 1)[1])[0]
        assert env == "HERDR_AGENT_CMD=" + want and "--tools ''" in want and "--strict-mcp-config" in want
    assert sum(line.startswith("--machine beast pane run") for line in runs) == 2
    # the owner granted these in a room they own: they work at once
    assert {a["name"] for a in rig.w.s.list_room_agents(rig.w.alice, rig.w.room)["agents"]} >= {"fleet-1", "fleet-4"}
    # each agent is tied to the pane it runs in, so the dashboard can show it next to its fabric session
    linked = {a["name"]: a["instance_id"] for a in rig.w.s.list_my_agents(rig.w.alice)["agents"] if a["name"].startswith("fleet-")}
    assert set(linked) == set(hosts) and all(v and v.startswith(f"pane:{hosts[k]}:w") for k, v in linked.items())


def test_the_rotation_continues_across_requests(rig):
    ask(rig, count=1, name_prefix="a")
    rig.r.once()
    ask(rig, count=1, name_prefix="b")
    rig.r.once()
    done = {p["name"]: p["host"] for s in rig.w.s.list_spawns(rig.w.alice)["spawns"] for p in s["placements"]}
    assert done["a-1"] != done["b-1"]


def test_an_opted_out_host_never_receives_agents(rig):
    rig.r.once()                                              # report the hosts first
    rig.w.s.set_fleet_host(rig.w.alice, "beast", {"auto_agents": False})
    ask(rig, count=3)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert {p["host"] for p in done["placements"]} == {"bender"} and len(done["placements"]) == 3
    assert not (rig.remote / "herdr-agents").exists()                    # nothing was written on the opted-out host


def test_no_eligible_host_finishes_with_a_reason_and_creates_nothing(rig):
    rig.r.once()
    for h in ("beast", "bender"):
        rig.w.s.set_fleet_host(rig.w.alice, h, {"auto_agents": False})
    ask(rig)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert done["status"] == "done" and done["placements"] == [] and "no host accepts" in done["error"]
    assert rig.w.s.list_my_agents(rig.w.alice)["agents"] == []


def test_an_unreachable_machine_is_left_out_and_a_failed_placement_is_reported(rig):
    (rig.log / "down-beast").write_text("")
    ask(rig, count=2)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert {p["host"] for p in done["placements"]} == {"bender"}
    hosts = {h["host"]: h["online"] for h in rig.w.s.list_fleet_hosts(rig.w.alice)["hosts"]}
    assert hosts == {"bender": True}                          # beast failed its probe, so it is not reported


def test_one_failed_agent_does_not_stop_the_rest(rig, monkeypatch):
    real = rig.r.place
    seen = []

    def flaky(host, agent, family, installed):
        seen.append(agent["name"])
        if agent["name"] == "fleet-2":
            raise runner.Failure("herdr exited 1: boom")
        return real(host, agent, family, installed)
    monkeypatch.setattr(rig.r, "place", flaky)
    ask(rig, count=3)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert [p["status"] for p in sorted(done["placements"], key=lambda p: p["name"])] == ["started", "failed", "started"]
    assert "boom" in next(p["error"] for p in done["placements"] if p["status"] == "failed")


def test_a_request_is_claimed_once_even_if_two_runners_race(rig):
    ask(rig, count=1)
    rig.r.once()
    rig.r.once()                                              # nothing left to claim: no second start
    assert len([line for line in calls(rig) if " pane run " in f" {line} "]) == 1


def test_worker_template_matches_the_script_the_dashboard_hands_out():
    js = ("import('./packages/collab-scripts.js').then(m=>process.stdout.write(m.workerScript({base:'__HERDR_BASE__'})))")
    if not shutil.which("node"):
        pytest.skip("needs node")
    fresh = subprocess.run(["node", "-e", js], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert WORKER.read_text() == fresh, ("collab-worker.sh is out of date; regenerate it with: node -e \"" + js +
                                         "\" > deploy/control-plane/collab-worker.sh")
    subprocess.run(["bash", "-n", str(WORKER)], check=True)


@pytest.mark.parametrize("label,target,enabled,ok", [
    ("beast", "beast", True, True), ("Bad Name", "x", True, False), ("-oProxy", "x", True, False),
    ("ok", "-oProxyCommand=x", True, False), ("ok", "user@host", False, False), ("ok", "user@host", True, True)])
def test_machine_names_that_could_smuggle_options_are_ignored(rig, monkeypatch, label, target, enabled, ok):
    listing = json.dumps([{"label": label, "target": target, "enabled": enabled}])
    monkeypatch.setattr(rig.r, "run", lambda argv, **k: listing)
    rig.r.discover()
    assert (len(rig.r.targets) == 2) is ok


def test_an_unexpected_crash_still_reports_and_leaves_no_live_agent(rig, monkeypatch):
    def boom(host, agent, family, installed):
        if agent["name"] != "fleet-1":
            raise OSError("No space left on device")
        return None
    monkeypatch.setattr(rig.r, "place", boom)
    ask(rig, count=3)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert done["status"] == "done" and sorted(p["status"] for p in done["placements"]) == ["failed", "failed", "started"]
    live = {a["name"] for a in rig.w.s.list_my_agents(rig.w.alice)["agents"] if a["status"] == "active"}
    assert live == {"fleet-1"}                                  # the ones that never started were revoked
    assert not (rig.home / "herdr-agents/fleet-2.json").exists()


def test_the_service_being_unreachable_ends_the_cycle_cleanly(rig):
    rig.r.url = "http://127.0.0.1:1"                            # nothing listens here
    with pytest.raises(runner.Failure):
        rig.r.once()


def test_unticking_a_host_mid_request_stops_placements_there(rig, monkeypatch):
    rig.r.once()                                                # hosts reported
    real, n = rig.r.place, []

    def untick_after_first(host, agent, family, installed):
        real(host, agent, family, installed)
        n.append(host)
        if len(n) == 1:
            rig.w.s.set_fleet_host(rig.w.alice, "beast", {"auto_agents": False})
    monkeypatch.setattr(rig.r, "place", untick_after_first)
    ask(rig, count=4)
    rig.r.once()
    assert "beast" not in n[1:]                                 # whatever came after the click avoided it
    assert len(n) == 4


def test_a_request_nobody_took_in_time_is_expired_not_started_late(rig):
    sp = ask(rig, count=1)
    rig.w.s.clock.t += runner_stale() + 1
    rig.r.once()
    assert rig.w.s.list_spawns(rig.w.alice)["spawns"][0]["status"] == "expired"
    assert [line for line in calls(rig) if " pane run " in f" {line} "] == [] and sp["spawn_id"]


def runner_stale():
    from herdr_engineering.control_plane.collab import SPAWN_STALE
    return SPAWN_STALE


def test_a_host_without_the_cli_is_never_handed_that_family(rig):
    rig.have["beast"] = ["codex"]                              # beast has no claude
    ask(rig)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert done["status"] == "done" and {p["host"] for p in done["placements"]} == {"bender"}
    hosts = {h["host"]: h["families"] for h in rig.w.s.list_fleet_hosts(rig.w.alice)["hosts"]}
    assert hosts == {"bender": ["claude-code", "codex", "pi"], "beast": ["codex"]}


def test_no_host_with_the_cli_finishes_with_a_reason_and_creates_nothing(rig):
    rig.have[None], rig.have["beast"] = ["claude-code"], ["claude-code"]
    before = len(rig.w.s.list_my_agents(rig.w.alice)["agents"])
    ask(rig, harness_type="pi")
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert done["status"] == "done" and "pi installed" in done["error"] and done["placements"] == []
    assert len(rig.w.s.list_my_agents(rig.w.alice)["agents"]) == before


def test_the_agent_family_needs_no_cli(rig):
    rig.have[None], rig.have["beast"] = [], []
    ask(rig, harness_type="agent", count=2)
    rig.r.once()
    assert {p["status"] for p in rig.w.s.list_spawns(rig.w.alice)["spawns"][0]["placements"]} == {"started"}


def test_by_default_agents_get_no_tools_and_codex_is_not_started(rig):
    assert "--tools ''" in runner.FAMILY_CMD_TEXT_ONLY["claude-code"] and "--no-tools" in runner.FAMILY_CMD_TEXT_ONLY["pi"]
    assert "codex" not in runner.FAMILY_CMD_TEXT_ONLY
    before = len(rig.w.s.list_my_agents(rig.w.alice)["agents"])
    ask(rig, harness_type="codex")
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert done["placements"] == [] and "--allow-tools" in done["error"]
    assert len(rig.w.s.list_my_agents(rig.w.alice)["agents"]) == before          # nothing was created


def test_allow_tools_switches_to_the_full_commands(rig):
    rig.r.cmds = runner.FAMILY_CMD_TOOLS
    ask(rig, harness_type="codex", count=2)
    rig.r.once()
    done = rig.w.s.list_spawns(rig.w.alice)["spawns"][0]
    assert {p["status"] for p in done["placements"]} == {"started"}
    assert any("codex exec --skip-git-repo-check -" in line for line in calls(rig))
    assert runner.Runner("http://x", Path("/tmp/x"), "h", "s", WORKER, "b", allow_tools=True).cmds is runner.FAMILY_CMD_TOOLS
    assert runner.Runner("http://x", Path("/tmp/x"), "h", "s", WORKER, "b").cmds is runner.FAMILY_CMD_TEXT_ONLY


def test_pi_prompt_cannot_be_read_as_a_file_or_flag():
    for cmd in (runner.FAMILY_CMD_TEXT_ONLY["pi"], runner.FAMILY_CMD_TOOLS["pi"]):
        assert ' -- "Task from a room member: $(cat)"' in cmd


def test_each_started_agent_is_reported_to_the_fabric_as_a_session_on_its_pane(rig):
    ask(rig, count=2)
    rig.r.once()
    ev = [e for e in runner_front().ingested if e["entity_type"] == "session"]
    assert len(ev) == 2 and {e["payload"]["host_id"] for e in ev} == {"bender", "beast"}
    e = ev[0]
    assert e["event_type"] == "session.start" and e["source"] == "collab-spawn"
    assert e["payload"]["agent_role"] == "claude" and e["payload"]["mission_id"] == e["payload"]["pane_id"].split(":")[0]
    assert e["entity_id"] == f"collab:{e['payload']['host_id']}:{e['payload']['pane_id']}"
    assert all("credential" not in json.dumps(x) for x in ev)


def runner_front():
    return Front


def test_a_second_label_for_the_same_machine_is_not_a_second_host(rig, monkeypatch):
    machines = [{"label": "beast", "target": "beast.lan", "enabled": True},
                {"label": "beast-again", "target": "beast.lan", "enabled": True},
                {"label": "zed", "target": "zed.lan", "enabled": True}]
    monkeypatch.setattr(rig.r, "run", lambda argv, stdin=None, timeout=60.0: json.dumps(machines))
    rig.r.discover()
    assert sorted(rig.r.targets) == ["beast", "bender", "zed"]


SANDBOX = ROOT / "deploy/control-plane/collab-sandbox.sh"


def test_with_a_sandbox_the_box_is_installed_and_the_worker_is_started_inside_it(rig):
    rig.r.sandbox_text = SANDBOX.read_text()
    ask(rig)
    rig.r.once()
    for base in (rig.home, rig.remote):
        box = base / "herdr-agents/collab-sandbox.sh"
        assert box.read_text() == SANDBOX.read_text() and box.stat().st_mode & 0o777 == 0o700
    runs = [line for line in calls(rig) if " pane run " in f" {line} "]
    assert runs and all('HERDR_SANDBOX="$HOME/herdr-agents/collab-sandbox.sh"' in line
                        and 'HERDR_AUDIT="$HOME/herdr-agents/audit/fleet-' in line for line in runs)


def test_without_a_sandbox_nothing_is_installed_and_no_box_is_requested(rig):
    assert rig.r.sandbox_text is None
    ask(rig, count=2)
    rig.r.once()
    assert not (rig.home / "herdr-agents/collab-sandbox.sh").exists()
    assert not any("HERDR_SANDBOX" in line for line in calls(rig))


def test_a_host_without_working_bubblewrap_is_offered_no_family_when_sandboxed(monkeypatch, tmp_path):
    r = runner.Runner("http://x", tmp_path, "h", "s", WORKER, "b", sandbox=SANDBOX)
    probes = iter(["claude\ncodex\nbwrap\n", "claude\ncodex\n"])
    monkeypatch.setattr(r, "run", lambda argv, **kw: next(probes))
    assert r.installed_families(None) == ["claude-code", "codex"]
    assert r.installed_families(None) == []                         # no bwrap: never run task text unboxed


# ---------------------------------------------------------------- names, kept equal both ways
STATEFUL_HERDR = r'''#!/usr/bin/env python3
"""A herdr with state: workspaces, tabs and panes with labels, one state file per machine."""
import json, os, sys
a = sys.argv[1:]
machine = "local"
if a[:1] == ["--machine"]:
    machine, a = a[1], a[2:]
path = os.path.join(os.environ["FAKE_LOG"], f"state-{machine}.json")
st = json.load(open(path)) if os.path.exists(path) else {"n": 0, "ws": {}, "tabs": {}, "panes": {}}
def save():
    json.dump(st, open(path, "w"))
def out(r):
    print(json.dumps({"result": r}))
if a[:2] == ["machine", "list"]:
    print(json.dumps([{"id": "12f6", "label": "beast", "target": "beast", "session": "default", "enabled": True}]))
elif a[:2] == ["workspace", "create"]:
    st["n"] += 1; n = st["n"]; w, t, p = f"w{n}", f"w{n}:t1", f"w{n}:p1"
    st["ws"][w] = {"workspace_id": w, "label": a[a.index("--label") + 1], "pane_count": 1, "tab_count": 1}
    st["tabs"][t] = {"tab_id": t, "workspace_id": w, "label": "1", "number": 1}
    st["panes"][p] = {"pane_id": p, "tab_id": t, "workspace_id": w}
    save(); out({"root_pane": {"pane_id": p, "tab_id": t, "workspace_id": w}})
elif a[:2] == ["pane", "list"]: out({"panes": list(st["panes"].values())})
elif a[:2] == ["tab", "list"]: out({"tabs": list(st["tabs"].values())})
elif a[:2] == ["workspace", "list"]: out({"workspaces": list(st["ws"].values())})
elif a[:2] == ["pane", "run"]: pass
elif a[1:2] == ["rename"] and a[0] in ("pane", "tab", "workspace"):
    key = {"pane": "panes", "tab": "tabs", "workspace": "ws"}[a[0]]
    st[key][a[2]]["label"] = " ".join(a[3:]); save(); out({"type": "ok"})
else:
    print("unexpected: " + " ".join(a), file=sys.stderr); sys.exit(2)
'''


@pytest.fixture
def named(rig, tmp_path):
    stateful = tmp_path / "bin" / "herdr-stateful"
    stateful.write_text(STATEFUL_HERDR)
    stateful.chmod(0o755)
    rig.r.herdr = str(stateful)
    rig.state = lambda machine="local": json.loads((rig.log / f"state-{machine}.json").read_text())
    rig.labels = lambda machine="local": {k: {i: v["label"] for i, v in rig.state(machine)[k].items() if "label" in v}
                                           for k in ("ws", "tabs", "panes")}
    return rig


def spawned(rig, count=2):
    ask(rig, count=count)
    rig.r.once()
    return {a["name"]: a for a in rig.w.s.list_my_agents(rig.w.alice)["agents"]}


def test_a_new_agent_carries_its_name_on_workspace_tab_and_pane(named):
    agents = spawned(named)
    for machine in ("local", "beast"):
        labels = named.labels(machine)
        assert set(labels["ws"].values()) == set(labels["tabs"].values()) == set(labels["panes"].values())
        assert len(labels["ws"]) == 1 and next(iter(labels["ws"].values())) in agents


def rename_in_web(named, old, new):
    agent = next(a for a in named.w.s.list_my_agents(named.w.alice)["agents"] if a["name"] == old)
    named.w.s.rename_agent(named.w.alice, agent["agent_id"], {"name": new})


def host_of(named, name):
    return "local" if any(name in named.labels("local")["ws"].values() for _ in [0]) else "beast"


def test_a_rename_on_the_web_reaches_the_herdr_labels(named):
    agents = spawned(named)
    old = sorted(agents)[0]
    where = host_of(named, old)
    rename_in_web(named, old, "architect")
    named.r.once()
    labels = named.labels(where)
    assert "architect" in labels["ws"].values() and "architect" in labels["tabs"].values() \
        and "architect" in labels["panes"].values() and old not in labels["ws"].values()


def test_a_rename_in_herdr_reaches_the_web_and_is_tidied_to_a_valid_name(named):
    agents = spawned(named)
    old = sorted(agents)[0]
    where = host_of(named, old)
    st = named.state(where)
    pane = next(iter(st["panes"]))
    st["panes"][pane]["label"] = "Fresh Name!"
    (named.log / f"state-{where}.json").write_text(json.dumps(st))
    named.r.once()
    assert "fresh-name" in {a["name"] for a in named.w.s.list_my_agents(named.w.alice)["agents"]}
    labels = named.labels(where)
    assert set(labels["ws"].values()) == set(labels["tabs"].values()) == set(labels["panes"].values()) == {"fresh-name"}


def test_a_herdr_rename_to_a_taken_name_is_refused_and_herdr_is_set_back(named):
    agents = spawned(named)
    one, two = sorted(agents)
    where = host_of(named, one)
    st = named.state(where)
    st["ws"][next(iter(st["ws"]))]["label"] = two
    (named.log / f"state-{where}.json").write_text(json.dumps(st))
    named.r.once()
    assert {a["name"] for a in named.w.s.list_my_agents(named.w.alice)["agents"]} == {one, two}
    assert set(named.labels(where)["ws"].values()) == {one}


def test_when_both_sides_changed_the_web_name_wins(named):
    agents = spawned(named)
    old = sorted(agents)[0]
    where = host_of(named, old)
    st = named.state(where)
    st["panes"][next(iter(st["panes"]))]["label"] = "from-herdr"
    (named.log / f"state-{where}.json").write_text(json.dumps(st))
    rename_in_web(named, old, "from-web")
    named.r.once()
    assert {"from-web"} <= {a["name"] for a in named.w.s.list_my_agents(named.w.alice)["agents"]}
    assert set(named.labels(where)["panes"].values()) == {"from-web"}


def test_an_agent_in_a_shared_workspace_only_gets_its_pane_labelled(named):
    info = named.w.s.connect_agent(named.w.alice, {"name": "solo"})
    st = {"n": 5, "ws": {"w5": {"workspace_id": "w5", "label": "my project", "pane_count": 3, "tab_count": 2}},
          "tabs": {"w5:t1": {"tab_id": "w5:t1", "workspace_id": "w5", "label": "build", "number": 1}},
          "panes": {"w5:p9": {"pane_id": "w5:p9", "tab_id": "w5:t1", "workspace_id": "w5"}}}
    (named.log / "state-local.json").write_text(json.dumps(st))
    named.w.s.link_agent(named.w.alice, info["agent_id"], {"instance_id": "herdr:bender:default:w5%3Ap9"})
    named.r.once()
    labels = named.labels("local")
    assert labels["panes"] == {"w5:p9": "solo"} and labels["ws"] == {"w5": "my project"} and labels["tabs"] == {"w5:t1": "build"}
    st = named.state("local")
    st["panes"]["w5:p9"]["label"] = "renamed-here"
    (named.log / "state-local.json").write_text(json.dumps(st))
    named.r.once()
    assert "renamed-here" in {a["name"] for a in named.w.s.list_my_agents(named.w.alice)["agents"]}
    assert named.labels("local")["ws"] == {"w5": "my project"}


def test_link_targets_are_parsed_strictly():
    assert runner.link_target("pane:bender:w1:p4") == ("bender", "w1:p4")
    assert runner.link_target("herdr:bender:default:w1N%3Ap4") == ("bender", "w1N:p4")
    for bad in (None, "", "pane:", "pane:bender", "pane:Bad Host:w1:p1", "herdr:bender:x", "pane:b:../x", "other:a:b:c"):
        assert runner.link_target(bad) is None, bad
    assert runner.label_name("Fresh Name!") == "fresh-name" and runner.label_name("  ") is None
    assert runner.label_name("x" * 40) == "x" * 32 and runner.label_name(None) is None
