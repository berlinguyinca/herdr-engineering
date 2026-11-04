"""Actual HTTP → PG journal → native Hindsight integration, with synthetic data."""

import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant_memory_runtime import build_memory_runtime
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.e2e.pg import ThrowawayPostgres, find_bins


def test_unconfigured_memory_does_not_start_another_service():
    assert build_memory_runtime(None, {}) is None
    assert build_memory_runtime(None, {"enabled": False}) is None


@pytest.mark.skipif(
    not os.environ.get("HERDR_MEMORY_DEPLOYMENT_TEST_CONFIG"),
    reason="Requires explicitly selected native Hindsight deployment",
)
@pytest.mark.parametrize(
    "registry_source,credential_source", [("file", "file"), ("inline", "inline")]
)
def test_two_harnesses_share_project_memory_and_cannot_resurrect_a_forgotten_note(
    tmp_path, registry_source, credential_source
):
    from herdr_memory.transport import MemoryError, Transport, private_file

    native = json.loads(Path(os.environ["HERDR_MEMORY_DEPLOYMENT_TEST_CONFIG"]).read_text())
    bins = find_bins({})
    assert bins is not None
    pg = ThrowawayPostgres(bins, tmp_path / "pg")
    shutil.rmtree(pg.sock)
    pg.sock = Path(tempfile.mkdtemp(prefix="memrt-", dir="/tmp"))
    project = "runtime-" + uuid.uuid4().hex
    banks = {"fact": project + "-facts", "summary": project + "-summaries"}
    registry = tmp_path / "registry.json"
    registry_data = {
        "backend_url": native["api_url"],
        "domains": {
            project: {
                "domain_type": "project",
                "domain_id": "example/" + project,
                "tenant_id": "example",
                "bank_ids": banks,
                "backend_token_file": native["backend_key_files"]["validation"],
            }
        },
        "credentials": {
            "codex-project-token": {"harness": "codex", "domains": [project], "read_only": False},
            "pi-project-token": {"harness": "pi", "domains": [project], "read_only": False},
            "outsider-token": {"harness": "claude", "domains": [], "read_only": True},
        },
        "operator_domains": [project],
    }
    if credential_source == "inline":
        domain = registry_data["domains"][project]
        domain["backend_token"] = private_file(domain.pop("backend_token_file"))
    if registry_source == "file":
        registry.write_text(json.dumps(registry_data))
        registry.chmod(0o600)
        memory_config = {"enabled": True, "registry_file": str(registry)}
    else:
        memory_config = {"enabled": True, **registry_data}
    runtime = server = None
    pg.start()
    try:
        runtime = build_memory_runtime(pg.dsn, memory_config)
        server = ControlPlaneServer(ControlPlaneRepo(), memory_gateway=runtime)
        threading.Thread(target=server.serve, daemon=True).start()
        server.wait_until_ready()
        base = f"http://127.0.0.1:{server.port}/api/v1/assistant-memory/hindsight"

        def call(token, method, path, body=None):
            request = urllib.request.Request(
                base + path,
                method=method,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                data=json.dumps(body).encode() if body is not None else None,
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        bank = "/v1/default/banks/" + banks["fact"]
        note = {
            "async": False,
            "items": [
                {
                    "document_id": "cross-harness-note",
                    "content": "The synthetic Atlas release must use the blue slot before switching production users.",
                    "metadata": {"explicit": "true", "harness": "forged"},
                }
            ],
        }
        assert call("codex-project-token", "POST", bank + "/memories", note)[0] in (200, 202)
        saved = pg.query(
            "SELECT provenance,approval_metadata FROM pa_memory WHERE domain_id=$1",
            "example/" + project,
        )[0]
        provenance = json.loads(saved["provenance"])
        assert provenance["harness"] == "codex"
        assert json.loads(saved["approval_metadata"])["human_verified"] is False
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/api/v1/assistant-memory/domains"
        ) as response:
            domains = json.load(response)["domains"]
        assert domains[0]["alias"] == project and domains[0]["counts"]["active"] == 1
        assert "backend_token_file" not in json.dumps(domains)
        assert "backend_token" not in json.dumps(domains)
        assert runtime.health()["enabled"] is True
        if credential_source == "inline":
            assert domain["backend_token"] not in json.dumps(runtime.health())
        query = {
            "query": "Which release environment is needed before live traffic?",
            "max_tokens": 1000,
        }
        status, result = call("pi-project-token", "POST", bank + "/memories/recall", query)
        assert status == 200 and "blue" in json.dumps(result["results"]).lower()
        assert call("outsider-token", "POST", bank + "/memories/recall", query)[0] == 404
        assert call("pi-project-token", "DELETE", bank + "/documents/cross-harness-note")[0] in (
            200,
            202,
        )
        status, result = call("codex-project-token", "POST", bank + "/memories/recall", query)
        assert status == 200 and not result["results"]
        assert call("codex-project-token", "POST", bank + "/memories", note)[0] == 409
    finally:
        if server:
            server.shutdown()
        elif runtime:
            runtime.close()
        pg.close()
        # Remove only this test's unique synthetic banks from the validation tenant.
        transport = Transport(
            url=native["api_url"], token_file=native["backend_key_files"]["validation"], timeout=10
        )
        for bank_id in banks.values():
            try:
                transport.request("DELETE", "/v1/default/banks/" + bank_id)
            except MemoryError as error:
                if (
                    not isinstance(error.__cause__, urllib.error.HTTPError)
                    or error.__cause__.code != 404
                ):
                    raise


