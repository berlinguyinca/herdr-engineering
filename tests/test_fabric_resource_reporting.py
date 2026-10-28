from types import SimpleNamespace

import pytest

from herdr_engineering.fabric_agent import AgentConfig, FabricAgent


@pytest.mark.parametrize("enabled,failed", [(True, False), (False, False), (True, True)])
def test_host_tick_reports_token_bound_scheduler_resources_without_breaking_legacy_reporting(
        tmp_path, monkeypatch, fake_host_config, enabled, failed):
    fake_host_config({"scheduler": {"telemetry": enabled}})
    monkeypatch.setattr("herdr_engineering.host_config._read_host_json", lambda _: {
        "control_plane_url": "http://cp.invalid", "token": "test-host-token"})
    sample = {"host_uid": "host-" + "a" * 32, "sequence": 1}
    monkeypatch.setattr("herdr_engineering.resource_probe.ResourceProbe.sample", lambda _: sample)
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if failed:
            raise OSError("unavailable")
        return {}

    monkeypatch.setattr("herdr_engineering.host_config._default_fetch", fetch)
    agent = FabricAgent(SimpleNamespace(), AgentConfig(
        tailnet_ip="192.0.2.1", store_path=tmp_path / "agent.json",
        control_plane_url="http://cp.invalid"))
    monkeypatch.setattr(agent._sampler, "sample", lambda: {"cpu": 10})
    monkeypatch.setattr(agent, "_report_host", lambda *args: 200)
    assert agent._host_tick() is True
    if enabled:
        assert len(calls) == 1
        url, args = calls[0]
        assert url == "http://cp.invalid/api/v1/fleet/config/telemetry"
        assert args["token"] == "test-host-token" and args["body"] == sample
        assert args["method"] == "POST" and args["timeout"] == 2
    else:
        assert calls == []
