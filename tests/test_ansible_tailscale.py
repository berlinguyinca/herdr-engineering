"""Static + structural guards for the ansible tailscale role (no tailnet needed)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
ROOT = REPO / "ansible"
ROLE = ROOT / "roles" / "tailscale"


def _walk(items):
    for t in items:
        yield t
        for key in ("block", "rescue", "always"):
            if key in t:
                yield from _walk(t[key])


def _tasks():
    return list(_walk(yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())))


def _by_name(fragment):
    hits = [t for t in _tasks() if fragment in t["name"]]
    assert hits, fragment
    return hits


def _module(t):
    return next(k for k in t if k.startswith("ansible.builtin.") or k.startswith("community."))


def test_defaults():
    d = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert d["tailscale_auth_key"] == ""
    assert d["tailscale_tags"] == ["tag:fleet"]


def test_key_touching_tasks_are_no_log():
    for frag in ("temp directory", "Restrict the key directory", "Write the auth key",
                 "Run tailscale up", "Remove the auth key directory"):
        for t in _by_name(frag):
            assert t.get("no_log") is True, t["name"]
    # any other task that references the key value must not echo it unredacted
    for t in _tasks():
        if "tailscale_auth_key" in str(t) and t.get("no_log") is not True:
            text = str(t)
            assert (
                "length" in text  # presence checks in `when`
                or "[REDACTED]" in text  # the sanitized diagnostic
            ), t["name"]


def test_key_file_is_0600_in_0700_dir_and_cleanup_in_always():
    tasks = list(_walk(yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())))
    copy = next(t for t in tasks if "Write the auth key" in t["name"])
    assert copy["ansible.builtin.copy"]["mode"] == "0600"
    assert copy["ansible.builtin.copy"]["dest"].startswith("{{ tailscale_key_dir.path }}/")
    chmod = next(t for t in tasks if "Restrict the key directory" in t["name"])
    assert chmod["ansible.builtin.file"]["mode"] == "0700"
    td = next(t for t in tasks if "temp directory" in t["name"])
    assert td["ansible.builtin.tempfile"]["state"] == "directory"
    top = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    join = next(t for t in top if t["name"].startswith("Join the tailnet"))
    assert any("Remove the auth key directory" in t["name"] for t in join["always"])
    assert not any("Remove the auth key" in t["name"] for t in join["block"])


def test_join_gated_on_needslogin_and_not_check_mode():
    top = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    join = next(t for t in top if t["name"].startswith("Join the tailnet"))
    when = " ".join(join["when"])
    assert "tailscale_backend_state == 'NeedsLogin'" in when
    assert "not ansible_check_mode" in when
    up = _by_name("Run tailscale up")[0]
    assert "tailscale_backend_state" not in str(up.get("when", ""))  # gated by the block
    argv = up["ansible.builtin.command"]["argv"]
    assert any(a.startswith("--auth-key=file:") for a in argv)
    assert not any("tailscale_auth_key" in a for a in argv)


def test_stopped_and_unsettled_states_never_join():
    stopped = _by_name("stopped node")[0]
    assert "Stopped" in str(stopped["when"])
    unexpected = _by_name("unexpected or unsettled")[0]
    assert "ansible.builtin.fail" in unexpected
    probe = _by_name("settled Tailscale state")[0]
    assert probe["check_mode"] is False and probe["retries"] == 10 and probe["delay"] == 3


def test_systemd_guarded_by_installed_binary():
    sd = _by_name("Run tailscaled at boot")[0]
    assert "tailscale_bin.rc == 0" in " ".join(sd["when"])


def test_version_gate():
    gate = _by_name(">= 1.62")[0]
    assert "version('1.62', '<')" in " ".join(gate["when"])


def test_macos_tasks_non_fatal():
    brew = _by_name("macOS (Homebrew")[0]
    assert brew.get("ignore_errors") is True or brew.get("failed_when") is False
    up = _by_name("Run tailscale up")[0]
    assert up["failed_when"] is False
    linux_fail = _by_name("Fail when tailscale up failed")[0]
    assert "== 'Linux'" in " ".join(linux_fail["when"])
    skip_mac = _by_name("Skip the join on macOS")[0]
    assert "ansible.builtin.debug" in skip_mac


def test_no_tskey_pattern_outside_placeholder():
    files = [*ROLE.rglob("*.yml"), ROOT / "README.md", ROOT / "playbooks" / "site.yml"]
    for f in files:
        for m in re.finditer(r"tskey-[A-Za-z0-9_-]*", f.read_text()):
            assert m.group(0) in ("tskey-auth-XXXX", "tskey-"), (f, m.group(0))
            if m.group(0) == "tskey-":
                # only inside the redaction regex / prose "tskey-..."
                ctx = f.read_text()[m.start() - 20 : m.end() + 25]
                assert "[A-Za-z0-9_-]+" in ctx or "..." in ctx, (f, ctx)


def test_site_includes_role_before_platform_role():
    play = yaml.safe_load((ROOT / "playbooks" / "site.yml").read_text())[0]
    names = []
    for t in play["tasks"]:
        inc = t.get("ansible.builtin.include_role")
        if inc:
            names.append(inc["name"])
    assert names[0] == "tailscale"
    assert names.index("tailscale") < len(names) - 1
    assert "herdr_linux" in str([t for t in play["tasks"]][names.index("tailscale") + 1:])


def _ansible(name):
    exe = shutil.which(name) or str(Path.home() / ".local/bin" / name)
    return exe if Path(exe).exists() else None


def test_syntax_check():
    exe = _ansible("ansible-playbook")
    if not exe:
        pytest.skip("ansible-playbook not available")
    r = subprocess.run(
        [exe, "--syntax-check", "-i", str(ROOT / "inventory" / "hosts.example.yml"),
         str(ROOT / "playbooks" / "site.yml")],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert r.returncode == 0, r.stderr


def test_diagnostic_redacts_key(tmp_path):
    exe = _ansible("ansible-playbook")
    if not exe:
        pytest.skip("ansible-playbook not available")
    diag = _by_name("sanitized tailscale up diagnostic")[0]
    assert "no_log" not in diag  # sanitized output is meant to be shown
    play = [{
        "hosts": "localhost", "gather_facts": False,
        "vars": {
            "tailscale_auth_key": "customkeyformat12345",
            "tailscale_up": {"rc": 1, "stderr_lines": [
                "l1", "l2", "bad key tskey-auth-kAbC123_xyz-9 given",
                "echo customkeyformat12345 here", "l5", "l6"]},
        },
        "tasks": [diag],
    }]
    pb = tmp_path / "p.yml"
    pb.write_text(yaml.safe_dump(play))
    r = subprocess.run([exe, "-i", "localhost,", "-c", "local", str(pb)],
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "tskey-auth-kAbC123" not in out
    assert "customkeyformat12345" not in out
    assert "[REDACTED]" in out and "rc=1" in out
    assert "l1" not in out.split("tailscale up failed")[1]


def test_status_probe_is_bounded_on_linux_and_a_timeout_is_unsettled():
    probe = _by_name("Wait for a settled Tailscale state")[0]
    cmd = probe["ansible.builtin.command"]
    # Linux wraps the probe in `timeout 10` (stock macOS has no timeout(1)).
    assert "timeout 10 " in cmd and "ansible_facts['system'] == 'Linux'" in cmd
    assert cmd.rstrip().endswith("tailscale status --json")
    # rc 124 (timed out) yields no BackendState, i.e. 'Unknown': retried, then
    # the unsettled-state task fails clearly. It never crashes the probe task.
    assert probe["failed_when"] is False
    assert "Unknown" in probe["until"]
