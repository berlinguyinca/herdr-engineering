import copy
import gc
import importlib.util
import sys
from pathlib import Path

import pytest

from herdr_engineering import host_config as hc


@pytest.fixture
def acknowledged_sink():
    """Shared across control-plane and domain selections regardless of ordering."""
    class AcknowledgedSink:
        def __init__(self):
            self.events = []

        def record(self, event):
            self.record_required(event)

        def record_required(self, event):
            self.events.append(copy.deepcopy(event))

    return AcknowledgedSink()


@pytest.fixture(autouse=True)
def _restore_gc_state():
    """In-process web starts call tune_gc (freeze + thresholds); never let that leak into later tests."""
    saved = gc.get_threshold()
    gc.unfreeze()
    yield
    gc.set_threshold(*saved)
    gc.unfreeze()


@pytest.fixture(autouse=True)
def _no_real_host_config(monkeypatch):
    """Tests never read the developer's real host.json or reach a control plane."""
    hc.reset_host_config()

    def _unavailable():
        raise hc.HostConfigUnavailable("no host configuration in tests", permanent=True)

    monkeypatch.setattr(hc, "host_config", _unavailable)
    yield
    hc.reset_host_config()


@pytest.fixture
def fake_host_config(monkeypatch):
    """fake_host_config({"ci": {"url": ...}}, control_plane_url=...) installs a HostConfig."""
    def install(values=None, *, control_plane_url="http://cp.test", host_id="testhost",
                revision="rev1"):
        cfg = hc.HostConfig(control_plane_url, host_id, revision, values or {})
        monkeypatch.setattr(hc, "host_config", lambda: cfg)
        return cfg
    return install


def pytest_addoption(parser):
    parser.addoption("--allow-fabric-http-skip", action="store_true", default=False,
                     help="skip (instead of fail) the Fabric HTTP tests on a host with no non-loopback address")


@pytest.fixture(autouse=True)
def _disk_guard_override_by_default(monkeypatch):
    """A nearly full dev disk must not refuse builds in unrelated tests; guard tests unset this."""
    monkeypatch.setenv("HERDR_DISK_GUARD_OVERRIDE", "1")


def _flaky_plugin():
    """The quarantine / failed-test-list plugin of the unit gate (scripts/ci/herdr_flaky_plugin.py), loaded by path so a plain
    ``pytest`` run quarantines exactly like the gate does. The gate also passes ``-p herdr_flaky_plugin``; one registration wins."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "herdr_flaky_plugin.py"
    spec = importlib.util.spec_from_file_location("herdr_flaky_plugin", path)
    module = sys.modules.get("herdr_flaky_plugin") or importlib.util.module_from_spec(spec)
    if "herdr_flaky_plugin" not in sys.modules:
        sys.modules["herdr_flaky_plugin"] = module
        spec.loader.exec_module(module)
    return module


def pytest_configure(config):
    _flaky_plugin().register(config)


_GUARDED = ("test_side_pane_", "test_handoff_controller_")
_RIG_MARKERS = ("herdr_server", "herdr_live")


@pytest.fixture(autouse=True)
def _no_real_herdr(request, tmp_path_factory, monkeypatch):
    """Side-pane and handoff-controller tests that are not marked `herdr_server` / `herdr_live` can never reach a real Herdr.

    The guard is a tripwire, not a stand-in: an executable `herdr` first on PATH writes its argv to a file and exits 97, and
    the test FAILS at teardown if the file exists (so a test that catches the failure cannot hide the call). Beside it:
    every HERDR_* variable (socket, pane, tab, binary) is removed, and HOME, the engineering state and the handoff store
    point into a temporary directory. A tripwire was chosen over a PATH without `herdr` because a test that builds a
    `HerdrCli()` or `SideHerdr()` from os.environ is then caught and named, instead of failing at random with "not found";
    subprocesses copy os.environ and so inherit it."""
    name = request.module.__name__.rsplit(".", 1)[-1]
    if not name.startswith(_GUARDED) or any(request.node.get_closest_marker(m) for m in _RIG_MARKERS):
        yield
        return
    import os

    guard = tmp_path_factory.mktemp("no-herdr")
    bin_dir, hits = guard / "bin", guard / "tripwire.txt"
    bin_dir.mkdir()
    wire = bin_dir / "herdr"
    wire.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{hits}"\nexit 97\n')
    wire.chmod(0o755)
    for key in [k for k in os.environ if k.startswith("HERDR_") and k != "HERDR_DISK_GUARD_OVERRIDE"]:  # set for every test above
        monkeypatch.delenv(key)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(guard / "home"))
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(guard / "state"))
    monkeypatch.setenv("HERDR_HANDOFF_STORE", str(guard / "store"))
    yield
    if hits.exists():
        pytest.fail(f"this test ran the real `herdr`: {hits.read_text().strip()!r}", pytrace=False)
