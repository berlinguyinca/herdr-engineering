"""Real egress transactions with only root/Docker side effects stubbed."""

import fcntl
import json
import os

import pytest

from herdr_engineering import web_egress as policy
from herdr_engineering.web_egress_runtime import Transaction, private_json


@pytest.fixture
def runtime(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    lock = state / "rollout.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    current = state / "web-egress/current.json"
    attempt = state / "attempt-one/web-egress"
    events = []
    rules = []
    web = {
        "Id": "a" * 64,
        "Config": {
            "Labels": {
                "com.docker.compose.project": policy.PROJECT,
                "com.docker.compose.service": "control-plane-web",
            }
        },
        "NetworkSettings": {"Networks": {policy.DEFAULT: {"GwPriority": 0}}},
    }
    networks = []

    def inspect():
        return networks, [], web, [], [{"addr_info": [{"local": "100.104.39.6", "family": "inet"}]}]

    def execute(args):
        events.append(args)
        if args[4:] == ["status"]:
            return "Status: active\n"
        if args[:5] == ["sudo", "-n", "--", "/usr/sbin/ufw", "show"]:
            return "\n".join(rules)
        assert args[0] == "sudo" or args[:3] == ["docker", "network", "rm"]
        if args[0] == "sudo":
            if "delete" in args:
                if os.environ.get("TEST_EGRESS_DELETE_FAIL"):
                    raise RuntimeError("stub root delete failed")
                rules.clear()
            else:
                # A real UFW show-added equivalent, without running root.
                rules.append(" ".join(["ufw", *args[4:]]))
        else:
            assert not networks[0]["Containers"]
            networks.clear()
        return ""

    transaction = Transaction(
        current, attempt, lock_fd=fd, lock_path=lock, execute=execute, inspect=inspect
    )

    def attach():
        record = json.loads((attempt / "candidate.json").read_text())
        networks[:] = [
            {
                "Id": "c" * 64,
                "Name": policy.NETWORK,
                "Driver": "bridge",
                "Internal": False,
                "EnableIPv6": False,
                "Options": {"com.docker.network.bridge.name": policy.BRIDGE},
                "Labels": {**policy.LABELS, "io.herdr.web-egress.generation": record["generation"]},
                "IPAM": {"Config": [{"Subnet": policy.CIDR, "Gateway": "172.31.252.1"}]},
                "Containers": {web["Id"]: {"IPv4Address": "172.31.252.2/29"}},
            }
        ]
        web["NetworkSettings"]["Networks"][policy.NETWORK] = {
            "NetworkID": "c" * 64,
            "IPAddress": "172.31.252.2",
            "IPPrefixLen": 29,
            "Gateway": "172.31.252.1",
            "GwPriority": 100,
        }
        events.append(["compose", "candidate-with-egress"])

    yield transaction, events, rules, networks, web, attach
    os.close(fd)


def test_first_activation_grants_only_after_compose_and_profile(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    assert not rules
    attach()
    events.append(["profile", "candidate"])
    tx.after("forward")
    assert rules
    assert events.index(["profile", "candidate"]) < next(
        i for i, e in enumerate(events) if "allow" in e
    )
    assert json.loads((tx.attempt / "candidate.json").read_text())["network_id"] == "c" * 64
    tx.commit("forward")
    assert json.loads(tx.current.read_text())["enabled"] is True
    assert not tx.pending.exists()


def test_disabled_initially_has_no_root_or_network_effect(runtime):
    tx, events, *_ = runtime
    tx.snapshot(enabled=False, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    tx.after("forward")
    tx.commit("forward")
    assert events == []


def test_failed_forward_revokes_before_rollback_membership_and_clears_profile(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    tx.after("forward")
    tx.before("rollback")
    assert not rules
    events.append(["compose", "previous-without-egress"])
    del web["NetworkSettings"]["Networks"][policy.NETWORK]
    networks[0]["Containers"] = {}
    events.append(["profile", "previous-clear"])
    tx.after("rollback")
    tx.commit("rollback")
    deletion = next(i for i, e in enumerate(events) if "delete" in e)
    assert deletion < events.index(["compose", "previous-without-egress"])
    assert json.loads(tx.current.read_text()) == {"enabled": False}


def test_root_delete_failure_prevents_abandoning_network(runtime, monkeypatch):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    tx.after("forward")
    monkeypatch.setenv("TEST_EGRESS_DELETE_FAIL", "1")
    with pytest.raises(RuntimeError):
        tx.before("rollback")
    assert rules and networks[0]["Containers"]
    assert tx.pending.exists()
    assert not any(e[:3] == ["docker", "network", "rm"] for e in events)


def test_foreign_member_blocks_grant_and_network_removal(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    networks[0]["Containers"]["f" * 64] = {"Name": "share-public"}
    with pytest.raises(ValueError):
        tx.after("forward")
    assert not rules
    with pytest.raises(ValueError):
        tx.prune("candidate")
    assert not any(e[:3] == ["docker", "network", "rm"] for e in events)


def test_write_ahead_receipt_survives_interruption_and_recovery_revokes_only(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    tx.after("forward")
    replacement = Transaction(
        tx.current,
        tx.attempt,
        lock_fd=tx.lock_fd,
        lock_path=tx.lock_path,
        execute=tx.execute,
        inspect=tx.inspect,
    )
    replacement.recover()
    assert not rules
    assert networks[0]["Containers"]  # leave membership/image/profile to the same coordinator
    assert tx.pending.exists()  # not silently declared a completed rollback
    replacement.before("rollback")
    del web["NetworkSettings"]["Networks"][policy.NETWORK]
    networks[0]["Containers"] = {}
    replacement.after("rollback")
    replacement.commit("rollback")
    assert not tx.pending.exists()


def test_executor_requires_same_existing_rollout_lock(runtime, tmp_path):
    tx, *_ = runtime
    tx.lock_path = tmp_path / "different-lock"
    tx.lock_path.write_text("")
    with pytest.raises(ValueError):
        tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")


def test_tampered_rule_argv_is_never_executed(runtime):
    tx, events, *_ = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    path = tx.attempt / "candidate.json"
    record = json.loads(path.read_text())
    record["ufw_allow"] = ["ufw", "allow", "from", "172.16.0.0/12"]
    private_json(path, record)
    events.clear()
    with pytest.raises(ValueError):
        tx.before("forward")
    assert events == []


def test_prior_enabled_selector_and_profile_restore_as_one_transition(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    tx.after("forward")
    tx.commit("forward")
    first = json.loads(tx.current.read_text())
    later = Transaction(
        tx.current,
        tx.attempt.parent.parent / "attempt-two/web-egress",
        lock_fd=tx.lock_fd,
        lock_path=tx.lock_path,
        execute=tx.execute,
        inspect=tx.inspect,
    )
    later.snapshot(enabled=False, cidr=policy.CIDR, destination="100.104.39.6/32")
    later.before("forward")
    assert not rules
    events.append(["compose", "disabled"])
    del web["NetworkSettings"]["Networks"][policy.NETWORK]
    networks[0]["Containers"] = {}
    later.after("forward")
    later.before("rollback")
    web["NetworkSettings"]["Networks"][policy.NETWORK] = {
        "NetworkID": "c" * 64,
        "IPAddress": "172.31.252.2",
        "IPPrefixLen": 29,
        "Gateway": "172.31.252.1",
        "GwPriority": 100,
    }
    networks[0]["Containers"] = {web["Id"]: {"IPv4Address": "172.31.252.2/29"}}
    events.append(["compose", "previous-enabled"])
    events.append(["profile", "previous"])
    later.after("rollback")
    later.commit("rollback")
    assert rules
    assert json.loads(tx.current.read_text())["generation"] == first["generation"]
    assert (
        json.loads((later.attempt / "previous.json").read_text())["network_id"]
        == first["network_id"]
    )


def test_foreign_existing_rule_is_not_adopted_or_deleted(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    rules.append(
        "ufw allow in on br-herdr-cp-eg proto tcp from 172.31.252.0/29 to 100.104.39.6 port 443 comment foreign"
    )
    with pytest.raises(ValueError):
        tx.after("forward")
    assert not any("delete" in args for args in events)


def test_competing_rollout_lock_cannot_mutate_receipts_or_root(runtime):
    tx, events, *_ = runtime
    other = os.open(tx.lock_path, os.O_RDWR)
    original = tx.lock_fd
    tx.lock_fd = other
    try:
        with pytest.raises(BlockingIOError):
            tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
        assert events == []
        assert not tx.attempt.exists()
    finally:
        os.close(other)
        tx.lock_fd = original


def test_gateway_reassignment_refuses_grant_without_root(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    inspect = tx.inspect
    tx.inspect = lambda: (*inspect()[:4], [])
    events.clear()
    with pytest.raises(ValueError):
        tx.after("forward")
    assert not rules
    assert not any(e[0] == "sudo" for e in events)


def test_partial_root_add_failure_is_revoked_with_receipt_preserved(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    execute = tx.execute

    def fail_add(args):
        output = execute(args)
        if "allow" in args and "delete" not in args:
            raise RuntimeError("partial root command failure")
        return output

    tx.execute = fail_add
    with pytest.raises(RuntimeError):
        tx.after("forward")
    assert not rules
    assert tx.pending.exists()
    assert json.loads((tx.attempt / "candidate.json").read_text())["rule_may_exist"] is False


def test_cli_adopts_inherited_existing_lock_descriptor(tmp_path):
    import subprocess
    import sys

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    command = f'''set -euo pipefail
umask 077
exec 7>>"{state}/rollout.lock"
flock -n 7
"{sys.executable}" -m herdr_engineering.web_egress_runtime snapshot --current "{state}/web-egress/current.json" --attempt "{state}/attempt-test/web-egress" --lock-fd 7 --lock-path "{state}/rollout.lock"
"{sys.executable}" -m herdr_engineering.web_egress_runtime before --current "{state}/web-egress/current.json" --attempt "{state}/attempt-test/web-egress" --lock-fd 7 --lock-path "{state}/rollout.lock"
'''
    result = subprocess.run(["bash", "-c", command], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads((state / "attempt-test/web-egress/candidate.json").read_text()) == {
        "enabled": False
    }


@pytest.mark.parametrize("action", ["before", "after", "commit"])
def test_invalid_callback_direction_is_rejected_without_selection_or_root(
    runtime, action, monkeypatch
):
    tx, events, *_ = runtime

    def selected(*args):
        raise AssertionError("must reject before selecting receipt")

    monkeypatch.setattr(tx, "record", selected)
    with pytest.raises(ValueError):
        getattr(tx, action)("guess-previous")
    assert events == []
    assert not tx.attempt.exists()


@pytest.mark.parametrize("failure", ["missing", "sudo-password", "inactive"])
def test_readonly_root_capability_preflight_refuses_before_receipts(runtime, failure):
    tx, events, *_ = runtime

    def execute(args):
        events.append(args)
        if failure == "missing":
            raise FileNotFoundError("missing executable")
        if failure == "sudo-password":
            raise PermissionError("sudo needs password")
        return "Status: inactive\n"

    tx.execute = execute
    with pytest.raises((ValueError, OSError, RuntimeError)):
        tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    assert not tx.attempt.exists()
    assert all("allow" not in args and "delete" not in args for args in events)


def test_inactive_firewall_still_allows_disable_and_removes_persisted_rule(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    tx.after("forward")
    tx.commit("forward")
    execute = tx.execute

    def inactive(args):
        if args[4:] == ["status"]:
            return "Status: inactive\n"
        return execute(args)

    later = Transaction(
        tx.current,
        tx.attempt.parent.parent / "attempt-inactive/web-egress",
        lock_fd=tx.lock_fd,
        lock_path=tx.lock_path,
        execute=inactive,
        inspect=tx.inspect,
    )
    later.snapshot(enabled=False, cidr=policy.CIDR)
    later.before("forward")
    assert not rules
    assert any("delete" in args for args in events)
    del web["NetworkSettings"]["Networks"][policy.NETWORK]
    networks[0]["Containers"] = {}
    later.after("forward")
    later.commit("forward")
    assert json.loads(tx.current.read_text()) == {"enabled": False}


def test_inactive_recovery_revokes_and_never_regrants(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    tx.after("forward")
    execute = tx.execute

    def inactive(args):
        if args[4:] == ["status"]:
            return "Status: inactive\n"
        return execute(args)

    tx.execute = inactive
    tx.recover()
    assert not rules and networks[0]["Containers"]
    with pytest.raises(ValueError):
        tx.after("forward")
    assert not rules
    assert tx.pending.exists()


def test_after_requires_its_own_saved_pending_transition(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    attach()
    events.clear()
    with pytest.raises(ValueError):
        tx.after("forward")
    assert not rules and not events


def test_before_never_overwrites_another_attempts_journal(runtime):
    tx, events, *_ = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    foreign = {
        "attempt": str(tx.attempt.parent.parent / "attempt-other/web-egress"),
        "direction": "forward",
    }
    private_json(tx.pending, foreign)
    events.clear()
    with pytest.raises(ValueError):
        tx.before("forward")
    assert json.loads(tx.pending.read_text()) == foreign
    assert events == []


def test_prune_rechecks_bridge_and_cidr_before_removing_empty_owned_network(runtime):
    tx, events, rules, networks, web, attach = runtime
    tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    tx.before("forward")
    attach()
    tx.after("forward")
    tx.before("rollback")
    del web["NetworkSettings"]["Networks"][policy.NETWORK]
    networks[0]["Containers"] = {}
    networks[0]["Options"]["com.docker.network.bridge.name"] = "br-other"
    with pytest.raises(ValueError):
        tx.prune("candidate")
    assert not any(args[:3] == ["docker", "network", "rm"] for args in events)


def test_preexisting_unowned_rule_refuses_before_snapshot_or_compose(runtime):
    tx, events, rules, *_ = runtime
    rules.append(
        "ufw allow in on br-herdr-cp-eg proto tcp from 172.31.252.0/29 to 100.104.39.6 port 443 comment foreign"
    )
    with pytest.raises(ValueError):
        tx.snapshot(enabled=True, cidr=policy.CIDR, destination="100.104.39.6/32")
    assert not tx.attempt.exists()
    assert not any("delete" in command or "allow" in command for command in events)
