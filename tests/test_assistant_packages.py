from __future__ import annotations

import copy
import json

import pytest

from herdr_engineering import assistant_cli, cli
from herdr_engineering.assistant_cli import AssistantClient, ConfigError
from herdr_engineering.assistant_packages import ManifestError, agent_package_record
from tests.test_assistant_cli import OTHER_TOKEN, TOKEN, config, seed_world

pytest_plugins = ("tests.test_assistant_cli",)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def manifest(kind="pi"):
    artifacts = {
        "pi": {"type": "trusted-pi-runtime", "digest": DIGEST_A},
        "docker": {"type": "oci-image",
                   "reference": "registry.example/herdr/venue-agent@" + DIGEST_A},
        "apptainer": {"type": "apptainer-sif",
                      "reference": "oras://registry.example/herdr/venue-agent@" + DIGEST_A,
                      "digest": DIGEST_B},
    }
    return {
        "schema_version": 1,
        "name": "venue-coordinator",
        "version": "1.2.0",
        "charter": {
            "purpose": "Research and compare event venues.",
            "scope": ["event.venue.research", "event.venue.correspondence.prepare"],
            "rules": ["Never book or contact a venue without an active task grant."],
            "delegation_requests": ["web.research", "email.draft"],
        },
        "capabilities": ["event.coordinate", "email.draft"],
        "runtime": {"kind": kind, "artifact": artifacts[kind]},
        "sandbox": {
            "profile": "task-restricted-v1",
            "network": "broker-only",
            "filesystem": "task-only",
            "limits": {"cpu_quota_percent": 100, "memory_bytes": 1_073_741_824,
                       "process_count": 128,
                       "wall_time_seconds": 3600},
        },
        "model_policy": {"allowed": ["qwen3-32b", "llama-4-scout"],
                         "default": "qwen3-32b"},
        "references": {
            "procedures": [{"id": "procedure:venue-research", "version": "2.1.0",
                            "digest": DIGEST_A}],
            "knowledge": [{"id": "knowledge:event-venue-baseline", "version": "1.0.0",
                           "digest": DIGEST_B}],
        },
    }


@pytest.mark.parametrize("kind", ["pi", "docker", "apptainer"])
def test_valid_manifest_is_canonical_content_addressed_and_not_runnable(kind):
    source = manifest(kind)
    validated = agent_package_record(source)
    reversed_source = {key: source[key] for key in reversed(source)}
    assert agent_package_record(reversed_source)["digest"] == validated["digest"]
    assert validated["manifest"] == source
    assert validated["runtime_kind"] == kind
    assert validated["digest"].startswith("sha256:")
    assert validated["runtime_launch_supported"] is False
    assert validated["capabilities_are_requests"] is True


@pytest.mark.parametrize(("path", "value"), [
    (("owner_user_id",), "alice"),
    (("credential",), "secret"),
    (("grant",), {"email.send": True}),
    (("sandbox", "mounts"), ["/Users/alice"]),
    (("sandbox", "env"), {"TOKEN": "secret"}),
    (("sandbox", "privileged"), True),
    (("runtime", "command"), ["/bin/sh", "-c", "anything"]),
])
def test_unknown_authority_host_and_execution_fields_are_rejected(path, value):
    source = manifest()
    target = source
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ManifestError, match="unknown field"):
        agent_package_record(source)


@pytest.mark.parametrize("change", [
    lambda m: m["sandbox"].update(profile="custom"),
    lambda m: m["sandbox"].update(network="host"),
    lambda m: m["sandbox"].update(filesystem="home"),
    lambda m: m["sandbox"]["limits"].update(cpu_quota_percent=0),
    lambda m: m["sandbox"]["limits"].update(memory_bytes=1),
    lambda m: m["sandbox"]["limits"].update(process_count=100_000),
    lambda m: m["sandbox"]["limits"].update(wall_time_seconds=0),
])
def test_sandbox_profile_and_limits_are_strict(change):
    source = manifest()
    change(source)
    with pytest.raises(ManifestError):
        agent_package_record(source)


