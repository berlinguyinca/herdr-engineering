"""Static + structural guards for the herdr_fleet_update role, playbook and cron."""

import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "ansible"
ROLE = ROOT / "roles" / "herdr_fleet_update"
CRON = ROOT / "tasks" / "fleet-update-cron.yml"
PLAYBOOK = ROOT / "playbooks" / "fleet-update.yml"


def _walk(items):
    for t in items or []:
        yield t
        for key in ("block", "rescue", "always"):
            if key in t:
                yield from _walk(t[key])


def _load(path):
    return yaml.safe_load(path.read_text())


def _tasks(path=ROLE / "tasks" / "main.yml"):
    return list(_walk(_load(path)))


def _by_name(fragment, path=ROLE / "tasks" / "main.yml"):
    hits = [t for t in _tasks(path) if fragment in t.get("name", "")]
    assert hits, fragment
    return hits


def _defaults():
    return _load(ROLE / "defaults" / "main.yml")


def test_defaults():
    d = _defaults()
    assert d["fleet_update_security_only"] is True
    assert d["fleet_update_max_defer_days"] == 7
    assert d["fleet_update_force"] is False
    assert d["fleet_update_api_key"] == ""
    assert d["maintenance"] is False
    assert "fleet-update-state.json" in d["fleet_update_state_file"]
    # the agent posts entity_id = socket.gethostname() == ansible nodename
    assert d["fleet_update_host_id"] == "{{ ansible_facts['nodename'] }}"


def test_pins_reuse_the_single_herdr_pin():
    gv = _load(ROOT / "playbooks" / "group_vars" / "all.yml")
    assert gv["herdr_pinned_version"] == "0.9.1"
    assert gv["fleet_pins"]["herdr"] == "{{ herdr_pinned_version }}"
    assert set(gv["fleet_pins"]) == {"herdr", "pi", "pi_extensions", "autospec"}
    # defined exactly once in the tree: never forked into a play's vars
    hits = [p for p in ROOT.rglob("*.yml")
            if re.search(r"^\s*herdr_pinned_version\s*:", p.read_text(), re.M)]
    assert hits == [ROOT / "playbooks" / "group_vars" / "all.yml"]


def test_every_http_call_uses_the_host_id_var_and_is_no_log():
    uris = [t for t in _tasks() if "ansible.builtin.uri" in t]
    assert len(uris) == 3  # sessions GET, host GET, ingest POST
    for t in uris:
        assert t["no_log"] is True, t["name"]  # may carry X-API-Key into the cron log
        assert t["delegate_to"] == "localhost" and t["become"] is False, t["name"]
        assert t["ansible.builtin.uri"]["headers"].startswith("{{ fleet_update_request_headers")
    host_get = _by_name("read this host's control-plane record")[0]
    assert "fleet_update_host_id" in host_get["ansible.builtin.uri"]["url"]
    assert "inventory_hostname" not in host_get["ansible.builtin.uri"]["url"]
    d = _defaults()
    assert "X-API-Key" in d["fleet_update_request_headers"]


def test_reads_may_run_in_check_mode_writes_may_not():
    for frag in ("read active sessions", "read this host's control-plane record"):
        t = _by_name(frag)[0]
        assert t["check_mode"] is False and t["failed_when"] is False
        assert t["ansible.builtin.uri"]["method"] == "GET"
    sessions = _by_name("read active sessions")[0]
    assert sessions["run_once"] is True
    assert sessions["ansible.builtin.uri"]["url"].endswith("/api/v1/sessions?status=active")
    post = _by_name("Post the fleet-update event")[0]
    assert post["ansible.builtin.uri"]["method"] == "POST"
    assert "not ansible_check_mode" in str(post["when"])
    assert "check_mode" not in post
    record = _by_name("Record results")[0]
    assert "'dry_run': ansible_check_mode" in record["ansible.builtin.command"]["stdin"]
    assert record["run_once"] is True and record["delegate_to"] == "localhost"


def test_event_post_is_tolerant_and_never_sends_telemetry():
    post = _by_name("Post the fleet-update event")[0]
    assert post["failed_when"] is False
    body = post["ansible.builtin.uri"]["body"]
    assert "fleet_update_record.stdout | from_json" in body
    assert "fleet_update_host_id" in body
    assert "telemetry" not in json.dumps(post)
    warn = _by_name("Warn when the control plane refused the event")[0]
    assert "ansible.builtin.debug" in warn
    for p in [*(ROLE / "tasks").glob("*.yml"), PLAYBOOK]:
        code = "\n".join(ln for ln in p.read_text().splitlines() if not ln.lstrip().startswith("#"))
        assert "telemetry" not in code, p


def test_maintenance_is_sent_every_run():
    report = _by_name("Collect this host's report")[0]
    assert "maintenance | default(false) | bool" in json.dumps(report)


