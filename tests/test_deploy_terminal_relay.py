"""deploy-fleet-agents.sh --with-terminal-relay: flag handling and dry-run by subprocess with a stub ssh/scp (nothing
reaches a host), and the host-side functions (python choice, template rendering, install, health check, rollback) run
locally against a temporary HOME with stub uv/systemctl/journalctl/loginctl."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/deploy-fleet-agents.sh"
RELAY_DIR = ROOT / "deploy/herdr-terminal-relay"
SERVICE, PLIST = RELAY_DIR / "herdr-terminal-relay.service", RELAY_DIR / "com.herdr.terminal-relay.plist"
CONNECTED = "terminal relay connected"


def _functions() -> str:
    text = SCRIPT.read_text()
    start = text.index("# --- terminal relay host functions")
    end = text.index("# --- end terminal relay host functions ---")
    return text[start:end]


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


# ------------------------------------------------------------------------------------------ script, by subprocess
@pytest.fixture
def rig(tmp_path):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    log = tmp_path / "calls.log"
    hosts = tmp_path / "hosts.txt"
    hosts.write_text("alpha|alpha.example.test|systemd\nbeta|beta.example.test|launchd\n")
    # ssh: log the command, answer `echo X` like a shell would not (the paths stay literal), succeed at everything
    _stub(bindir / "ssh", f'echo "ssh $*" >> {log}\nfor last; do :; done\ncase "$last" in "echo "*) echo "${{last#echo }}" ;; esac\nexit 0\n')
    _stub(bindir / "scp", f'echo "scp $*" >> {log}\nexit 0\n')
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}", HERDR_FLEET_HOSTS=str(hosts))

    def run(*args):
        return subprocess.run(["bash", str(SCRIPT), "--no-fetch", "--ref", "HEAD", *args], env=env,
                              capture_output=True, text=True, timeout=120, cwd=ROOT)

    def calls():
        return log.read_text() if log.exists() else ""
    return run, calls


def test_dry_run_without_the_flag_says_the_relay_is_not_requested(rig):
    run, calls = rig
    r = run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert "terminal relay: not requested" in r.stdout and "--with-terminal-relay" in r.stdout
    assert calls() == ""                                          # a dry run touches no host


def test_dry_run_with_the_flag_describes_the_relay_rollout_and_honours_hosts(rig):
    run, calls = rig
    r = run("--dry-run", "--with-terminal-relay", "--hosts", "beta")
    assert r.returncode == 0, r.stderr
    assert "== beta (launchd)" in r.stdout and "== alpha" not in r.stdout
    out = r.stdout
    assert "terminal relay" in out and "Python >= 3.12" in out and CONNECTED in out and "roll back" in out
    assert re.search(r"release fleet-[0-9a-f]{12}", out)
    assert calls() == ""


def test_the_relay_is_never_rolled_without_the_flag(rig):
    run, calls = rig
    r = run("--hosts", "alpha")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "herdr-terminal-relay" not in calls() and "relay_host_install" not in calls()


def test_the_flag_rolls_the_relay_after_the_other_services_and_copies_wheel_and_template(rig):
    run, calls = rig
    r = run("--hosts", "alpha", "--with-terminal-relay")
    assert r.returncode == 0, r.stderr + r.stdout
    c = calls()
    assert "relay_host_install systemd fleet-" in c
    assert c.index("herdr-service-metadata") < c.index("relay_host_install systemd fleet-")      # the other services first
    scps = [line for line in c.splitlines() if line.startswith("scp ")]
    assert any(".whl" in line and "herdr-terminal-relay/releases/fleet-" in line for line in scps)
    assert any("herdr-terminal-relay.service" in line for line in scps)
    assert "relay: ok" in r.stdout


def test_a_launchd_host_gets_the_plist(rig):
    run, calls = rig
    r = run("--hosts", "beta", "--with-terminal-relay")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "relay_host_install launchd fleet-" in calls()
    assert any("com.herdr.terminal-relay.plist" in line for line in calls().splitlines() if line.startswith("scp "))


def test_a_failed_relay_roll_fails_the_run_but_not_the_other_hosts(tmp_path, rig):
    run, calls = rig
    run_env_ssh = tmp_path / "bin" / "ssh"
    body = run_env_ssh.read_text().replace("exit 0\n", 'case "$last" in *"relay_host_install"*) exit 1 ;; esac\nexit 0\n')
    run_env_ssh.write_text(body)
    r = run("--with-terminal-relay")
    assert r.returncode != 0
    assert len(re.findall(r"relay_host_install (?:systemd|launchd) fleet-", calls())) == 2     # every host is attempted
    assert "== beta" in r.stdout


def test_the_flag_is_documented_and_unknown_flags_still_fail(rig):
    run, _ = rig
    assert "--with-terminal-relay" in subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True,
                                                     text=True).stdout
    assert run("--no-such-flag").returncode == 2


# ------------------------------------------------------------------------------------------ host-side functions
@pytest.fixture
def host(tmp_path):
    home = tmp_path / "home dir"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "host.log"
    journal = tmp_path / "journal.txt"
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:/usr/bin:/bin", HERDR_RELAY_PATH_EXTRA="",
               HERDR_RELAY_BREW_PYTHON=str(tmp_path / "no-brew-python"), HERDR_RELAY_HEALTH_SECONDS="2")
    env.pop("HERDR_SOCKET_PATH", None)        # a developer's own herdr must not leak into the checks

    def stubs(*, uv=False, py312=False, linger="yes", connected=True, herdr=True):
        if uv:   # `uv venv DIR` makes a venv whose python is a stub; `uv pip install` makes herdr-eng appear
            _stub(bindir / "uv", f'''echo "uv $*" >> {log}
case "$1" in
  venv) for last; do :; done; mkdir -p "$last/bin"
        printf '#!/bin/sh\\nexit 0\\n' > "$last/bin/python"; chmod +x "$last/bin/python" ;;
  pip) py=""; while [ $# -gt 0 ]; do case "$1" in --python) py="$2"; shift ;; esac; shift; done
       d="$(dirname "$py")"; printf '#!/bin/sh\\n' > "$d/herdr-eng"; chmod +x "$d/herdr-eng" ;;
esac
''')
        if py312:
            _stub(bindir / "python3.12", f'echo "python3.12 $*" >> {log}\nexit 0\n')
        _stub(bindir / "systemctl", f'echo "systemctl $*" >> {log}\nexit 0\n')
        _stub(bindir / "launchctl", f'echo "launchctl $*" >> {log}\nexit 0\n')
        _stub(bindir / "loginctl", f'echo "Linger={linger}"\n')
        _stub(bindir / "id", 'case "$1" in -un) echo wohl ;; -u) echo 501 ;; *) /usr/bin/id "$@" ;; esac\n')
        _stub(bindir / "journalctl", f'echo "journalctl $*" >> {log}\n' +
              (f'echo "{CONNECTED}"\n' if connected else "echo nothing yet\n"))
        _stub(bindir / "pgrep", f'exit {0 if herdr else 1}\n')
        sock = home / ".config/herdr/herdr.sock"
        sock.parent.mkdir(parents=True, exist_ok=True)

    def release(rel, wheel=True, template=SERVICE):
        d = home / ".local/share/herdr-terminal-relay/releases" / rel
        d.mkdir(parents=True)
        if wheel:
            (d / "herdr_engineering-0.0.0-py3-none-any.whl").write_text("wheel")
        (d / template.name).write_text(template.read_text())
        return d

    def call(snippet):
        script = f"{_functions()}\n{snippet}"
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)

    def calls():
        return log.read_text() if log.exists() else ""
    return SimpleNamespaceLike(home=home, bindir=bindir, env=env, stubs=stubs, release=release, call=call, calls=calls,
                               tmp=tmp_path, journal=journal)


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_python_choice_prefers_uv_then_python312_then_homebrew(host, tmp_path):
    host.stubs(uv=True, py312=True)
    assert host.call("relay_python").stdout.strip() == "uv"
    (host.bindir / "uv").unlink()
    r = host.call("relay_python")
    assert r.stdout.strip() == str(host.bindir / "python3.12") and r.returncode == 0
    (host.bindir / "python3.12").unlink()
    brew = _stub(tmp_path / "no-brew-python", "exit 0\n")
    assert host.call("relay_python").stdout.strip() == str(brew)


def test_python_choice_refuses_when_only_an_old_python3_exists(host):
    host.stubs()
    _stub(host.bindir / "python3", 'echo "Python 3.9.6"\n')        # what non-login ssh finds on macOS
    r = host.call("relay_python")
    assert r.returncode != 0 and r.stdout.strip() == ""
    assert "Python >= 3.12" in r.stderr and "uv" in r.stderr and "python3.12" in r.stderr


@pytest.mark.parametrize("manager,template,needle", [("systemd", SERVICE, "ExecStart="), ("launchd", PLIST, "<string>")])
def test_render_points_at_the_per_release_venv_and_fills_the_account(host, tmp_path, manager, template, needle):
    host.stubs()
    out = tmp_path / "out"
    r = host.call(f'relay_render "{template}" "{out}" "$HOME/.local/share/herdr-terminal-relay/current"')
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    current = f"{host.home}/.local/share/herdr-terminal-relay/current"
    assert f"{current}/venv/bin/herdr-eng" in text
    assert "@CURRENT@" not in text and "@USER@" not in text and ".local/bin/herdr-eng" not in text
    if manager == "launchd":
        assert "/Users/wohl/Library/Logs/herdr-terminal-relay.log" in text
        assert re.search(r"<string>herdr_engineering|<string>host</string>\s*<string>terminal-relay</string>", text)


def test_the_tracked_templates_use_only_the_release_placeholders():
    for path in (SERVICE, PLIST):
        text = path.read_text()
        assert "@CURRENT@/venv/bin/herdr-eng" in text and "~/.local/bin" not in text and "%h/.local/bin" not in text
    assert "@USER@" in PLIST.read_text()


def test_install_builds_a_venv_switches_current_and_starts_the_unit(host):
    host.stubs(uv=True)
    rel = host.release("fleet-aaaaaaaaaaaa")
    r = host.call("relay_host_install systemd fleet-aaaaaaaaaaaa")
    assert r.returncode == 0, r.stdout + r.stderr
    base = host.home / ".local/share/herdr-terminal-relay"
    assert (base / "current").resolve() == rel.resolve()
    assert (rel / "venv/bin/herdr-eng").exists()
    unit = (host.home / ".config/systemd/user/herdr-terminal-relay.service").read_text()
    assert f"ExecStart={base}/current/venv/bin/herdr-eng host terminal-relay" in unit
    c = host.calls()
    order = [c.index(x) for x in ("systemctl --user daemon-reload", "systemctl --user enable", "systemctl --user restart")]
    assert order == sorted(order)
    assert "uv venv" in c and ">=3.12" in c and "uv pip install" in c
    assert "relay: ok" in r.stdout or "connected" in r.stdout


def test_install_on_macos_loads_the_plist_with_the_account_name(host):
    host.stubs(uv=True)
    host.release("fleet-bbbbbbbbbbbb", template=PLIST)
    log = host.home / "Library/Logs/herdr-terminal-relay.log"
    log.parent.mkdir(parents=True)
    log.write_text("old line\n")
    # launchctl load makes the relay write its line
    _stub(host.bindir / "launchctl", f'echo "launchctl $*" >> {host.tmp}/host.log\n'
          f'case "$1" in load) echo "{CONNECTED}" >> "{log}" ;; esac\nexit 0\n')
    r = host.call("relay_host_install launchd fleet-bbbbbbbbbbbb")
    assert r.returncode == 0, r.stdout + r.stderr
    plist = (host.home / "Library/LaunchAgents/com.herdr.terminal-relay.plist").read_text()
    assert "/Users/wohl/Library/Logs/herdr-terminal-relay.log" in plist and "@USER@" not in plist
    assert "launchctl load" in host.calls()


def test_old_connected_line_in_the_log_is_not_evidence(host):
    host.stubs(uv=True)
    host.release("fleet-cccccccccccc", template=PLIST)
    log = host.home / "Library/Logs/herdr-terminal-relay.log"
    log.parent.mkdir(parents=True)
    log.write_text(f"{CONNECTED}\n")                                # from the previous release
    r = host.call("relay_host_install launchd fleet-cccccccccccc")
    assert r.returncode != 0


def test_without_evidence_of_a_poll_the_symlink_rolls_back_and_the_previous_release_restarts(host):
    host.stubs(uv=True, connected=False)
    old = host.release("fleet-oldoldoldold")
    new = host.release("fleet-newnewnewnew")
    base = host.home / ".local/share/herdr-terminal-relay"
    (base / "current").symlink_to(old)
    r = host.call("relay_host_install systemd fleet-newnewnewnew")
    assert r.returncode != 0
    assert (base / "current").resolve() == old.resolve() and new.exists()
    assert host.calls().count("systemctl --user restart") == 2          # the new release, then the previous one again
    assert "roll" in r.stderr.lower()


def test_a_first_install_without_evidence_stops_and_disables_the_unit(host):
    host.stubs(uv=True, connected=False)
    host.release("fleet-firstfirstfi")
    r = host.call("relay_host_install systemd fleet-firstfirstfi")
    assert r.returncode != 0
    assert not (host.home / ".local/share/herdr-terminal-relay/current").exists()
    assert "systemctl --user disable --now" in host.calls() or "systemctl --user stop" in host.calls()


def test_no_python_means_nothing_changes_and_the_run_fails_clearly(host):
    host.stubs()
    old = host.release("fleet-oldoldoldold")
    host.release("fleet-newnewnewnew")
    base = host.home / ".local/share/herdr-terminal-relay"
    (base / "current").symlink_to(old)
    r = host.call("relay_host_install systemd fleet-newnewnewnew")
    assert r.returncode != 0 and "Python >= 3.12" in r.stderr
    assert (base / "current").resolve() == old.resolve()
    assert "systemctl" not in host.calls()


def test_linger_off_and_a_missing_herdr_only_warn(host):
    host.stubs(uv=True, linger="no", herdr=False)
    host.release("fleet-dddddddddddd")
    r = host.call("relay_host_install systemd fleet-dddddddddddd")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "linger" in r.stderr.lower() and "enable-linger" in r.stderr
    assert "herdr" in r.stderr and "herdr.sock" in r.stderr
