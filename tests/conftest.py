import copy
import gc
import os
from pathlib import Path

import pytest

from herdr_engineering import host_config as hc
from herdr_engineering import scratch_safety


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


_ACCOUNT_HOME = scratch_safety.user_home


@pytest.fixture(autouse=True)
def _throwaway_home_follows_home_env(monkeypatch):
    """Production resolves the home from the password database and never from ``$HOME``. Most tests
    want a throwaway home, so here (and only here) ``user_home`` follows the test's ``HOME``. A test
    that needs the real behaviour asks for ``account_home``."""
    monkeypatch.setattr(scratch_safety, "user_home",
                        lambda: Path(os.environ["HOME"]) if os.environ.get("HOME") else _ACCOUNT_HOME())


@pytest.fixture
def account_home(monkeypatch):
    """The password-database home, with the production ``user_home`` back in place."""
    monkeypatch.setattr(scratch_safety, "user_home", _ACCOUNT_HOME)
    return _ACCOUNT_HOME()
