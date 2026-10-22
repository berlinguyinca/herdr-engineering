import pytest

from herdr_engineering import host_config as hc


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
