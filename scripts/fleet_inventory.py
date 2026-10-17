#!/usr/bin/env python3
"""Mutate the PRIVATE HerdR fleet inventory (ansible/inventory/hosts.yml).

The inventory is machine-local and gitignored (each host self-enrolls via
scripts/install.sh; the controller keeps the union). This helper adds/list
hosts in the same structure install.sh writes, so the controller can onboard
a new site and then manage it with the fleet-sync playbook.

Usage:
  fleet_inventory.py add   --inventory PATH --host NAME [--os linux|macos]
  fleet_inventory.py list  --inventory PATH
"""
import argparse
import sys
from pathlib import Path

import yaml


def _ensure_tree(data):
    """Return all.children.herdr_fleet.children (creating empty structure)."""
    all_ = data.setdefault("all", {})
    children = all_.setdefault("children", {})
    fleet = children.setdefault("herdr_fleet", {})
    return fleet.setdefault("children", {})


def add_host(inventory: Path, host: str, os_name: str = "linux") -> bool:
    """Add `host` under the linux/macos group. Returns True if added."""
    with inventory.open() as fh:
        data = yaml.safe_load(fh) or {}
    children = _ensure_tree(data)
    group = children.setdefault(os_name, {})
    hosts = group.setdefault("hosts", {})
    if host in hosts:
        return False
    hosts[host] = {}
    _write(inventory, data)
    return True


def remove_host(inventory: Path, host: str) -> bool:
    """Remove `host` from any fleet group. Returns True if it was present.

    Leaves any private host_vars/<host>.yml in place so re-joining later is
    trivial (connection details are preserved).
    """
    with inventory.open() as fh:
        data = yaml.safe_load(fh) or {}
    removed = False
    for group in data.get("all", {}).get("children", {}).get("herdr_fleet", {}).get("children", {}).values():
        hosts = (group.get("hosts") or {})
        if host in hosts:
            del hosts[host]
            removed = True
    if removed:
        _write(inventory, data)
    return removed


def list_hosts(inventory: Path) -> list[str]:
    with inventory.open() as fh:
        data = yaml.safe_load(fh) or {}
    children = data.get("all", {}).get("children", {}).get("herdr_fleet", {}).get("children", {})
    out: list[str] = []
    for group in children.values():
        out.extend((group.get("hosts") or {}).keys())
    return out


def write_host_vars(host_vars_dir: Path, host: str, ansible_host: str,
                    user: str, interpreter: str = "/usr/bin/python3") -> bool:
    """Write a private host_vars/<host>.yml if absent. Returns True if written."""
    path = host_vars_dir / f"{host}.yml"
    if path.exists():
        return False
    host_vars_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Private connection details for the fleet host `%s` (tailnet MagicDNS).\n"
        "# Not committed (gitignored). No secrets here.\n"
        "ansible_host: %s\n"
        "ansible_user: %s\n"
        "ansible_python_interpreter: %s\n" % (host, ansible_host, user, interpreter)
    )
    return True


def _write(inventory: Path, data) -> None:
    inventory.write_text("# Private fleet inventory (managed by scripts/fleet_inventory.py; NOT committed).\n"
                         "# Connection users/addresses go in group_vars or ansible_* vars, never here.\n"
                         + yaml.safe_dump(data, sort_keys=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fleet_inventory.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("remove")
    r.add_argument("--inventory", required=True)
    r.add_argument("--host", required=True)

    a = sub.add_parser("add")
    a.add_argument("--inventory", required=True)
    a.add_argument("--host", required=True)
    a.add_argument("--os", default="linux", choices=("linux", "macos"))

    l = sub.add_parser("list")
    l.add_argument("--inventory", required=True)

    v = sub.add_parser("host-var")
    v.add_argument("--dir", required=True)
    v.add_argument("--host", required=True)
    v.add_argument("--ansible-host", required=True)
    v.add_argument("--user", required=True)
    v.add_argument("--interpreter", default="/usr/bin/python3")

    args = ap.parse_args(argv)
    if args.cmd == "remove":
        removed = remove_host(Path(args.inventory), args.host)
        print(f"{'removed' if removed else 'not present'}: {args.host}")
        return 0
    if args.cmd == "add":
        added = add_host(Path(args.inventory), args.host, args.os)
        print(f"{'added' if added else 'already present'}: {args.host}")
        return 0
    if args.cmd == "list":
        for h in list_hosts(Path(args.inventory)):
            print(h)
        return 0
    if args.cmd == "host-var":
        written = write_host_vars(Path(args.dir), args.host, args.ansible_host,
                                  args.user, args.interpreter)
        print(f"{'wrote' if written else 'already exists'}: {args.host}.yml")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
