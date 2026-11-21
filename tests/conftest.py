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
        raise hc.HostConfigUnavailable("no host configuration in tests")

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