def test_decision_feeds_from_both_signals_and_force():
    decide = _by_name("Decide update vs defer")[0]
    stdin = decide["ansible.builtin.command"]["stdin"]
    for key in ("fleet_update_force | bool", "fleet_update_max_defer_days",
                "fleet_update_sessions_probe", "fleet_update_state_file"):
        assert key in stdin, key
    assert decide["check_mode"] is False and decide["run_once"] is True


def test_updates_only_on_update_decision_with_rescue_and_restart_last():
    top = _load(ROLE / "tasks" / "main.yml")
    block = next(t for t in top if t["name"].startswith("Update this host"))
    assert block["when"] == "fleet_update_decision.action == 'update'"
    names = [t["name"] for t in block["block"]]
    restart = [i for i, n in enumerate(names) if "Restart the fabric agent" in n]
    assert restart and restart[-1] >= len(names) - 3
    assert any("fabric-systemd.yml" in json.dumps(t) for t in block["block"])
    assert any("fabric-agent.yml" in json.dumps(t) for t in block["block"])
    rescue = json.dumps(block["rescue"])
    assert "ansible_failed_task.name" in rescue
    assert "stderr" not in rescue and ".msg" not in rescue  # no secrets in the reason


def test_os_packages_security_only_and_full_upgrade_opt_in():
    os_tasks = ROLE / "tasks" / "os-packages.yml"
    sec = _by_name("list security updates", os_tasks)[0]
    assert "apt-get -s" in sec["ansible.builtin.shell"] and "-security" in sec["ansible.builtin.shell"]
    assert sec["check_mode"] is False and sec["changed_when"] is False
    full = _by_name("full upgrade", os_tasks)[0]
    assert "not (fleet_update_security_only | bool)" in str(full["when"])
    brew = _by_name("Homebrew", os_tasks)[0]
    assert "brew update" in brew["ansible.builtin.shell"]
    assert "brew upgrade" in brew["ansible.builtin.shell"]


def test_playbook_targets_fleet_and_includes_role():
    play = _load(PLAYBOOK)[0]
    assert play["hosts"] == "herdr_fleet"
    assert any(t.get("ansible.builtin.include_role", {}).get("name") == "herdr_fleet_update"
               for t in play["tasks"])


def test_site_includes_cron_only_for_gateway_host():
    play = _load(ROOT / "playbooks" / "site.yml")[0]
    inc = [t for t in play["tasks"]
           if t.get("ansible.builtin.include_tasks") == "../tasks/fleet-update-cron.yml"]
    assert len(inc) == 1
    assert inc[0]["when"] == "inventory_hostname == herdr_engineering_gateway_host"
    assert "herdr_pinned_version" not in play.get("vars", {})


def _cron_task():
    return _by_name("Install the daily fleet update cron entry", CRON)[0]


def test_cron_entry_shape():
    t = _cron_task()
    c = t["ansible.builtin.cron"]
    assert c["name"] == "herdr fleet update"
    assert str(c["minute"]) == "30" and str(c["hour"]) == "3"
    job = c["job"]
    assert "flock -n" in job
    assert "--vault-password-file" in job and "fleet_update_vault_password_file" in job
    assert "playbooks/fleet-update.yml" in job
    assert "fleet_update_log_file" in job and ">>" in job
    assert t["become"] is False  # user crontab, not root's


def test_cron_has_no_secret():
    text = CRON.read_text()
    job = _cron_task()["ansible.builtin.cron"]["job"]
    for bad in ("vault_password", "--ask-vault-pass", "X-API-Key", "fleet_update_api_key",
                "tailscale_auth_key", "ANSIBLE_VAULT_PASSWORD"):
        assert bad not in job.replace("vault_password_file", ""), bad
    assert "tskey" not in text
    # the vault password file is never created/written by Ansible
    for t in _tasks(CRON):
        for mod in ("ansible.builtin.copy", "ansible.builtin.template", "ansible.builtin.file",
                    "ansible.builtin.lineinfile"):
            if mod in t:
                assert "fleet_update_vault_password_file" not in json.dumps(t[mod]), t["name"]


def test_cron_default_vault_file_path():
    d = _load(ROOT / "playbooks" / "group_vars" / "all.yml")
    assert d["fleet_update_vault_password_file"].endswith(
        "/.config/herdr-engineering/ansible-vault-pass")


def test_inventory_example_documents_maintenance():
    assert re.search(r"#\s*maintenance: true", (ROOT / "inventory" / "hosts.example.yml").read_text())


def _ansible(name):
    exe = shutil.which(name) or str(Path.home() / ".local/bin" / name)
    return exe if Path(exe).exists() else None


