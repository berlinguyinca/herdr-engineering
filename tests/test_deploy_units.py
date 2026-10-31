"""Every tracked systemd unit parses; new units take no Environment=/EnvironmentFile= (YAML config only); the
Architecture Explorer worker unit invokes a command line the real CLI accepts, configured from YAML."""
import argparse
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr_engineering.architecture import command

ROOT = Path(__file__).resolve().parents[1]
UNITS = sorted(ROOT / p for p in subprocess.run(
    ["git", "ls-files", "*.service", "*.timer"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split())
UNITS = sorted(set(UNITS) | set((ROOT / "deploy").rglob("*.service")) | set((ROOT / "deploy").rglob("*.timer")))
# Units that predate the "YAML config only, no environment variables" rule. Frozen: do not add to this list.
LEGACY_ENVIRONMENT = frozenset({
    "deploy/control-plane/herdr-ci-report-relay.service",   # PYTHONPATH + optional key env file (ci_report_relay)
    "deploy/herdr-cp-feed-plugin/herdr-cp-sessions.service",
    "deploy/mission-feed/herdr-eng-mission-feed.service",
    "deploy/mission-feed/herdr-service-metadata.service",
    "scripts/herdr-eng-build-cleanup.service",
    "scripts/herdr-eng-fleet-update.service",
})
ARCH_UNIT = ROOT / "deploy/control-plane/herdr-architecture-serve.service"


def parse_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Minimal systemd unit parser: [Section] headers, Key=Value lines, comments, backslash continuations."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    pending = ""
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = pending + raw.strip()
        pending = ""
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        key, sep, value = line.partition("=")
        assert sep and key.strip() and " " not in key.strip(), f"{path}:{n}: not Key=Value: {raw!r}"
        assert current is not None, f"{path}:{n}: setting before any [Section]"
        sections[current].append((key.strip(), value.strip()))
    assert not pending, f"{path}: dangling line continuation"
    return sections


def test_units_are_found():
    assert ARCH_UNIT in UNITS and len(UNITS) >= 10


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_unit_parses(unit):
    s = parse_unit(unit)
    assert "Unit" in s, f"{unit} has no [Unit]"
    if unit.suffix == ".service":
        assert any(k == "ExecStart" for k, _ in s.get("Service", [])), f"{unit} has no ExecStart"
    else:
        assert "Timer" in s, f"{unit} has no [Timer]"
        target = dict(s["Timer"]).get("Unit", unit.with_suffix(".service").name)
        assert (unit.parent / target).exists(), f"{unit} triggers missing {target}"


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: str(p.relative_to(ROOT)))
def test_units_read_no_environment(unit):
    rel = str(unit.relative_to(ROOT))
    keys = {k for sec in parse_unit(unit).values() for k, _ in sec}
    uses_env = bool(keys & {"Environment", "EnvironmentFile", "PassEnvironment"})
    if rel in LEGACY_ENVIRONMENT:
        return
    assert not uses_env, f"{rel}: configuration comes from YAML, never Environment=/EnvironmentFile="


def test_legacy_environment_list_is_not_stale():
    assert all((ROOT / p).exists() for p in LEGACY_ENVIRONMENT)


def test_architecture_serve_unit_is_a_hardened_long_running_worker():
    s = parse_unit(ARCH_UNIT)
    svc = dict(s["Service"])
    assert svc["Type"] == "simple" and svc["Restart"] == "on-failure"
    assert svc["NoNewPrivileges"] == "yes" and svc["UMask"] == "0077"
    assert not ARCH_UNIT.with_suffix(".timer").exists()          # serve is a loop, not a oneshot
    assert dict(s["Install"])["WantedBy"] == "default.target"
    argv = shlex.split(svc["ExecStart"])
    assert argv[0].endswith("/herdr-eng") and argv[1:3] == ["architecture", "serve"]
    parser = argparse.ArgumentParser()
    command.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv[1:])                          # the real CLI accepts the unit's command line
    assert args.func is command.cmd_serve and args.config.endswith("architecture-serve.yaml")
    assert args.url is None and args.api_key_file is None        # everything else comes from the YAML file


def _serve_args(**kw):
    base = dict(config=None, url=None, api_key_file=None, host_id=None, checkout=[], poll=None, timeout=None,
                cache_dir=None, once=True)
    return SimpleNamespace(**{**base, **kw})


def test_serve_config_yaml_example_resolves_and_flags_win(tmp_path):
    example = ROOT / "deploy/control-plane/architecture-serve.example.yaml"
    args = _serve_args(config=str(example))
    command.resolve_serve_config(args)
    home = str(Path.home())
    assert args.url == "http://dev.lan" and args.api_key_file.startswith(home)
    assert args.host_id == "beast" and (args.poll, args.timeout) == (5.0, 300.0)
    assert args.cache_dir == command.DEFAULT_CACHE
    assert args.checkout == [f"github.com/berlinguyinca/herdr-engineering={home}/IdeaProjects/herdr-engineering"]
    over = _serve_args(config=str(example), url="http://cp", host_id="h1", poll=1.0,
                       checkout=[f"github.com/berlinguyinca/herdr-engineering={tmp_path}"])
    command.resolve_serve_config(over)
    assert (over.url, over.host_id, over.poll) == ("http://cp", "h1", 1.0)
    assert over.checkout == [f"github.com/berlinguyinca/herdr-engineering={tmp_path}"]


