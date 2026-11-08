"""The host's config file parser and the CLI wiring (no mocks: real files, a real loopback HTTP listener)."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from herdr_artifact_host import cli, host_config

SECRET = "s" * 40
CLAIM = "c" * 40


def good() -> dict:
    return {
        "version": 1,
        "public_host": "artifacts.example.test",
        "public_url": "https://artifacts.example.test",
        "mcp_public_hosts": ["mcp.example.test"],
        "session_seconds": 14400,
        "cognito": {
            "pool_id": "us-west-2_POOL",
            "client_ids": ["client-a"],
            "web_client_id": "client-a",
            "domain": "auth.example.test",
            "group": "team",
        },
        "session_secret": SECRET,
        "claim_key": CLAIM,
        "internal_key_digest": hashlib.sha256(b"k" * 40).hexdigest(),
        "health_port": 8092,
        "database": {"user": "artifact_host", "password": "p" * 20},
    }


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    return path


def test_load_maps_the_file_to_a_config(tmp_path: Path) -> None:
    cfg = host_config.load(write(tmp_path, good()))
    assert cfg.public_host == "artifacts.example.test"
    assert cfg.mcp_public_hosts == ("mcp.example.test",)
    assert cfg.group == "team"


@pytest.mark.parametrize(
    ("mutate", "key"),
    [
        (lambda d: d.update(session_secret="short"), "session_secret"),
        (lambda d: d.update(claim_key=SECRET), "claim_key"),
        (lambda d: d.update(bogus=1), "bogus"),
        (lambda d: d.update(version=2), "version"),
        (lambda d: d.update(public_host="Bad_Host"), "public_host"),
    ],
)
def test_parse_refuses_bad_files_naming_keys_never_values(mutate, key: str) -> None:
    data = copy.deepcopy(good())
    mutate(data)
    with pytest.raises(ValueError) as err:
        host_config.parse(data)
    assert key in str(err.value)
    for secret in (SECRET, CLAIM, "p" * 20):
        assert secret not in str(err.value)


def test_load_reports_unreadable_and_non_json_files_without_content(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        host_config.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{" + SECRET)
    with pytest.raises(ValueError, match="not valid JSON") as err:
        host_config.load(bad)
    assert SECRET not in str(err.value)


def test_public_config_carries_health_hosts_and_cognito(tmp_path: Path) -> None:
    cfg = host_config.load(write(tmp_path, good()))
    pc = cli.public_config(cfg)
    assert pc.mcp_public_hosts == ("mcp.example.test",)
    assert pc.public_host == "artifacts.example.test"
    assert pc.web_client_id == "client-a" and pc.member_group == "team"


class _Health(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _serve(status: int):
    handler = type("H", (_Health,), {"status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_healthcheck_public_probes_the_health_port(tmp_path: Path) -> None:
    server = _serve(200)
    try:
        data = good()
        data["health_port"] = server.server_address[1]
        path = write(tmp_path, data)
        assert cli.main(["healthcheck", "public", "--config", str(path)]) == 0
        server.shutdown()
    finally:
        server.server_close()
    assert cli.main(["healthcheck", "public", "--config", str(path)]) == 1


def test_healthcheck_fails_on_a_non_200(tmp_path: Path) -> None:
    server = _serve(503)
    try:
        data = good()
        data["health_port"] = server.server_address[1]
        assert cli.main(["healthcheck", "public", "--config", str(write(tmp_path, data))]) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_internal_probe_targets_the_internal_port(tmp_path: Path) -> None:
    cfg = host_config.load(write(tmp_path, good()))
    assert cli.probe_url("internal", cfg).endswith(f":{host_config.INTERNAL_PORT}/internal/healthz")


def test_every_role_subcommand_is_registered_and_a_bad_config_exits_2(
    tmp_path: Path, capsys
) -> None:
    missing = str(tmp_path / "nope.json")
    for cmd in ("public", "internal", "migrate"):
        assert cli.main([cmd, "--config", missing]) == 2
    assert cli.main(["healthcheck", "internal", "--config", missing]) == 2
    assert "cannot read" in capsys.readouterr().err
