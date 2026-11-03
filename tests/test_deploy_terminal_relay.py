"""deploy-fleet-agents.sh --with-terminal-relay: flag handling and dry-run by subprocess with a stub ssh/scp (nothing
reaches a host), and the host-side functions (python choice, template rendering, install, health check, rollback) run
locally against a temporary HOME with stub uv/python/systemctl/launchctl/loginctl; the health check reads
the relay's status file, not a log."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/deploy-fleet-agents.sh"
RELAY_DIR = ROOT / "deploy/herdr-terminal-relay"
SERVICE, PLIST = RELAY_DIR / "herdr-terminal-relay.service", RELAY_DIR / "com.herdr.terminal-relay.plist"


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
    assert "terminal relay" in out and "Python >= 3.12" in out and "status file" in out and "roll back" in out
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
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "host.log"
    dead = subprocess.Popen(["true"])
    dead.wait()
    # A CI box has a real python3.13 and pip in /usr/bin: a decoy on PATH proves no real interpreter is ever consulted.
    decoy = tmp_path / "usr-bin"
    decoy.mkdir()
    for name in ("python3.13", "python3", "pip", "uv"):
        _stub(decoy / name, 'case "$*" in *version_info*) echo 3.13 ;; *) echo "decoy $0 $*" >> "$DECOY_LOG"; exit 9 ;; esac\n')
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{decoy}:/usr/bin:/bin", DECOY_LOG=str(tmp_path / "decoy.log"),
               HERDR_RELAY_PATH_EXTRA="", HERDR_RELAY_PYTHON_DIRS=str(bindir), HERDR_RELAY_UV_DIRS=str(bindir),
               HERDR_RELAY_BREW_PYTHON=str(tmp_path / "no-brew-python"), HERDR_RELAY_HEALTH_SECONDS="2",
               HERDR_ENGINEERING_STATE=str(state), STUB_ALIVE_PID=str(os.getpid()), STUB_DEAD_PID=str(dead.pid))
    env.pop("HERDR_SOCKET_PATH", None)        # a developer's own herdr must not leak into the checks

    def pystub(path, version, *, venv_version=None):
        """An interpreter that REPORTS its version, and can make a venv whose python reports venv_version."""
        vv = venv_version or version
        _stub(path, f"""echo "$(basename "$0") $*" >> {log}
case "$*" in
  *version_info*) echo {version} ;;
  "-m venv "*) d="${{@: -1}}"; mkdir -p "$d/bin"
    printf '#!/bin/bash\\ncase "$*" in *version_info*) echo {vv} ;; esac\\n' > "$d/bin/python"
    printf '#!/bin/bash\\nd="$(dirname "$0")"; printf "#!/bin/sh\\\\n" > "$d/herdr-eng"; chmod +x "$d/herdr-eng"\\n' > "$d/bin/pip"
    chmod +x "$d/bin/python" "$d/bin/pip" ;;
esac
""")

    def stubs(*, uv=False, uv_fails=False, pys=(), venv_version=None, linger="yes", herdr=True, connect="ok"):
        if uv:
            _stub(bindir / "uv", f"""echo "uv $*" >> {log}
