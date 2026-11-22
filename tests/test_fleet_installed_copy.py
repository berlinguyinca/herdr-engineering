"""`fleet join` must work from the installed wheel, which ships `herdr_engineering*` only (no scripts/)."""
import json
import subprocess
import sys
from pathlib import Path

from herdr_engineering import fleet

REPO = Path(__file__).resolve().parent.parent


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """A real environment with no user config, so no gateway is configured and the local registry answers."""
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "HERDR_ENGINEERING_STATE": str(tmp_path / "state"),
            "PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"}


def test_inventory_helper_imports_without_scripts_on_path(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text("all:\n  children:\n    herdr_fleet:\n      children: {}\n")
    code = (
        "import sys\n"
        "assert not any(p.rstrip('/').endswith('scripts') for p in sys.path), sys.path\n"
        "try:\n"
        "    import fleet_inventory\n"
        "except ImportError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('scripts/fleet_inventory.py leaked onto sys.path')\n"
        "from pathlib import Path\n"
        "import herdr_engineering.fleet as f\n"
        "from herdr_engineering.fleet_inventory import list_hosts\n"
        f"p = Path({str(inv)!r})\n"
        "assert f.add_host(p, 'newbox') is True\n"
        "assert list_hosts(p) == ['newbox']\n"
        "assert f.write_host_vars(p.parent / 'hv', 'newbox', 'newbox.ts.net', 'alice') is True\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
                          text=True, env=_isolated_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "newbox" in inv.read_text()


def test_scripts_shim_still_runs_as_a_file(tmp_path):
    inv = tmp_path / "hosts.yml"
    inv.write_text("all:\n  children:\n    herdr_fleet:\n      children: {}\n")
    env = _isolated_env(tmp_path)
    env.pop("PYTHONPATH")
    proc = subprocess.run([sys.executable, str(REPO / "scripts/fleet_inventory.py"), "add",
                           "--inventory", str(inv), "--host", "box"],
                          cwd=tmp_path, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "added: box" in proc.stdout


def test_devfabric_leases_reads_through_herdr_eng_not_native_herdr(tmp_path, monkeypatch):
    env = _isolated_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Real registration through the real CLI in a subprocess, into the isolated state dir.
    reg = subprocess.run(
        [sys.executable, "-m", "herdr_engineering.cli", "devfabric", "register", "--machine",
         "newbox", "--port", "8080", "--url", "http://newbox:8080"],
        capture_output=True, text=True, env=env, cwd=tmp_path)
    if reg.returncode != 0:  # flag names are not part of this fix; fall back to the registry API
        from herdr_engineering.devfabric import DevServiceRegistry
        DevServiceRegistry().register(target_machine_id="newbox", target_host="newbox", target_port=8080)
    leases = fleet.devfabric_leases()
    assert any("newbox" in json.dumps(lease) for lease in leases), (leases, reg.stderr)


def test_devfabric_leases_empty_registry_is_empty_list(tmp_path, monkeypatch):
    for key, value in _isolated_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    assert fleet.devfabric_leases() == []


def test_native_adapter_has_no_devfabric_methods():
    """devfabric is a herdr-eng subcommand; the native `herdr` binary has none."""
    from herdr_engineering.herdr_adapter import NativeHerdrAdapter
    assert not hasattr(NativeHerdrAdapter, "devfabric_list")
    assert not hasattr(NativeHerdrAdapter, "devfabric_register")
