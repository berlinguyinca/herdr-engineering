import pytest

from herdr_engineering.exposure_cloudflare import CloudflareTunnelProvider


def test_cloudflare_provider_is_disabled_by_default():
    calls = []
    provider = CloudflareTunnelProvider(runner=lambda *args: calls.append(args))
    assert provider.capabilities()["enabled"] is False
    with pytest.raises(RuntimeError, match="disabled"):
        provider.expose({"local_target": "http://127.0.0.1:8085"})
    assert calls == []


def test_enabled_cloudflare_provider_uses_loopback_target_only():
    calls = []
    provider = CloudflareTunnelProvider(enabled=True,
                                       runner=lambda *args: calls.append(args) or "ok")
    result = provider.expose({"local_target": "http://127.0.0.1:8085"})
    assert result["status"] == "observed"
    assert calls == [("tunnel", "--url", "http://127.0.0.1:8085")]
    with pytest.raises(ValueError, match="loopback"):
        provider.expose({"local_target": "http://10.0.0.2:8085"})