@pytest.mark.parametrize("kind,artifact", [
    ("pi", {"type": "trusted-pi-runtime", "digest": "latest"}),
    ("docker", {"type": "oci-image", "reference": "registry.example/image:latest"}),
    ("docker", {"type": "oci-image", "reference": "registry.example/image@sha256:abc"}),
    ("apptainer", {"type": "apptainer-sif", "reference": "/host/image.sif",
                   "digest": DIGEST_A}),
])
def test_runtime_artifacts_must_be_immutable_and_kind_specific(kind, artifact):
    source = manifest(kind)
    source["runtime"]["artifact"] = artifact
    with pytest.raises(ManifestError, match="artifact"):
        agent_package_record(source)


def test_model_default_must_be_allowed_and_lists_are_unique():
    source = manifest()
    source["model_policy"]["default"] = "unapproved-model"
    with pytest.raises(ManifestError, match="default"):
        agent_package_record(source)
    source = manifest()
    source["capabilities"].append(source["capabilities"][0])
    with pytest.raises(ManifestError, match="unique"):
        agent_package_record(source)


def test_references_are_versioned_content_addresses_not_paths_or_urls():
    source = manifest()
    source["references"]["knowledge"][0]["id"] = "/home/alice/knowledge.json"
    with pytest.raises(ManifestError, match="reference"):
        agent_package_record(source)


def test_nested_text_lists_and_total_manifest_size_are_bounded():
    source = manifest()
    source["charter"]["rules"] = ["x"] * 33
    with pytest.raises(ManifestError, match="rules"):
        agent_package_record(source)
    source = manifest()
    source["charter"]["purpose"] = "x" * 1001
    with pytest.raises(ManifestError, match="purpose"):
        agent_package_record(source)
    source = manifest()
    source["padding"] = "x" * 70_000
    with pytest.raises(ManifestError, match="64 KiB"):
        agent_package_record(source)


def test_validation_does_not_mutate_input():
    source = manifest()
    before = copy.deepcopy(source)
    agent_package_record(source)
    assert source == before


def test_manifest_nan_is_rejected():
    source = manifest()
    source["sandbox"]["limits"]["memory_bytes"] = float("nan")
    with pytest.raises(ManifestError):
        agent_package_record(source)


def test_schema_version_boolean_and_deep_json_are_structured_errors():
    source = manifest()
    source["schema_version"] = True
    with pytest.raises(ManifestError, match="schema_version"):
        agent_package_record(source)
    source = manifest()
    deep = []
    cursor = deep
    for _ in range(2000):
        child = []
        cursor.append(child)
        cursor = child
    source["charter"]["purpose"] = deep
    with pytest.raises(ManifestError, match="nesting"):
        agent_package_record(source)


@pytest.mark.parametrize("reference", [
    "user:pass/agent@" + DIGEST_A,
    "registry.example/../agent@" + DIGEST_A,
    "registry.example/team/agent?tag=x@" + DIGEST_A,
    "registry.example/team/agent#fragment@" + DIGEST_A,
    "registry.example:99999/team/agent@" + DIGEST_A,
    "registry.example/Team/agent@" + DIGEST_A,
    "registry.example/" + "a" * 500 + "@" + DIGEST_A,
])
def test_container_references_reject_credentials_traversal_delimiters_and_bad_registry(reference):
    source = manifest("docker")
    source["runtime"]["artifact"]["reference"] = reference
    with pytest.raises(ManifestError, match="artifact"):
        agent_package_record(source)


