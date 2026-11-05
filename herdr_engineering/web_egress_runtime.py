"""Egress callbacks for the existing deploy transaction; no second coordinator.

Only this opt-in executor runs UFW. Every mutating action needs the inherited
rollout lock. Receipts are write-ahead, private and validated before execution.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from . import web_egress as p


def private_dir(path):
    path = Path(path)
    if not path.exists():
        private_dir(path.parent)
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("unsafe state directory")
    # Existing deployment directories are already protected; do not chmod callers' parents.


def private_json(path, value):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("unsafe receipt")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".egress-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise ValueError("unsafe receipt")
        with os.fdopen(fd, "r", closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(fd)


def make_record(cidr, destination, generation=None):
    cidr = str(p._cidr(cidr))
    destination = p.gateway(destination)
    allow = [
        "ufw",
        "allow",
        "in",
        "on",
        p.BRIDGE,
        "proto",
        "tcp",
        "from",
        cidr,
        "to",
        destination,
        "port",
        "443",
        "comment",
        p.COMMENT,
    ]
    return {
        "enabled": True,
        "cidr": cidr,
        "destination": destination,
        "generation": generation or secrets.token_hex(16),
        "network_id": None,
        "rule_may_exist": False,
        "ufw_allow": allow,
        "ufw_delete": ["ufw", "delete", *allow[1:]],
    }


def validate(record):
    if record == {"enabled": False}:
        return record
    if not isinstance(record, dict) or record.get("enabled") is not True:
        raise ValueError("invalid receipt")
    expected = make_record(record["cidr"], record["destination"], record["generation"])
    if (
        set(record) != set(expected)
        or not re.fullmatch(r"[0-9a-f]{32}", record["generation"])
        or record["ufw_allow"] != expected["ufw_allow"]
        or record["ufw_delete"] != expected["ufw_delete"]
        or type(record["rule_may_exist"]) is not bool
        or (
            record["network_id"] is not None
            and not re.fullmatch(r"[0-9a-f]{64}", record["network_id"])
        )
    ):
        raise ValueError("invalid receipt")
    return record


def execute(command):
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


class RootPreflightError(ValueError):
    pass


def root_preflight(run, *, active_required=True):
    """Read-only capability check; never enable UFW or add/delete permissions."""
    try:
        status = run(["status"])
        lines = {line.strip() for line in status.splitlines()}
        if not lines.intersection({"Status: active", "Status: inactive"}):
            raise RootPreflightError("unsupported UFW status")
        if active_required and "Status: active" not in lines:
            raise RootPreflightError("active UFW required")
        return run(["show", "added"])
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise RootPreflightError("noninteractive sudo/UFW required") from error


def selected_name(direction):
    if direction not in ("forward", "rollback"):
        raise ValueError("invalid callback direction")
    return "candidate" if direction == "forward" else "previous"


def matching_rules(output, record):
    matches = []
    for line in output.splitlines():
        tokens = shlex.split(line)
        if not tokens or tokens[0] != "ufw":
            continue
        try:
            same = (
                tokens[tokens.index("on") + 1] == p.BRIDGE
                and tokens[tokens.index("proto") + 1] == "tcp"
                and str(p._cidr(tokens[tokens.index("from") + 1])) == record["cidr"]
                and str(ipaddress.ip_network(tokens[tokens.index("to") + 1]))
                == record["destination"]
                and tokens[tokens.index("port") + 1] == "443"
            )
        except (ValueError, IndexError):
            continue
        if same:
            matches.append(tokens)
    return matches


def refuse_rule_collision(candidate, previous, output):
    for rule in matching_rules(output, candidate):
        if (
            not previous.get("enabled")
            or not previous.get("rule_may_exist")
            or previous.get("ufw_allow") != candidate["ufw_allow"]
            or "comment" not in rule
            or rule[rule.index("comment") + 1] != p.COMMENT
        ):
            raise ValueError("unowned existing firewall rule")


class Transaction:
    def __init__(self, current, attempt, *, lock_fd, lock_path, execute=execute, inspect=p.collect):
        self.current, self.attempt = Path(current), Path(attempt)
        self.pending = self.current.parent / "pending.json"
        self.lock_fd, self.lock_path = lock_fd, Path(lock_path)
        self.execute, self.inspect = execute, inspect

    def locked(self):
        opened, named = os.fstat(self.lock_fd), self.lock_path.lstat()
        state = self.lock_path.parent
        info = state.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or self.current.parent.parent.resolve() != state.resolve()
            or self.attempt.parent.parent.resolve() != state.resolve()
        ):
            raise ValueError("receipt must belong to this protected deployment state")
        if (
            not stat.S_ISREG(named.st_mode)
            or opened.st_ino != named.st_ino
            or opened.st_dev != named.st_dev
            or named.st_uid != os.geteuid()
            or named.st_mode & 0o077
        ):
            raise ValueError("existing rollout lock required")
        # Adopt the same inherited open-file description, never unlock it here.
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def record(self, name):
        return validate(read_json(self.attempt / (name + ".json")))

    def save(self, name, record):
        private_json(self.attempt / (name + ".json"), validate(record))

    def snapshot(self, *, enabled, cidr, destination=None):
        self.locked()
        if self.pending.exists():
            raise ValueError("interrupted egress attempt requires recovery")
        previous = validate(read_json(self.current, {"enabled": False}))
        candidate = {"enabled": False}
        if enabled:
            if previous["enabled"] and previous["cidr"] != cidr:
                raise ValueError("disable the old network before changing its subnet")
            candidate = make_record(cidr, destination, previous.get("generation"))
            candidate["network_id"] = previous.get("network_id")
            self.check(candidate, attached=False)
        if enabled or previous["enabled"]:
            added = root_preflight(self.root, active_required=enabled)
            refuse_rule_collision(candidate if enabled else previous, previous, added)
        self.save("previous", previous)
        self.save("candidate", candidate)

    def check(self, record, *, attached, discover=False):
        networks, routes, web, interfaces, addresses = self.inspect()
        matching = [item for item in networks if item.get("Name") == p.NETWORK]
        if matching:
            network = matching[0]
            if (
                network.get("Labels", {}).get("io.herdr.web-egress.generation")
                != record["generation"]
            ):
                raise ValueError("foreign network generation")
            if record["network_id"] is None and discover:
                record["network_id"] = network["Id"]
        return p.policy(
            enabled=True,
            cidr=record["cidr"],
            destination=record["destination"],
            addresses=addresses,
            networks=networks,
            routes=routes,
            web=web,
            interfaces=interfaces,
            expected_network_id=record["network_id"],
            require_attached=attached,
        )

    def root(self, args):
        self.locked()
        return self.execute(["sudo", "-n", "--", "/usr/sbin/ufw", *args])

    def rules(self, record):
        return matching_rules(self.root(["show", "added"]), record)

    def revoke(self, record):
        if not record["enabled"] or not record["rule_may_exist"]:
            return
        for rule in self.rules(record):
            if "comment" not in rule or rule[rule.index("comment") + 1] != p.COMMENT:
                raise ValueError("foreign rule collision; refusing deletion")
            self.root(["--force", *record["ufw_delete"][1:]])
        record["rule_may_exist"] = False

    def pending_transition(self, direction, *, required):
        journal = read_json(self.pending)
        if journal is None:
            if required:
                raise ValueError("no saved transition for this callback")
            return
        if set(journal) != {"attempt", "direction"} or journal["attempt"] != str(
            self.attempt.resolve()
        ):
            raise ValueError("another attempt owns the pending transition")
        selected_name(journal["direction"])
        if required and journal["direction"] != direction:
            raise ValueError("callback does not match saved transition")

    def before(self, direction):
        name = selected_name(direction)
        self.locked()
        self.pending_transition(direction, required=False)
        previous, candidate = self.record("previous"), self.record("candidate")
        if not previous["enabled"] and not candidate["enabled"]:
            return
        if (
            candidate["enabled"]
            or candidate.get("rule_may_exist")
            or previous.get("rule_may_exist")
        ):
            root_preflight(
                self.root, active_required=direction == "forward" and candidate["enabled"]
            )
        private_json(self.pending, {"attempt": str(self.attempt.resolve()), "direction": direction})
        # Permission is removed before the caller recreates or abandons membership.
        self.revoke(candidate)
        self.revoke(previous)
        self.save("candidate", candidate)
        self.save("previous", previous)
        target = candidate if name == "candidate" else previous
        if target["enabled"]:
            self.check(target, attached=False)

    def after(self, direction):
        name = selected_name(direction)
        self.locked()
        record = self.record(name)
        if not record["enabled"]:
            return
        self.pending_transition(direction, required=True)
        self.check(record, attached=True, discover=True)
        root_preflight(self.root)
        self.save(name, record)  # immutable network ID recorded before granting anything
        matches = self.rules(record)
        if matches and not record["rule_may_exist"]:
            raise ValueError("unowned existing rule")
        if any(
            "comment" not in rule or rule[rule.index("comment") + 1] != p.COMMENT
            for rule in matches
        ):
            raise ValueError("foreign rule collision")
        record["rule_may_exist"] = True
        self.save(name, record)  # write-ahead: even an interrupted root command is recoverable
        try:
            self.root(record["ufw_allow"][1:])
            self.check(record, attached=True)
        except Exception:
            self.revoke(record)
            self.save(name, record)
            raise

    def commit(self, direction):
        name = selected_name(direction)
        self.locked()
        selected = self.record(name)
        if (
            selected["enabled"]
            or self.record("previous" if name == "candidate" else "candidate")["enabled"]
        ):
            self.pending_transition(direction, required=True)
        if selected["enabled"]:
            self.check(selected, attached=True)
        private_json(self.current, selected)
        if self.pending.exists():
            self.pending.unlink()

    def prune(self, name):
        if name not in ("candidate", "previous"):
            raise ValueError("invalid receipt selector")
        self.locked()
        record = self.record(name)
        if not record["enabled"]:
            return
        self.revoke(record)
        networks, _, _, _, _ = self.inspect()
        if not record["network_id"]:
            possible = [
                net
                for net in networks
                if net.get("Name") == p.NETWORK
                and net.get("Labels", {}).get("io.herdr.web-egress.generation")
                == record["generation"]
            ]
            if not possible:
                return
            record["network_id"] = possible[0]["Id"]
        matches = [net for net in networks if net.get("Id") == record["network_id"]]
        if not matches:
            return
        network = matches[0]
        if (
            network.get("Driver") != "bridge"
            or network.get("Internal") is not False
            or network.get("EnableIPv6") is not False
            or (network.get("Options") or {}).get("com.docker.network.bridge.name") != p.BRIDGE
            or network.get("IPAM", {}).get("Config")
            != [
                {
                    "Subnet": record["cidr"],
                    "Gateway": str(p._cidr(record["cidr"]).network_address + 1),
                }
            ]
            or network.get("Name") != p.NETWORK
            or network.get("Labels", {}).get("io.herdr.web-egress.generation")
            != record["generation"]
            or any(network.get("Labels", {}).get(key) != value for key, value in p.LABELS.items())
            or network.get("Containers")
        ):
            raise ValueError("cannot remove foreign or occupied network")
        self.execute(["docker", "network", "rm", record["network_id"]])

    def recover(self):
        self.locked()
        pending = read_json(self.pending)
        if pending is None:
            return
        selected_name(pending.get("direction"))
        if pending.get("attempt") != str(self.attempt.resolve()):
            raise ValueError("wrong recovery attempt")
        # Revoke only. The same coordinator must restore its saved image/config/profile,
        # then call before/after/commit rollback. Never call this a completed rollback.
        root_preflight(self.root, active_required=False)
        self.revoke(self.record("candidate"))
        self.revoke(self.record("previous"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "preview",
            "snapshot",
            "before",
            "after",
            "commit",
            "prune",
            "recover",
            "selector",
        ],
    )
    parser.add_argument("--current", required=True)
    parser.add_argument("--attempt")
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--cidr", default=p.CIDR)
    parser.add_argument("--gateway-ip")
    parser.add_argument("--direction", choices=["forward", "rollback"], default="forward")
    parser.add_argument("--record", choices=["candidate", "previous"], default="candidate")
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--lock-path")
    args = parser.parse_args(argv)
    try:
        if args.action == "preview":
            previous = validate(read_json(args.current, {"enabled": False}))
            if Path(args.current).with_name("pending.json").exists():
                raise ValueError("pending recovery")
            if not args.enabled:
                if previous["enabled"]:
                    added = root_preflight(
                        lambda command: execute(["sudo", "-n", "--", "/usr/sbin/ufw", *command]),
                        active_required=False,
                    )
                    refuse_rule_collision(previous, previous, added)
                print(json.dumps({"enabled": False, "managed": previous["enabled"]}))
                return 0
            if sys.platform != "linux":
                print(
                    "Private egress enabled preflight requires a Linux host; no changes made.",
                    file=sys.stderr,
                )
                return 1
            p.gateway(args.gateway_ip)
            p._cidr(args.cidr)
            network_id = previous.get("network_id")
            networks, routes, web, interfaces, addresses = p.collect()
            p.policy(
                enabled=True,
                cidr=args.cidr,
                destination=args.gateway_ip,
                addresses=addresses,
                networks=networks,
                routes=routes,
                web=web,
                interfaces=interfaces,
                expected_network_id=network_id,
            )
            root_preflight(lambda command: execute(["sudo", "-n", "--", "/usr/sbin/ufw", *command]))
            print(json.dumps({"enabled": True, "managed": True}))
            return 0
        transaction = Transaction(
            args.current, args.attempt, lock_fd=args.lock_fd, lock_path=args.lock_path
        )
        if args.action == "selector":
            selected = transaction.record(
                "candidate" if args.direction == "forward" else "previous"
            )
            print(json.dumps(selected))
            return 0
        if args.action == "snapshot":
            transaction.snapshot(enabled=args.enabled, cidr=args.cidr, destination=args.gateway_ip)
        elif args.action in ("before", "after", "commit"):
            getattr(transaction, args.action)(args.direction)
        elif args.action == "prune":
            transaction.prune(args.record)
        else:
            transaction.recover()
        return 0
    except RootPreflightError as error:
        print(f"Private egress preflight refused: {error}; no changes made.", file=sys.stderr)
        return 1
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        AttributeError,
        subprocess.SubprocessError,
        RuntimeError,
    ):
        print(
            "Private egress transaction failed; preserve its receipt and network state.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
