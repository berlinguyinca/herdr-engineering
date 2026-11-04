"""``/api/v1/skills`` routes and construction of the (flag-gated) skill registry service.

Config (YAML only, never environment variables)::

    control_plane:
      skill_registry:
        enabled: false            # default; when off the routes answer 404 and nothing is constructed
        signing_key_file: /path   # 64 hex chars (Ed25519 seed), file mode 0600
        key_id: skills-1
        trusted_keys: {skills-1: <64 hex public key>}   # optional; defaults to the signing key's public half
"""
from __future__ import annotations

import base64
import binascii
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .skill_registry import MAX_PACKAGE_BYTES, SkillError, SkillRegistry
from .skill_transfer import SkillTransfer

CAPABILITY = "skill_registry_v1"
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_BODY_LIMIT = MAX_PACKAGE_BYTES * 4 // 3 + 256 * 1024


def skill_registry_config(cfg) -> dict:
    cfg = {} if cfg is None else cfg
    if not isinstance(cfg, dict):
        raise ValueError("control_plane.skill_registry must be a mapping")
    unknown = set(cfg) - {"enabled", "signing_key_file", "key_id", "trusted_keys"}
    if unknown:
        raise ValueError(f"control_plane.skill_registry: unknown keys {sorted(unknown)}")
    if cfg.get("enabled", False) not in (True, False):
        raise ValueError("control_plane.skill_registry.enabled must be true or false")
    out = {"enabled": cfg.get("enabled", False) is True, "key_id": cfg.get("key_id") or "skills-1",
           "signing_key_file": cfg.get("signing_key_file"), "trusted_keys": cfg.get("trusted_keys") or {}}
    if out["enabled"] and not out["signing_key_file"]:
        raise ValueError("control_plane.skill_registry.signing_key_file is required when enabled")
    if not isinstance(out["trusted_keys"], dict):
        raise ValueError("control_plane.skill_registry.trusted_keys must be a mapping")
    return out


def build_skill_registry(cfg, *, artifact_store, rustfs, acl=None, record_event=None):
    """Return ``(registry, transfer)`` or ``(None, None)`` when the flag is off."""
    config = skill_registry_config(cfg)
    if not config["enabled"]:
        return None, None
    if artifact_store is None or rustfs is None:
        raise ValueError("control_plane.skill_registry requires the versioned artifact registry and object store")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    path = Path(config["signing_key_file"])
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("skill signing key file must not be accessible to group or others")
    try:
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(path.read_text().strip()))
    except (ValueError, binascii.Error):
        raise ValueError("skill signing key file must hold 64 hex characters") from None
    trusted = {config["key_id"]: key.public_key()}
    for key_id, hex_key in config["trusted_keys"].items():
        trusted[str(key_id)] = Ed25519PublicKey.from_public_bytes(bytes.fromhex(str(hex_key)))

    def on_grant(artifact_id, principal_id, permissions, granted_by):
        grant = {"artifact_id": artifact_id, "principal_id": principal_id,
                 "permissions": sorted(permissions), "expires_at": None, "granted_by": granted_by}
        artifact_store("record_acl_event", "grant", grant)       # durable source of truth
        if acl is not None:
            acl.grant(artifact_id, principal_id, set(permissions))  # in-memory mirror (no on_change loop)

    registry = SkillRegistry(artifact_store, rustfs, signing_key=key, key_id=config["key_id"],
                             trusted_keys=trusted, on_grant=on_grant)
    return registry, SkillTransfer(registry, record_event=record_event)


def _fail(handler, exc):
    handler._json({"error": str(exc) if exc.status != 404 else "not found"}, exc.status)
    return True


def route_skills(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/skills*``. Returns True when the request was answered."""
    if resource != "skills":
        return False
    transfer = getattr(server, "skill_transfer", None)
    if transfer is None:                       # flag off: indistinguishable from an unknown route
        handler._json({"error": "not found"}, 404)
        return True
    registry = transfer.registry
    principal = handler._resource_principal()
    method = handler.command
    tail = parts[4:]
    try:
        if method == "GET":
            if not tail:
                query = (parse_qs(urlsplit(handler.path).query).get("q") or [""])[0]
                handler._json({"skills": registry.search(principal, query)})
                return True
            if len(tail) == 2 and tail[0] == "download":
                handler._bytes(transfer.redeem(principal, tail[1]), "application/json")
                return True
            if len(tail) == 3 and tail[1] == "versions":
                handler._json(transfer.get_skill(principal, tail[0], tail[2]))
                return True
        elif method == "POST":
            origin = handler.headers.get("Origin")
            if origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                           or urlsplit(origin).scheme not in {"http", "https"}):
                handler._deny(403, "cross-origin skill mutation refused")
                return True
            if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                handler._json({"error": "skill mutations require application/json"}, 415)
                return True
            body = handler._read_json(_BODY_LIMIT)
            if not isinstance(body, dict):
                handler._json({"error": "JSON object body required"}, 400)
                return True
            if not tail:
                handler._json(dict(_publish(registry, principal, body)), 201)
                return True
            if len(tail) == 4 and tail[1] == "versions" and tail[3] == "transfer":
                ref = transfer.grant(principal, tail[0], tail[2], install=body.get("install") is True)
                handler._json({"skill_id": ref.skill_id, "version": ref.version, "sha256": ref.sha256,
                               "transport_sha256": ref.transport_sha256, "size_bytes": ref.size_bytes,
                               "signature": ref.signature, "key_id": ref.key_id,
                               "expires_at": ref.expires_at, "url": ref.url})
                return True
            if len(tail) == 4 and tail[1] == "versions" and tail[3] == "installations":
                transfer.record_installation(principal, tail[0], tail[2], str(body.get("sha256") or ""))
                handler._json({"ok": True})
                return True
    except SkillError as exc:
        return _fail(handler, exc)
    handler._json({"error": "not found"}, 404)
    return True


def _publish(registry, principal, body):
    allowed = {"name", "version", "input_schema", "output_schema", "capabilities", "supported_file_types",
               "fixtures", "tests", "dependency_lock", "provenance", "files", "request_key", "skill_id", "share_with"}
    if set(body) - allowed:
        raise SkillError(400, "unknown fields; identity is derived from authentication")
    raw = body.get("files")
    try:
        if not isinstance(raw, dict):
            raise ValueError
        files = {p: base64.b64decode(v, validate=True) for p, v in raw.items()}
    except (ValueError, TypeError, binascii.Error):
        raise SkillError(400, "files must map paths to base64 content") from None
    metadata = {k: v for k, v in body.items() if k not in {"files", "request_key", "skill_id", "share_with"}}
    metadata["files"] = files
    skill_id = body.get("skill_id")
    if skill_id is not None and not isinstance(skill_id, str):
        raise SkillError(400, "invalid skill_id")
    share = body.get("share_with") or []
    if not isinstance(share, list):
        raise SkillError(400, "share_with must be a list")
    done = registry.publish(principal, metadata, request_key=body.get("request_key"),
                            skill_id=skill_id, share_with=share)
    return dict(skill_id=done.skill_id, version=done.version, version_id=done.version_id,
                      sha256=done.sha256)