def test_cli_package_validate_is_local_and_needs_no_credential(tmp_path, monkeypatch, capsys):
    path = tmp_path / "package.json"
    path.write_text(json.dumps(manifest()))
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN", raising=False)
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN_FILE", raising=False)
    assert cli.main(["assistant", "packages", "validate", "--file", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["runtime_launch_supported"] is False
    assert result["manifest"]["name"] == "venue-coordinator"


def test_cli_package_validate_returns_versioned_error_without_credentials(tmp_path, monkeypatch,
                                                                          capsys):
    source = manifest()
    source["credential"] = "must-not-be-accepted"
    path = tmp_path / "bad-package.json"
    path.write_text(json.dumps(source))
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN", raising=False)
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN_FILE", raising=False)
    assert cli.main(["assistant", "packages", "validate", "--file", str(path), "--json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1 and output["ok"] is False
    assert output["error"]["code"] == "invalid_request"
    assert "must-not-be-accepted" not in json.dumps(output)


def test_cli_deep_json_file_returns_versioned_invalid_request(tmp_path, monkeypatch, capsys):
    source = manifest()
    source["charter"]["purpose"] = "DEEP_VALUE"
    encoded = json.dumps(source).replace('"DEEP_VALUE"', "[" * 2000 + "0" + "]" * 2000)
    assert len(encoded.encode()) < 64 * 1024
    path = tmp_path / "deep-package.json"
    path.write_text(encoded)
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN", raising=False)
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN_FILE", raising=False)
    assert cli.main(["assistant", "packages", "validate", "--file", str(path), "--json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1 and output["ok"] is False
    assert output["error"]["code"] == "invalid_request"


def test_input_reader_converts_decoder_recursion_to_config_error(tmp_path, monkeypatch):
    path = tmp_path / "package.json"
    path.write_text("{}")

    def recurse(_text):
        raise RecursionError("raw decoder detail")

    monkeypatch.setattr(assistant_cli.json, "loads", recurse)
    with pytest.raises(ConfigError, match="nested"):
        assistant_cli._read_object(str(path))


def test_package_register_replays_and_changed_same_version_conflicts(collab_server):
    server, _ = collab_server
    room, _ = seed_world(server)
    client = AssistantClient(config(server.port))
    first = client.package_register(room, manifest())
    replay = client.package_register(room, manifest())
    assert replay["package"]["package_id"] == first["package"]["package_id"]
    assert replay["replayed"] is True
    changed = manifest()
    changed["charter"]["purpose"] = "A changed definition under the same version."
    with pytest.raises(Exception) as conflict:
        client.package_register(room, changed)
    assert getattr(conflict.value, "status", None) == 409


def test_package_list_and_inspect_are_owner_only(collab_server):
    server, _ = collab_server
    room, agent = seed_world(server)
    owner = AssistantClient(config(server.port))
    registered = owner.package_register(room, manifest())["package"]
    assert owner.packages_list(room)["packages"][0]["digest"] == registered["digest"]
    assert owner.package_get(room, registered["package_id"])["manifest"]["version"] == "1.2.0"

    unrelated = AssistantClient(config(server.port, token=OTHER_TOKEN))
    with pytest.raises(Exception) as hidden:
        unrelated.packages_list(room)
    assert getattr(hidden.value, "status", None) == 404

    agent_client = AssistantClient(config(
        server.port, token=agent["credential"], auth_kind="agent", device=agent["device_id"]))
    with pytest.raises(Exception) as forbidden:
        agent_client.package_register(room, manifest())
    assert getattr(forbidden.value, "status", None) in {403, 404}


def test_cli_package_registration_uses_versioned_json(collab_server, tmp_path, monkeypatch, capsys):
    server, _ = collab_server
    room, _ = seed_world(server)
    path = tmp_path / "package.json"
    path.write_text(json.dumps(manifest("docker")))
    monkeypatch.setenv("HERDR_ASSISTANT_URL", f"http://127.0.0.1:{server.port}")
    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN", TOKEN)
    monkeypatch.setenv("HERDR_ASSISTANT_AUTH_KIND", "user")
    command = ["assistant", "packages", "register", "--room", room,
               "--file", str(path), "--json"]
    assert cli.main(command) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1 and output["ok"] is True
    assert output["result"]["package"]["runtime_launch_supported"] is False