@pytest.mark.parametrize("playbook", ["fleet-update.yml", "site.yml"])
def test_syntax_check(playbook):
    exe = _ansible("ansible-playbook")
    if not exe:
        pytest.skip("ansible-playbook not available")
    r = subprocess.run(
        [exe, "--syntax-check", "-i", str(ROOT / "inventory" / "hosts.example.yml"),
         str(ROOT / "playbooks" / playbook)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert r.returncode == 0, r.stderr


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_check_run_with_unreachable_control_plane_defers(tmp_path):
    """--check on localhost: unknown busy state skips every update task."""
    exe = _ansible("ansible-playbook")
    if not exe:
        pytest.skip("ansible-playbook not available")
    inv = tmp_path / "hosts.ini"
    inv.write_text("[herdr_fleet]\nlocalhost ansible_connection=local\n")
    state = tmp_path / "state.json"
    r = subprocess.run(
        [exe, "--check", "-i", str(inv), str(PLAYBOOK),
         "-e", "herdr_engineering_become=false",
         "-e", f"fleet_update_control_plane_url=http://127.0.0.1:{_free_port()}",
         "-e", f"fleet_update_state_file={state}"],
        capture_output=True, text=True, cwd=ROOT, timeout=300,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "deferred: busy state unknown" in out
    # the update block's first include is skipped: nothing ran before the decision
    lines = out.splitlines()
    i = next(n for n, ln in enumerate(lines) if "Fleet update: OS packages]" in ln)
    assert lines[i + 1].startswith("skipping:"), lines[i:i + 2]
    assert "OS packages (Debian)" not in out and "herdr_linux :" not in out
    assert not state.exists()  # check mode writes nothing


def test_record_and_post_tasks_send_typed_json(tmp_path):
    """Run the role's own record + post tasks on localhost against a fake control
    plane (no host changes): the body must keep bool/null types, carry
    maintenance=false explicitly and no telemetry, and the state file is written."""
    import http.server
    import threading

    exe = _ansible("ansible-playbook")
    if not exe:
        pytest.skip("ansible-playbook not available")
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append((self.path, dict(self.headers),
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    wanted = ("Collect this host's report", "Record results", "Post the fleet-update event",
              "Warn when the control plane refused")
    tasks = [t for t in _load(ROLE / "tasks" / "main.yml")
             if any(w in t.get("name", "") for w in wanted)]
    for t in tasks:
        t["delegate_to"] = "localhost"
    state = tmp_path / "state.json"
    play = [{"hosts": "localhost", "gather_facts": False,
             "vars": {**_defaults(),
                      "fleet_update_helper": str(ROOT / "files" / "fleet-update-state.py"),
                      "fleet_update_control_plane_url": f"http://127.0.0.1:{srv.server_port}",
                      "fleet_update_api_key": "sekrit-key-123",
                      "fleet_update_state_file": str(state),
                      "fleet_update_host_id": "beast",
                      "fleet_pins": {"herdr": "0.9.1", "pi": "", "autospec": "",
                                     "pi_extensions": {}},
                      "fleet_update_outcome": "deferred",
                      "fleet_update_reason": "agents running",
                      "fleet_update_decision": {"last_updated": None,
                                                "deferred_since": "2026-10-20T00:00:00+00:00"}},
             "tasks": tasks}]
    pb = tmp_path / "p.yml"
    pb.write_text(yaml.safe_dump(play))
    r = subprocess.run([exe, "-i", "localhost,", "-c", "local", str(pb)],
                       capture_output=True, text=True, timeout=120)
    srv.shutdown()
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "sekrit-key-123" not in out
    assert len(seen) == 1
    path, headers, body = seen[0]
    assert path == "/api/v1/ingest"
    assert {k.lower(): v for k, v in headers.items()}.get("x-api-key") == "sekrit-key-123"
    assert body["entity_id"] == "beast" and body["entity_type"] == "host"
    p = body["payload"]
    assert p["maintenance"] is False
    assert "telemetry" not in p
    assert p["pins"] == {"herdr": "0.9.1"}
    assert p["fleet_update"] == {"last_updated": None,
                                 "deferred_since": "2026-10-20T00:00:00+00:00",
                                 "result": "deferred: agents running"}
    assert json.loads(state.read_text())["beast"]["result"] == "deferred: agents running"


def test_herdr_eng_only_at_a_pinned_revision_and_restart_gated():
    top = _load(ROLE / "tasks" / "main.yml")
    block = next(t for t in top if t["name"].startswith("Update this host"))["block"]
    fa = next(t for t in block if "fabric-agent.yml" in json.dumps(t))
    assert fa["when"] == "herdr_engineering_revision is defined"
    gv = _load(ROOT / "playbooks" / "group_vars" / "all.yml")
    assert gv["fleet_update_herdr_engineering_revision"] == ""
    for t in block:
        if "Restart the fabric agent" in t["name"]:
            assert "fleet_update_restart_needed | bool" in t["when"]
