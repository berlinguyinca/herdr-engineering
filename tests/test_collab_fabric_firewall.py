"""deploy/control-plane/collab-fabric-firewall.sh: the DOCKER-USER/INPUT rules that keep the fabric network's containers
from opening connections. Real iptables needs root, so the script is driven with a tiny iptables stand-in that keeps its rule
table in a file and speaks the same -C/-I/-D/-S exit codes (0 present, 1 absent, 4 not permitted); nothing touches the host."""
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"
SCRIPT = CP / "collab-fabric-firewall.sh"


@pytest.fixture
def ipt(tmp_path):
    table = tmp_path / "table"
    table.write_text("")
    stub = tmp_path / "iptables"
    stub.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        [ -z "${{STUB_DENIED:-}}" ] || {{ echo "iptables: Permission denied (you must be root)" >&2; exit 4; }}
        op="$1"; chain="$2"; shift 2
        if [ "$op" = -I ]; then shift; fi        # -I <chain> <pos> <rule...>
        line="$chain $*"
        case "$op" in
          -C) grep -qxF -- "$line" {table} ;;
          -I) echo "$line" >> {table} ;;
          -D) grep -vxF -- "$line" {table} > {table}.new; cp {table}.new {table} ;;
          *) exit 2 ;;
        esac
        """))
    stub.chmod(0o755)
    return stub, table


def run(ipt_stub, *args, env=None):
    import os
    return subprocess.run(["bash", str(SCRIPT), "--iptables", str(ipt_stub), *args], capture_output=True, text=True,
                          timeout=30, env={**os.environ, **(env or {})})


def test_script_is_valid_bash_and_executable():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert SCRIPT.stat().st_mode & 0o111


def test_default_subnet_is_the_composes_fixed_fabric_subnet():
    doc = yaml.safe_load((CP / "compose.collab-fabric.yaml").read_text())
    subnet = doc["networks"]["fabric"]["ipam"]["config"][0]["subnet"]
    assert f"--subnet CIDR (default {subnet})" in subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True,
                                                                  text=True).stdout


def test_check_reports_absent_then_present_after_install_and_install_is_idempotent(ipt):
    stub, table = ipt
    r = run(stub, "--check")
    assert r.returncode == 1 and "absent" in r.stdout
    assert run(stub, "--install").returncode == 0
    rules = table.read_text().splitlines()
    assert len(rules) == 2 and {x.split()[0] for x in rules} == {"DOCKER-USER", "INPUT"}
    assert all("-s 172.30.213.0/24" in x and "NEW" in x and x.endswith("-j DROP") for x in rules)
    r = run(stub, "--check")
    assert r.returncode == 0 and "present" in r.stdout and "absent" not in r.stdout
    assert run(stub, "--install").returncode == 0
    assert len(table.read_text().splitlines()) == 2                      # nothing doubled


def test_one_missing_rule_is_reported_absent(ipt):
    stub, table = ipt
    run(stub, "--install")
    table.write_text("".join(x + "\n" for x in table.read_text().splitlines() if x.startswith("INPUT")))
    r = run(stub, "--check")
    assert r.returncode == 1 and "DOCKER-USER: absent" in r.stdout and "INPUT: present" in r.stdout


def test_remove_deletes_the_rules_and_is_idempotent(ipt):
    stub, table = ipt
    run(stub, "--install")
    assert run(stub, "--remove").returncode == 0
    assert table.read_text() == ""
    assert run(stub, "--remove").returncode == 0
    assert run(stub, "--check").returncode == 1


def test_dry_run_prints_the_commands_and_changes_nothing(ipt):
    stub, table = ipt
    r = run(stub, "--install", "--dry-run", "--subnet", "10.9.8.0/24")
    assert r.returncode == 0 and "-I DOCKER-USER 1" in r.stdout and "-I INPUT 1" in r.stdout and "10.9.8.0/24" in r.stdout
    assert table.read_text() == ""


def test_not_being_allowed_to_read_the_rules_is_unknown_not_absent(ipt):
    stub, _ = ipt
    r = run(stub, "--check", env={"STUB_DENIED": "1"})
    assert r.returncode == 2 and "cannot" in r.stderr + r.stdout


def test_bad_arguments_are_refused(ipt):
    stub, _ = ipt
    assert run(stub, "--subnet", "not-a-cidr", "--check").returncode == 64
    assert run(stub).returncode == 64                                   # a mode is required
    assert run(stub, "--install", "--remove").returncode == 64
