"""Strict, authority-free assistant package manifest normalization."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_MANIFEST_BYTES = 64 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9._:-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_REGISTRY_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_IMAGE_SEGMENT = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")


class ManifestError(ValueError):
    pass


def agent_package_record(value: Any) -> dict:
    """Validate and return canonical package content plus non-authoritative metadata."""
    manifest = normalize_agent_package(value)
    encoded = _canonical_bytes(manifest)
    return {
        "manifest": manifest,
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "runtime_kind": manifest["runtime"]["kind"],
        "runtime_launch_supported": False,
        "capabilities_are_requests": True,
    }


def normalize_agent_package(value: Any) -> dict:
    """Return a detached, normalized v1 manifest or raise ``ManifestError``."""
    _bounded_json(value)
    root = _object(value, "manifest", {
        "schema_version", "name", "version", "charter", "capabilities", "runtime",
        "sandbox", "model_policy", "references",
    })
    if type(root.get("schema_version")) is not int or root["schema_version"] != 1:
        raise ManifestError("schema_version must be 1")
    name = _pattern(root.get("name"), _NAME, "name")
    version = _pattern(root.get("version"), _VERSION, "version")

    charter = _object(root.get("charter"), "charter", {
        "purpose", "scope", "rules", "delegation_requests",
    })
    normalized_charter = {
        "purpose": _text(charter.get("purpose"), "charter.purpose", 1000),
        "scope": _string_list(charter.get("scope"), "charter.scope", 32, 256,
                              pattern=_IDENTIFIER),
        "rules": _string_list(charter.get("rules"), "charter.rules", 32, 500),
        "delegation_requests": _string_list(
            charter.get("delegation_requests"), "charter.delegation_requests", 32, 128,
            pattern=_IDENTIFIER,
        ),
    }
    capabilities = _string_list(root.get("capabilities"), "capabilities", 32, 128,
                                pattern=_IDENTIFIER)
    runtime = _normalize_runtime(root.get("runtime"))
    sandbox = _normalize_sandbox(root.get("sandbox"))
    model_policy = _normalize_model_policy(root.get("model_policy"))
    references = _normalize_references(root.get("references"))
    return {
        "schema_version": 1,
        "name": name,
        "version": version,
        "charter": normalized_charter,
        "capabilities": capabilities,
        "runtime": runtime,
        "sandbox": sandbox,
        "model_policy": model_policy,
        "references": references,
    }


def _normalize_runtime(value: Any) -> dict:
    runtime = _object(value, "runtime", {"kind", "artifact"})
    kind = runtime.get("kind")
    if kind not in {"pi", "docker", "apptainer"}:
        raise ManifestError("runtime.kind must be pi, docker, or apptainer")
    artifact = runtime.get("artifact")
    if kind == "pi":
        item = _object(artifact, "runtime artifact", {"type", "digest"})
        if item.get("type") != "trusted-pi-runtime" or not _is_digest(item.get("digest")):
            raise ManifestError("pi runtime artifact must be a trusted-pi-runtime SHA-256 digest")
        normalized = {"type": "trusted-pi-runtime", "digest": item["digest"]}
    elif kind == "docker":
        item = _object(artifact, "runtime artifact", {"type", "reference"})
        reference = item.get("reference")
        if item.get("type") != "oci-image" or not _valid_image_reference(reference):
            raise ManifestError("docker runtime artifact must be an OCI reference pinned by SHA-256")
        normalized = {"type": "oci-image", "reference": reference}
    else:
        item = _object(artifact, "runtime artifact", {"type", "reference", "digest"})
        reference = item.get("reference")
        if (item.get("type") != "apptainer-sif"
                or not _valid_image_reference(reference, prefix="oras://")
                or not _is_digest(item.get("digest"))):
            raise ManifestError(
                "apptainer runtime artifact must be a SHA-256 SIF from a digest-pinned ORAS reference")
        normalized = {"type": "apptainer-sif", "reference": reference,
                      "digest": item["digest"]}
    return {"kind": kind, "artifact": normalized}


def _normalize_sandbox(value: Any) -> dict:
    sandbox = _object(value, "sandbox", {
        "profile", "network", "filesystem", "limits",
    })
    if sandbox.get("profile") != "task-restricted-v1":
        raise ManifestError("sandbox.profile must be task-restricted-v1")
    if sandbox.get("network") != "broker-only":
        raise ManifestError("sandbox.network must be broker-only")
    if sandbox.get("filesystem") != "task-only":
        raise ManifestError("sandbox.filesystem must be task-only")
    limits = _object(sandbox.get("limits"), "sandbox.limits", {
        "cpu_quota_percent", "memory_bytes", "process_count", "wall_time_seconds",
    })
    normalized_limits = {
        "cpu_quota_percent": _integer(
            limits.get("cpu_quota_percent"), "sandbox.limits.cpu_quota_percent", 10, 800),
        "memory_bytes": _integer(limits.get("memory_bytes"), "sandbox.limits.memory_bytes",
                                 268_435_456, 17_179_869_184),
        "process_count": _integer(limits.get("process_count"), "sandbox.limits.process_count",
                                  16, 1024),
        "wall_time_seconds": _integer(
            limits.get("wall_time_seconds"), "sandbox.limits.wall_time_seconds", 30, 86_400),
    }
    return {"profile": "task-restricted-v1", "network": "broker-only",
            "filesystem": "task-only", "limits": normalized_limits}


def _normalize_model_policy(value: Any) -> dict:
    policy = _object(value, "model_policy", {"allowed", "default"})
    allowed = _string_list(policy.get("allowed"), "model_policy.allowed", 16, 128,
                           pattern=_MODEL)
    default = _pattern(policy.get("default"), _MODEL, "model_policy.default")
    if default not in allowed:
        raise ManifestError("model_policy.default must be one of model_policy.allowed")
    return {"allowed": allowed, "default": default}


def _normalize_references(value: Any) -> dict:
    refs = _object(value, "references", {"procedures", "knowledge"})
    return {
        "procedures": _reference_list(refs.get("procedures"), "procedures", "procedure:"),
        "knowledge": _reference_list(refs.get("knowledge"), "knowledge", "knowledge:"),
    }


def _reference_list(value: Any, name: str, prefix: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 32:
        raise ManifestError(f"references.{name} must be a list of at most 32 entries")
    out = []
    identities = set()
    for index, raw in enumerate(value):
        item = _object(raw, f"references.{name}[{index}]", {"id", "version", "digest"})
        identifier = item.get("id")
        if (not isinstance(identifier, str) or not identifier.startswith(prefix)
                or not _IDENTIFIER.fullmatch(identifier)):
            raise ManifestError(f"invalid {name} reference id")
        version = _pattern(item.get("version"), _VERSION, f"references.{name} version")
        if not _is_digest(item.get("digest")):
            raise ManifestError(f"invalid {name} reference digest")
        identity = (identifier, version)
        if identity in identities:
            raise ManifestError(f"references.{name} entries must be unique")
        identities.add(identity)
        out.append({"id": identifier, "version": version, "digest": item["digest"]})
    return out


def _bounded_json(value: Any) -> None:
    _check_json_structure(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError):
        raise ManifestError("manifest must contain finite JSON values") from None
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest exceeds 64 KiB")


def _check_json_structure(value: Any) -> None:
    nodes = 0

    def walk(item: Any, depth: int, ancestors: frozenset[int]) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 4096:
            raise ManifestError("manifest contains too many nested values")
        if depth > 16:
            raise ManifestError("manifest nesting exceeds 16 levels")
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ManifestError("manifest object keys must be strings")
            marker = id(item)
            if marker in ancestors:
                raise ManifestError("manifest must contain finite JSON values")
            nested = ancestors | {marker}
            for child in item.values():
                walk(child, depth + 1, nested)
        elif isinstance(item, list):
            marker = id(item)
            if marker in ancestors:
                raise ManifestError("manifest must contain finite JSON values")
            nested = ancestors | {marker}
            for child in item:
                walk(child, depth + 1, nested)

    walk(value, 0, frozenset())


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode()


def _object(value: Any, name: str, fields: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be an object")
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise ManifestError(f"unknown field in {name}")
    if missing:
        raise ManifestError(f"missing field in {name}: {missing[0]}")
    return value


def _text(value: Any, name: str, limit: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > limit
            or value != value.strip() or not value.isprintable()):
        raise ManifestError(f"{name} must be 1-{limit} printable characters without outer whitespace")
    return value


def _pattern(value: Any, pattern: re.Pattern, name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ManifestError(f"invalid {name}")
    return value


def _string_list(value: Any, name: str, count: int, length: int,
                 *, pattern: re.Pattern | None = None) -> list[str]:
    if not isinstance(value, list) or len(value) > count:
        raise ManifestError(f"{name} must be a list of at most {count} entries")
    out = []
    for item in value:
        out.append(_pattern(item, pattern, name) if pattern else _text(item, name, length))
    if len(set(out)) != len(out):
        raise ManifestError(f"{name} entries must be unique")
    return out


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ManifestError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST.fullmatch(value))


def _valid_image_reference(value: Any, *, prefix: str = "") -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or not value.startswith(prefix):
        return False
    raw = value[len(prefix):]
    if raw.count("@") != 1 or any(char in raw for char in "?#\\"):
        return False
    location, digest = raw.rsplit("@", 1)
    if not _is_digest(digest):
        return False
    parts = location.split("/")
    if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
        return False
    registry = parts[0]
    if ":" in registry:
        if registry.count(":") != 1:
            return False
        registry, port_text = registry.rsplit(":", 1)
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65_535:
            return False
    labels = registry.split(".")
    if not labels or any(not _REGISTRY_LABEL.fullmatch(label) for label in labels):
        return False
    return all(bool(_IMAGE_SEGMENT.fullmatch(segment)) for segment in parts[1:])