@pytest.mark.parametrize("body,match", [("bogus: 1\n", "unknown key"), ("poll: -1\n", "poll must be"),
                                        ("checkouts: [a]\n", "checkouts must"), ("- x\n", "YAML mapping"),
                                        ("host_id: h1\n", "needs url and api_key_file")])
def test_serve_config_is_strict(tmp_path, body, match):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(body)
    with pytest.raises(SystemExit, match=match):
        command.resolve_serve_config(_serve_args(config=str(cfg)))


def test_serve_without_config_keeps_flag_defaults():
    args = _serve_args(url="http://cp", api_key_file="/k")
    command.resolve_serve_config(args)
    assert (args.poll, args.timeout, args.cache_dir) == (5.0, 300.0, command.DEFAULT_CACHE)


ANCHOR_DIR = ROOT / "deploy/control-plane"
ANCHOR_SERVICES = {"anchor": ANCHOR_DIR / "herdr-audit-anchor.service", "verify": ANCHOR_DIR / "herdr-audit-verify.service"}
ANCHOR_PATH = "/var/lib/herdr-anchor"
HARDENING = {"NoNewPrivileges": "yes", "ProtectSystem": "strict", "Restart": "no", "Type": "oneshot",
             "User": "herdr-anchor", "ProtectHome": "read-only", "PrivateTmp": "yes"}


@pytest.mark.parametrize("action", sorted(ANCHOR_SERVICES))
def test_audit_anchor_services_are_hardened_oneshots_that_run_the_real_cli(action):
    unit = ANCHOR_SERVICES[action]
    s = parse_unit(unit)
    svc = dict(s["Service"])
    for key, value in HARDENING.items():
        assert svc.get(key) == value, f"{unit.name}: {key}={value} required"
    assert svc.get("RestrictAddressFamilies", "").split() == ["AF_UNIX", "AF_INET", "AF_INET6"], \
        "AF_UNIX is needed for NSS / nscd name resolution"
    writable = [v for k, v in s["Service"] if k == "ReadWritePaths"]
    assert writable == ([ANCHOR_PATH] if action == "anchor" else []), "only the anchor service writes, and only its own dir"
    if action == "verify":
        assert ("ReadOnlyPaths", ANCHOR_PATH) in s["Service"]
    assert not any(k in {"Environment", "EnvironmentFile", "PassEnvironment"} for sec in s.values() for k, _ in sec)
    argv = shlex.split(svc["ExecStart"])
    assert argv[0].endswith("/herdr-eng") and argv[1:4] == ["control-plane", "assistant-audit", action]
    from herdr_engineering import cli
    args = cli._build_parser().parse_args(argv[1:])                    # the real CLI accepts the unit's command line
    assert args.action == action and args.api_key_file == "/etc/herdr/anchor/approver.key"
    assert args.fleet_config == "/etc/herdr/anchor/fleet-config.yaml" and "REPLACE" in args.control_plane
    assert "--api-key-file" in argv and not any("key=" in a or a.startswith("--api-key=") for a in argv)


@pytest.mark.parametrize("action,calendar", [("anchor", "*:0/15"), ("verify", "hourly")])
def test_audit_anchor_timers_trigger_their_service_on_schedule_and_are_never_enabled_by_the_repo(action, calendar):
    timer = ANCHOR_SERVICES[action].with_suffix(".timer")
    s = parse_unit(timer)
    assert dict(s["Timer"])["OnCalendar"] == calendar and dict(s["Timer"])["Unit"] == ANCHOR_SERVICES[action].name
    assert dict(s["Install"])["WantedBy"] == "timers.target"
    deploy_sh = (ROOT / "deploy/control-plane/deploy.sh").read_text()
    assert "herdr-audit" not in deploy_sh, "deploy.sh must not install or enable the anchor units"


def test_anchor_minimal_fleet_file_loads_with_the_real_loader(tmp_path):
    """The runbook's minimal fleet file (so the anchor user never reads the central one) is accepted by the CLI loader."""
    from herdr_engineering import cli
    minimal = tmp_path / "fleet-config.yaml"
    minimal.write_text("version: 1\nhosts: {}\nsections:\n  control_plane:\n    roles: [control-plane]\n    values:\n"
                       "      personal_assistant:\n        audit:\n          anchor_file: /var/lib/herdr-anchor/assistant-audit.anchor\n")
    minimal.chmod(0o600)                                              # the loader refuses a group/other-readable file
    _, cp = cli._load_cp_fleet_config(str(minimal))
    assert cp["personal_assistant"]["audit"]["anchor_file"] == f"{ANCHOR_PATH}/assistant-audit.anchor"
