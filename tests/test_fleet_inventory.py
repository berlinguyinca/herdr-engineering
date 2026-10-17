"""Tests for the private fleet-inventory helper (scripts/fleet_inventory.py)."""
from pathlib import Path

from scripts.fleet_inventory import add_host, list_hosts, remove_host, write_host_vars

BASE = """# Private fleet inventory
all:
  children:
    herdr_fleet:
      children:
        linux:
          hosts:
            fry: {}
            beast: {}
        macos:
          hosts:
            mac: {}
"""


def test_add_host_under_group(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text(BASE)
    assert add_host(inv, "newbox", "linux") is True
    assert "newbox" in list_hosts(inv)
    # idempotent
    assert add_host(inv, "newbox", "linux") is False
    assert list_hosts(inv).count("newbox") == 1


def test_add_host_macos_group(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text(BASE)
    assert add_host(inv, "macbook", "macos") is True
    assert "macbook" in list_hosts(inv)


def test_add_host_creates_new_group(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text(BASE)
    assert add_host(inv, "edge", "linux") is True  # existing group
    # A brand-new OS group is created on demand.
    assert add_host(inv, "pi", "linux") is True
    assert "pi" in list_hosts(inv)


def test_list_hosts(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text(BASE)
    hosts = list_hosts(inv)
    assert set(hosts) == {"fry", "beast", "mac"}


def test_remove_host(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text(BASE)
    assert remove_host(inv, "fry") is True
    assert set(list_hosts(inv)) == {"beast", "mac"}
    # removing an absent host is a no-op (returns False, unchanged)
    assert remove_host(inv, "fry") is False
    assert set(list_hosts(inv)) == {"beast", "mac"}
    # removing from a different group still works (macos)
    assert remove_host(inv, "mac") is True
    assert list_hosts(inv) == ["beast"]


def test_write_host_vars(tmp_path):
    d = tmp_path / "host_vars"
    assert write_host_vars(d, "newbox", "newbox.tail.ts.net", "alice") is True
    p = d / "newbox.yml"
    assert p.exists()
    assert "ansible_host: newbox.tail.ts.net" in p.read_text()
    # idempotent
    assert write_host_vars(d, "newbox", "other", "bob") is False
    assert "alice" in p.read_text()