case "$1" in
  venv) {'exit 1' if uv_fails else ''}
        for last; do :; done; mkdir -p "$last/bin"
        printf '#!/bin/bash\\ncase "$*" in *version_info*) echo {venv_version or "3.12"} ;; esac\\n' > "$last/bin/python"
        chmod +x "$last/bin/python" ;;
  pip) py=""; while [ $# -gt 0 ]; do case "$1" in --python) py="$2"; shift ;; esac; shift; done
       d="$(dirname "$py")"; printf '#!/bin/sh\\n' > "$d/herdr-eng"; chmod +x "$d/herdr-eng" ;;
esac
""")
        for name, version in pys:
            pystub(bindir / name, version, venv_version=venv_version)
        # what the relay would write on its first successful wait, once the service (re)starts
        write = {
            "ok": 'printf \'{"pid": %s, "started_at": 1, "connected_at": %s.5, "state": "connected"}\' "$STUB_ALIVE_PID" "$(date +%s)"',
            "dead": 'printf \'{"pid": %s, "started_at": 1, "connected_at": %s.5, "state": "connected"}\' "$STUB_DEAD_PID" "$(date +%s)"',
            "old": 'printf \'{"pid": %s, "started_at": 1, "connected_at": 100.5, "state": "connected"}\' "$STUB_ALIVE_PID"',
            "never": "true"}[connect]
        on_start = f'( {write} ) > "$HERDR_ENGINEERING_STATE/terminal-relay-status.json"\n'
        _stub(bindir / "systemctl", f'echo "systemctl $*" >> {log}\ncase "$*" in *restart*) {on_start};; esac\nexit 0\n')
        _stub(bindir / "launchctl", f'echo "launchctl $*" >> {log}\ncase "$1" in bootstrap|kickstart) {on_start};; esac\nexit 0\n')
        _stub(bindir / "loginctl", f'echo "Linger={linger}"\n')
        _stub(bindir / "id", 'case "$1" in -un) echo wohl ;; -u) echo 501 ;; *) /usr/bin/id "$@" ;; esac\n')
        _stub(bindir / "pgrep", f'exit {0 if herdr else 1}\n')
        (home / ".config/herdr").mkdir(parents=True, exist_ok=True)

    def release(rel, wheel=True, template=SERVICE):
        d = home / ".local/share/herdr-terminal-relay/releases" / rel
        d.mkdir(parents=True)
        if wheel:
            (d / "herdr_engineering-0.0.0-py3-none-any.whl").write_text("wheel")
        (d / template.name).write_text(template.read_text())
        return d

    def call(snippet):
        script = f"{_functions()}\n{snippet}"
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def calls():
        return log.read_text() if log.exists() else ""
    yield SimpleNamespaceLike(home=home, bindir=bindir, env=env, stubs=stubs, release=release, call=call, calls=calls,
                              tmp=tmp_path, state=state, pystub=pystub)
    assert not (tmp_path / "decoy.log").exists(), "a real-looking interpreter, pip or uv outside the stubs was used"


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_python_choice_prefers_uv_then_a_system_python_312_or_newer_then_homebrew(host, tmp_path):
    host.stubs(uv=True, pys=[("python3.12", "3.12")])
    assert host.call("relay_python").stdout.strip() == "uv"
    (host.bindir / "uv").unlink()
    r = host.call("relay_python")
    assert r.stdout.strip() == str(host.bindir / "python3.12") and r.returncode == 0
    (host.bindir / "python3.12").unlink()
    brew = tmp_path / "no-brew-python"
    host.pystub(brew, "3.12")
    assert host.call("relay_python").stdout.strip() == str(brew)


def test_a_host_with_only_python_313_works(host):
    host.stubs(pys=[("python3.13", "3.13")])
    assert host.call("relay_python").stdout.strip() == str(host.bindir / "python3.13")
    host.release("fleet-313313313313")
    r = host.call("relay_host_install systemd fleet-313313313313")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "python3.13 -m venv" in host.calls()


def test_python_choice_checks_the_real_version_not_the_name(host):
    host.stubs(pys=[("python3.12", "3.11"), ("python3", "3.9")])     # a misnamed binary, and macOS's own python3
    r = host.call("relay_python")
    assert r.returncode != 0 and r.stdout.strip() == ""
    assert "Python >= 3.12" in r.stderr and "uv" in r.stderr and "python3.12" in r.stderr


def test_a_plain_python3_that_is_new_enough_is_accepted(host):
    host.stubs(pys=[("python3", "3.14")])
    assert host.call("relay_python").stdout.strip() == str(host.bindir / "python3")


def test_a_venv_interpreter_older_than_312_is_refused_and_nothing_changes(host):
    host.stubs(pys=[("python3.12", "3.12")], venv_version="3.11")
    old = host.release("fleet-oldoldoldold")
    host.release("fleet-newnewnewnew")
    base = host.home / ".local/share/herdr-terminal-relay"
    (base / "current").symlink_to(old)
    r = host.call("relay_host_install systemd fleet-newnewnewnew")
    assert r.returncode != 0 and "3.12" in r.stderr
    assert (base / "current").resolve() == old.resolve() and "systemctl" not in host.calls()


def test_uv_failing_falls_back_to_the_system_python(host):
    host.stubs(uv=True, uv_fails=True, pys=[("python3.12", "3.12")])
    host.release("fleet-fallbackfall")
    r = host.call("relay_host_install systemd fleet-fallbackfall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "python3.12 -m venv" in host.calls()


def test_relay_path_prepends_the_tool_directories_once(host):
    r = host.call('HERDR_RELAY_PATH_EXTRA="/x/one:/x/two"; relay_path; echo "$PATH"')
    assert r.stdout.strip().startswith("/x/one:/x/two:")
    r = host.call('HERDR_RELAY_PATH_EXTRA=""; relay_path; echo "$PATH"')
    assert not r.stdout.strip().startswith(":")


def test_render_systemd_quotes_the_exec_path_for_a_home_with_a_space(host, tmp_path):
    import shlex
    host.stubs()
    out = tmp_path / "out"
    r = host.call(f'relay_render "{SERVICE}" "{out}" "$HOME/.local/share/herdr-terminal-relay/current"')
    assert r.returncode == 0, r.stderr
    line = next(x for x in out.read_text().splitlines() if x.startswith("ExecStart="))
    argv = shlex.split(line.removeprefix("ExecStart="))               # systemd splits ExecStart like a shell for quotes
    assert argv == [f"{host.home}/.local/share/herdr-terminal-relay/current/venv/bin/herdr-eng", "host", "terminal-relay"]
    assert "@CURRENT@" not in out.read_text()


def test_render_escapes_sed_metacharacters_and_percent_signs(host, tmp_path):
    host.stubs()
    out = tmp_path / "out"
    r = host.call(f'relay_render "{SERVICE}" "{out}" "/home/a&b|c%d/current"')
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "/home/a&b|c%%d/current/venv/bin/herdr-eng" in text      # & and | literal, % doubled for systemd specifiers
    assert "@CURRENT@" not in text


def test_render_plist_uses_the_remote_home_and_escapes_xml(host, tmp_path):
    host.stubs()
    out = tmp_path / "out"
    r = host.call(f'relay_render "{PLIST}" "{out}" "/Users/a&b/current"')
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert f"{host.home}/Library/Logs/herdr-terminal-relay.log" in text and "/Users/" not in text.replace("/Users/a&amp;b", "")
    assert "<string>/Users/a&amp;b/current/venv/bin/herdr-eng</string>" in text
    assert "@HOME@" not in text and "@CURRENT@" not in text and "@USER@" not in text


def test_the_tracked_templates_use_only_the_release_placeholders():
    service, plist = SERVICE.read_text(), PLIST.read_text()
    assert 'ExecStart="@CURRENT@/venv/bin/herdr-eng" host terminal-relay' in service
    assert "@CURRENT@/venv/bin/herdr-eng" in plist and "@HOME@/Library/Logs/herdr-terminal-relay.log" in plist
    for text in (service, plist):
        assert "~/.local/bin" not in text and "%h/.local/bin" not in text


def test_install_builds_a_venv_switches_current_and_starts_the_unit(host):
    host.stubs(uv=True)
    rel = host.release("fleet-aaaaaaaaaaaa")
    r = host.call("relay_host_install systemd fleet-aaaaaaaaaaaa")
    assert r.returncode == 0, r.stdout + r.stderr
    base = host.home / ".local/share/herdr-terminal-relay"
    assert (base / "current").resolve() == rel.resolve()
    assert (rel / "venv/bin/herdr-eng").exists()
    unit = (host.home / ".config/systemd/user/herdr-terminal-relay.service").read_text()
    assert f'ExecStart="{base}/current/venv/bin/herdr-eng" host terminal-relay' in unit
    c = host.calls()
    order = [c.index(x) for x in ("systemctl --user daemon-reload", "systemctl --user enable", "systemctl --user restart")]
    assert order == sorted(order)
    assert "uv venv" in c and ">=3.12" in c and "uv pip install" in c


def test_install_on_macos_uses_bootout_bootstrap_and_kickstart_only(host):
    host.stubs(uv=True)
    host.release("fleet-bbbbbbbbbbbb", template=PLIST)
    r = host.call("relay_host_install launchd fleet-bbbbbbbbbbbb")
    assert r.returncode == 0, r.stdout + r.stderr
    plist = (host.home / "Library/LaunchAgents/com.herdr.terminal-relay.plist").read_text()
    assert f"{host.home}/Library/Logs/herdr-terminal-relay.log" in plist and "@HOME@" not in plist
    cmds = [x for x in host.calls().splitlines() if x.startswith("launchctl")]
    label = "gui/501/com.herdr.terminal-relay"
    assert cmds[0] == f"launchctl bootout {label}"
    assert cmds[1] == f"launchctl bootstrap gui/501 {host.home}/Library/LaunchAgents/com.herdr.terminal-relay.plist"
    assert not any(x.split()[1] in ("load", "unload") for x in cmds)


def test_a_macos_rollback_restarts_the_previous_release_with_kickstart(host):
    host.stubs(uv=True, connect="never")
    old = host.release("fleet-oldoldoldold", template=PLIST)
    host.release("fleet-newnewnewnew", template=PLIST)
    base = host.home / ".local/share/herdr-terminal-relay"
    (base / "current").symlink_to(old)
    r = host.call("relay_host_install launchd fleet-newnewnewnew")
    assert r.returncode != 0 and (base / "current").resolve() == old.resolve()
    assert "launchctl kickstart -k gui/501/com.herdr.terminal-relay" in host.calls()


def test_an_old_status_file_is_not_evidence(host):
    host.stubs(uv=True, connect="never")
    host.release("fleet-cccccccccccc")
    (host.state / "terminal-relay-status.json").write_text(
        f'{{"pid": {os.getpid()}, "started_at": 1, "connected_at": {int(__import__("time").time())}.5, "state": "connected"}}')
    r = host.call("relay_host_install systemd fleet-cccccccccccc")
    assert r.returncode != 0 and "status file" in r.stderr


def test_a_status_file_stamped_before_the_restart_is_not_evidence(host):
    host.stubs(uv=True, connect="old")                  # something rewrote the file, but with a time before the roll
    host.release("fleet-eeeeeeeeeeee")
    assert host.call("relay_host_install systemd fleet-eeeeeeeeeeee").returncode != 0


def test_a_crash_looping_job_whose_pid_is_dead_is_not_evidence(host):
    host.stubs(uv=True, connect="dead")
    host.release("fleet-ffffffffffff")
    assert host.call("relay_host_install systemd fleet-ffffffffffff").returncode != 0


def test_a_relay_that_never_connects_never_satisfies_the_check(host):
    host.stubs(uv=True, connect="never")                 # a 403-looping relay writes no status file
    host.release("fleet-gggggggggggg")
    assert host.call("relay_host_install systemd fleet-gggggggggggg").returncode != 0


def test_a_fresh_status_file_of_a_live_pid_is_evidence(host):
    host.stubs(uv=True, connect="ok")
    host.release("fleet-hhhhhhhhhhhh")
    r = host.call("relay_host_install systemd fleet-hhhhhhhhhhhh")
    assert r.returncode == 0, r.stderr
    assert "journalctl" not in host.calls()


def test_without_evidence_the_symlink_rolls_back_and_the_previous_release_restarts(host):
    host.stubs(uv=True, connect="never")
    old = host.release("fleet-oldoldoldold")
    new = host.release("fleet-newnewnewnew")
    base = host.home / ".local/share/herdr-terminal-relay"
    (base / "current").symlink_to(old)
    r = host.call("relay_host_install systemd fleet-newnewnewnew")
    assert r.returncode != 0
    assert (base / "current").resolve() == old.resolve() and new.exists()
    assert host.calls().count("systemctl --user restart") == 2
    assert "roll" in r.stderr.lower()


def test_a_first_install_without_evidence_stops_and_disables_the_unit(host):
    host.stubs(uv=True, connect="never")
    host.release("fleet-firstfirstfi")
    r = host.call("relay_host_install systemd fleet-firstfirstfi")
    assert r.returncode != 0
    assert not (host.home / ".local/share/herdr-terminal-relay/current").exists()
    assert "systemctl --user disable --now" in host.calls()


def test_rerunning_the_current_release_keeps_its_venv_and_never_rolls_back_to_itself(host):
    host.stubs(uv=True, connect="ok")
    rel = host.release("fleet-samesamesame")
    assert host.call("relay_host_install systemd fleet-samesamesame").returncode == 0
    marker = rel / "venv/bin/keep-me"
    marker.write_text("x")                                        # proves the venv is not rebuilt
    host.stubs(uv=True, connect="never")
    r = host.call("relay_host_install systemd fleet-samesamesame")
    assert marker.exists()
    assert r.returncode != 0
    base = host.home / ".local/share/herdr-terminal-relay"
    assert (base / "current").resolve() == rel.resolve()          # not "rolled back" anywhere
    assert "restored" not in r.stderr and "nothing to roll back to" in r.stderr
    assert host.calls().count("uv venv") == 1


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


def test_an_unknown_manager_is_refused_before_anything_is_touched(host):
    host.stubs(uv=True)
    host.release("fleet-iiiiiiiiiiii")
    r = host.call("relay_host_install systemdd fleet-iiiiiiiiiiii")
    assert r.returncode != 0 and "systemd or launchd" in r.stderr
    assert not (host.home / ".local/share/herdr-terminal-relay/current").exists() and host.calls() == ""
    assert host.call("relay_restart systemdd").returncode != 0


def test_linger_off_and_a_missing_herdr_only_warn(host):
    host.stubs(uv=True, linger="no", herdr=False)
    host.release("fleet-dddddddddddd")
    r = host.call("relay_host_install systemd fleet-dddddddddddd")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "linger" in r.stderr.lower() and "enable-linger" in r.stderr
    assert "herdr" in r.stderr and "herdr.sock" in r.stderr


def test_a_typo_in_the_hosts_file_manager_fails_that_host_only(tmp_path, rig):
    run, calls = rig
    hosts_file = tmp_path / "hosts.txt"
    hosts_file.write_text("alpha|alpha.example.test|systemdd\nbeta|beta.example.test|launchd\n")
    r = subprocess.run(["bash", str(SCRIPT), "--no-fetch", "--ref", "HEAD", "--with-terminal-relay"],
                       env={**os.environ, "HOME": str(tmp_path / "home"), "HERDR_FLEET_HOSTS": str(hosts_file),
                            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}, capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode != 0 and "systemd or launchd" in r.stderr
    assert "alpha.example.test" not in calls() and "beta.example.test" in calls()      # no ssh to the typo host


def test_only_terminal_relay_rolls_just_the_relay_and_implies_the_flag(rig):
    run, calls = rig
    r = run("--hosts", "alpha", "--only-terminal-relay")
    assert r.returncode == 0, r.stderr + r.stdout
    c = calls()
    assert "relay_host_install systemd fleet-" in c
    assert "install_native_hooks" not in c and "herdr-cp-feed-plugin" not in c and "herdr-service-metadata" not in c


def test_only_terminal_relay_dry_run_names_only_the_relay(rig):
    run, calls = rig
    r = run("--dry-run", "--only-terminal-relay", "--hosts", "alpha")
    assert r.returncode == 0 and "terminal relay release" in r.stdout and "native hooks" not in r.stdout
    assert calls() == ""


def test_a_relay_step_that_fails_before_the_install_marks_the_host_failed_and_the_run_goes_on(tmp_path, rig):
    run, calls = rig
    ssh = tmp_path / "bin" / "ssh"
    ssh.write_text(ssh.read_text().replace("exit 0\n", 'case "$last" in *"releases/fleet-"*) exit 1 ;; esac\nexit 0\n'))
    r = run("--only-terminal-relay")
    assert r.returncode != 0
    assert "== beta" in r.stdout and "beta.example.test" in calls()      # the second host is still attempted