def _inline_registry():
    return {
        "backend_url": "http://127.0.0.1:8765",
        "domains": {
            "example": {
                "domain_type": "project",
                "domain_id": "project/example",
                "tenant_id": "tenant",
                "bank_ids": {"fact": "example-facts", "summary": "example-summaries"},
                "backend_token": "synthetic-private-token",
            }
        },
        "credentials": {"synthetic-caller-token": {"harness": "pi", "domains": ["example"]}},
        "operator_domains": ["example"],
        "allow_private_http": True,
    }


def test_enabled_inline_registry_reaches_runtime_without_external_file(monkeypatch):
    from herdr_engineering.control_plane import assistant_memory_runtime as module

    seen = []

    class Boundary:
        async def health(self):
            return {"enabled": True, "domain_count": 1}

        async def close(self):
            pass

    async def create(dsn, registry):
        assert dsn == "test-dsn"
        seen.append(registry)
        return Boundary()

    monkeypatch.setattr(module._Boundary, "create", create)
    original = _inline_registry()
    config = {"enabled": True, **original, "unrelated_provider_secret": "must-not-be-copied"}
    runtime = module.build_memory_runtime("test-dsn", config)
    try:
        assert runtime.health() == {"enabled": True, "domain_count": 1}
        assert seen == [original]
        config["domains"]["example"]["backend_token"] = "changed"
        assert seen[0]["domains"]["example"]["backend_token"] == "synthetic-private-token"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "inline_field",
    ["backend_url", "domains", "credentials", "operator_domains", "allow_private_http"],
)
def test_registry_file_and_any_inline_registry_field_are_mutually_exclusive(tmp_path, inline_field):
    from herdr_engineering.control_plane.assistant_memory_runtime import _load_registry

    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_inline_registry()))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="inline.*registry_file|registry_file.*inline"):
        _load_registry(
            {"registry_file": str(path), inline_field: _inline_registry()[inline_field]},
            os.getuid(),
        )


def test_file_registry_retains_private_file_checks(tmp_path):
    from herdr_engineering.control_plane.assistant_memory_runtime import _load_registry

    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_inline_registry()))
    path.chmod(0o600)
    assert _load_registry({"registry_file": str(path)}, os.getuid()) == _inline_registry()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private file"):
        _load_registry({"registry_file": str(path)}, os.getuid())


@pytest.mark.parametrize(
    "credential_fields",
    [
        {},
        {"backend_token": "secret", "backend_token_file": "/private/key"},
        {"backend_token": None, "backend_token_file": "/private/key"},
        {"backend_token": ""},
        {"backend_token_file": ""},
    ],
)
def test_invalid_domain_credential_sources_reject_before_database_start(
    monkeypatch, credential_fields
):
    from herdr_engineering.control_plane import assistant_memory_runtime as module

    config = _inline_registry()
    config["domains"]["example"].pop("backend_token")
    config["domains"]["example"].update(credential_fields)

    def unexpected_adapter(*args):
        pytest.fail("invalid credential configuration must fail before database startup")

    monkeypatch.setattr(module, "SyncArtifactAdapter", unexpected_adapter)
    with pytest.raises(ValueError, match="credential"):
        module.build_memory_runtime("test-dsn", {"enabled": True, **config})
