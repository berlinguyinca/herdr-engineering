import pytest

from herdr_engineering.exposure_tailscale import TailscaleFunnelProvider


def test_private_only_provider_reports_disabled_without_running_tailscale():
    calls = []
    provider = TailscaleFunnelProvider(enabled=False, runner=lambda *args: calls.append(args))
    assert provider.capabilities()["enabled"] is False
    with pytest.raises(RuntimeError, match="disabled"):
        provider.expose({"publication_id": "pub_1"})
    assert calls == []


def test_enabled_provider_uses_exact_scoped_route_command():
    calls = []
    provider = TailscaleFunnelProvider(enabled=True, runner=lambda *args: calls.append(args) or "ok")
    result = provider.expose({"publication_id": "pub_1", "local_target": "http://127.0.0.1:8088"})
    assert result["status"] == "observed"
    assert calls == [("funnel", "--bg", "http://127.0.0.1:8088")]
