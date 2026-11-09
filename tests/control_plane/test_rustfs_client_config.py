"""The real boto3 client the RustFS wrapper builds: path-style addressing and no default CRC headers.

RustFS (like MinIO) is addressed by ``http://rustfs:9000/<bucket>/<key>`` (the host ``rustfs`` has no wildcard
DNS for ``<bucket>.rustfs``), and boto3 >= 1.36 adds CRC checksum headers by default that non-AWS stores may
reject. No network is touched: building a client and reading its config only.
"""
import botocore

from herdr_engineering.control_plane.rustfs import RustFSClient, build_s3_config


def _client():
    return RustFSClient(endpoint="http://rustfs:9000", bucket="b", access_key="k", secret_key="s")._s3


def test_client_uses_path_style_addressing():
    assert _client().meta.config.s3["addressing_style"] == "path"


def test_client_keeps_region_and_endpoint():
    c = _client()
    assert c.meta.region_name == "us-east-1" and c.meta.endpoint_url == "http://rustfs:9000"


def test_checksums_only_when_required_where_botocore_supports_it():
    cfg = _client().meta.config
    if hasattr(botocore.config.Config(), "request_checksum_calculation"):
        assert cfg.request_checksum_calculation == "when_required"
        assert cfg.response_checksum_validation == "when_required"


def test_build_s3_config_degrades_for_old_botocore(monkeypatch):
    import herdr_engineering.control_plane.rustfs as mod
    real = mod.Config
    seen = []

    def old_config(**kw):
        seen.append(set(kw))
        if "request_checksum_calculation" in kw:
            raise TypeError("Got unexpected keyword argument 'request_checksum_calculation'")
        return real(**kw)
    monkeypatch.setattr(mod, "Config", old_config)
    cfg = build_s3_config()
    assert cfg.s3["addressing_style"] == "path"
    assert len(seen) == 2
