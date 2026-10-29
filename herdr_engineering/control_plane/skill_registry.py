"""Skill registry on the artifact registry (flag ``control_plane.skill_registry.enabled``, off by default).

A skill is a registry artifact of ``artifact_type='skill'``: the artifact id is the skill id, every published skill
version is one immutable artifact version, and the signed package bytes live in RustFS (content addressed). There is
no second store: sharing is an artifact ACL grant, search is the registry's ACL-filtered listing. Skills are untrusted
data; this module never imports, installs or executes them. Verification proves integrity and authenticity, NOT code
safety. Signing keys come only from server configuration.

Packages are bounded canonical UTF-8 JSON (sorted keys, compact separators, not RFC 8785) with base64 file bytes.
SHA-256 and Ed25519 cover the entire unsigned body including manifest, file bytes and key id.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MAX_PACKAGE_BYTES = 16 * 1024 * 1024
ARTIFACT_TYPE = "skill"
PACKAGE_NAME = "package.json"
MEDIA_TYPE = "application/vnd.herdr.skill+json"
_FIELDS = {'name', 'version', 'input_schema', 'output_schema', 'capabilities',
           'supported_file_types', 'fixtures', 'tests', 'dependency_lock', 'provenance'}
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class SkillError(Exception):
    """A refusal with an HTTP status. Missing and forbidden are both 404 (no existence leak)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _relative_path(value: str) -> list[str]:
    if (not isinstance(value, str) or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("expected a normalized relative path")
    return value.split("/")


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("manifest JSON keys must be strings")
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is None or type(value) in {str, bool, int}:
        return
    elif type(value) is not float or not math.isfinite(value):
        raise ValueError("manifest values must be finite JSON values")


def _canonical(value: Any) -> bytes:
    _validate_json(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _validate_content(metadata: dict, files: dict) -> None:
    if (not isinstance(files, dict) or not 1 <= len(files) <= 1000
            or not files.get('SKILL.md') or any(type(v) is not bytes for v in files.values())):
        raise ValueError('package requires SKILL.md and byte-valued files')
    for path in files:
        _relative_path(path)
        if len(path) > 1024 or ':' in path:
            raise ValueError('invalid package path')
    if sum(map(len, files.values())) > MAX_PACKAGE_BYTES:
        raise ValueError('package too large')
    for field in ('name', 'version'):
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            raise ValueError('name and version are required')
    if len(metadata['name']) > 200 or not _VERSION.fullmatch(metadata['version']):
        raise ValueError('invalid name or version')
    for field in ('input_schema', 'output_schema', 'provenance'):
        if not isinstance(metadata.get(field), dict):
            raise ValueError('schemas and provenance must be objects')
    if not metadata['provenance']:
        raise ValueError('provenance is required')
    for field in ('capabilities', 'supported_file_types', 'fixtures', 'tests'):
        value = metadata.get(field)
        if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
            raise ValueError('expected a list of strings')
        if len(set(value)) != len(value):
            raise ValueError('duplicate manifest entries')
    if not metadata['fixtures'] and not metadata['tests']:
        raise ValueError('fixtures or tests are required')
    references = [*metadata['fixtures'], *metadata['tests'], metadata.get('dependency_lock')]
    if any(not isinstance(p, str) or p not in files for p in references):
        raise ValueError('missing fixture, test or dependency lock')
    _canonical({k: metadata[k] for k in _FIELDS})


@dataclass(frozen=True)
class SkillVersion:
    skill_id: str
    version: str
    version_id: str
    storage_key: str
    sha256: str


@dataclass(frozen=True)
class SkillPackage:
    manifest: dict
    files: dict[str, bytes]
    sha256: str
    signature: str
    key_id: str


@dataclass(frozen=True)
class VerificationReport:
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _body(package: SkillPackage) -> dict:
    return {'manifest': package.manifest, 'key_id': package.key_id,
            'files': {p: base64.b64encode(b).decode('ascii') for p, b in package.files.items()}}


def _encode(package: SkillPackage) -> bytes:
    data = _canonical({**_body(package), 'sha256': package.sha256, 'signature': package.signature})
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError('package too large')
    return data


def decode_package(data: bytes) -> SkillPackage:
    obj = json.loads(data)
    return SkillPackage(obj['manifest'],
                        {p: base64.b64decode(b, validate=True) for p, b in obj['files'].items()},
                        obj['sha256'], obj['signature'], obj['key_id'])


def verify_skill_package(package: SkillPackage, trusted_keys: dict) -> VerificationReport:
    """Verify a detached package using configured public keys, never its own key."""
    try:
        _validate_content(package.manifest, package.files)
        m = package.manifest
        if (type(m.get('schema_version')) is not int or m['schema_version'] != 1
                or m.get('trust') != 'untrusted' or m.get('execution') != 'harness_only'):
            raise ValueError('invalid manifest')
        for field in ('skill_id', 'creator_id'):
            if not isinstance(m.get(field), str) or not m[field].strip():
                raise ValueError('missing package identity')
        if datetime.fromisoformat(m['created_at']).tzinfo is None:
            raise ValueError('created_at requires a timezone')
        expected = {p: {'sha256': hashlib.sha256(b).hexdigest(), 'size_bytes': len(b)}
                    for p, b in package.files.items()}
        if m.get('files') != expected:
            return VerificationReport(('file checksum mismatch',))
        body = _canonical(_body(package))
        _encode(package)
        if hashlib.sha256(body).hexdigest() != package.sha256:
            return VerificationReport(('package checksum mismatch',))
        trusted_keys[package.key_id].verify(bytes.fromhex(package.signature), body)
    except Exception:
        return VerificationReport(('invalid package or untrusted signature',))
    return VerificationReport(())


class SkillRegistry:
    """Synchronous service over the artifact store adapter (``call(method, *args, **kwargs)``) and RustFS.

    Authorization is the registry's own: owner or a live ``artifact_acl`` grant (``report_permission``). A skill that the
    principal cannot read is indistinguishable from one that does not exist.
    """

    def __init__(self, call, rustfs, *, signing_key, key_id: str, trusted_keys: dict, on_grant=None):
        if not key_id or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', key_id):
            raise ValueError('invalid signing key id')
        self.call, self.rustfs = call, rustfs
        self.signing_key, self.key_id = signing_key, key_id
        self.trusted_keys = dict(trusted_keys)
        self.on_grant = on_grant
        # Fail startup if configuration cannot verify its own signatures.
        self.trusted_keys[key_id].verify(signing_key.sign(b'skill-registry'), b'skill-registry')

    def verify_skill_package(self, package: SkillPackage) -> VerificationReport:
        return verify_skill_package(package, self.trusted_keys)

    def _sign(self, manifest: dict, files: dict[str, bytes]) -> SkillPackage:
        unsigned = SkillPackage(manifest, files, '', '', self.key_id)
        body = _canonical(_body(unsigned))
        package = SkillPackage(manifest, files, hashlib.sha256(body).hexdigest(),
                               self.signing_key.sign(body).hex(), self.key_id)
        _encode(package)
        return package

    def _can(self, principal: str, skill_id: str, permission: str) -> bool:
        return bool(isinstance(skill_id, str) and skill_id
                    and self.call('report_permission', skill_id, principal, permission))

    def _skill_versions(self, skill_id: str) -> list[dict]:
        return [v for v in self.call('list_versions', skill_id)
                if isinstance(v.get('provenance'), dict) and isinstance(v['provenance'].get('skill'), dict)]

    def _row(self, principal: str, skill_id: str, version: str) -> dict:
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise SkillError(400, 'invalid skill identity')
        if not self._can(principal, skill_id, 'read'):
            raise SkillError(404, 'not found')
        for row in self._skill_versions(skill_id):
            if row['provenance']['skill'].get('version') == version and row.get('state') == 'ready':
                return row
        raise SkillError(404, 'not found')

    def publish(self, principal: str, metadata: dict, *, request_key: str, skill_id: str | None = None,
                share_with=()) -> SkillVersion:
        """Publish one immutable skill version as an artifact version. ``share_with`` grants read to those principals."""
        try:
            if not isinstance(metadata, dict) or set(metadata) - _FIELDS - {'files'}:
                raise ValueError('unknown metadata fields')
            if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
                raise ValueError('a caller-supplied request_key is required')
            _validate_content(metadata, metadata.get('files'))
            share = [str(p) for p in share_with]
            if len(share) > 100 or any(not p or len(p) > 300 for p in share):
                raise ValueError('invalid share list')
        except (ValueError, TypeError, KeyError):
            raise SkillError(400, 'invalid skill metadata') from None
        files = dict(metadata['files'])
        version = metadata['version']
        base = None
        if skill_id is None:
            created = self.call('register_logical', original_filename=metadata['name'],
                                artifact_type=ARTIFACT_TYPE, mime_type=MEDIA_TYPE, created_by=principal)
            skill_id = created['artifact_id']
            if self.on_grant:
                self.on_grant(skill_id, principal, {'read', 'edit', 'review', 'share', 'admin'}, principal)
        else:
            if not self._can(principal, skill_id, 'edit'):
                raise SkillError(404, 'not found')
            expected = {p: {'sha256': hashlib.sha256(b).hexdigest(), 'size_bytes': len(b)} for p, b in files.items()}
            for v in self._skill_versions(skill_id):
                meta = v['provenance']['skill']
                if meta.get('version') != version:
                    continue
                old = meta['manifest']
                # A retry of the same publish returns the stored version; anything else is immutable.
                if old.get('files') == expected and all(old.get(k) == metadata[k] for k in _FIELDS):
                    out = v['output_manifest'][0]
                    return SkillVersion(skill_id, version, v['version_id'], out['storage_key'], meta['sha256'])
                raise SkillError(409, 'version is immutable')
            head = self.call('list_versions', skill_id)
            base = head[0]['version_id'] if head else None
        manifest = {**{k: metadata[k] for k in _FIELDS}, 'schema_version': 1, 'skill_id': skill_id,
                    'creator_id': principal, 'trust': 'untrusted', 'execution': 'harness_only',
                    'created_at': datetime.now(UTC).isoformat(),
                    'files': {p: {'sha256': hashlib.sha256(b).hexdigest(), 'size_bytes': len(b)}
                              for p, b in files.items()}}
        try:
            package = self._sign(manifest, files)
        except ValueError:
            raise SkillError(400, 'invalid skill metadata') from None
        data = _encode(package)
        key, digest = self.rustfs.put(data)
        output = [{'name': PACKAGE_NAME, 'storage_key': key, 'sha256': digest, 'size_bytes': len(data),
                   'media_type': MEDIA_TYPE}]
        provenance = {'principal_id': principal, 'request_key': request_key,
                      'skill': {'version': version, 'sha256': package.sha256, 'signature': package.signature,
                                'key_id': package.key_id, 'manifest': manifest}}
        committed = self.call('commit_version', skill_id, base_version_id=base, source_manifest_blob=None,
                              output_manifest=output, provenance=provenance, created_by=principal)
        for target in share:
            if target != principal and self.on_grant:
                self.on_grant(skill_id, target, {'read'}, principal)
        return SkillVersion(skill_id, version, committed['version_id'], key, package.sha256)

    def load(self, principal: str, skill_id: str, version: str) -> SkillPackage:
        """Fetch, re-verify against the stored manifest and return the package. Principal is mandatory."""
        row = self._row(principal, skill_id, version)
        meta = row['provenance']['skill']
        try:
            out = row['output_manifest'][0]
            data = self.rustfs.get(out['storage_key'], out['sha256'])
            if len(data) > MAX_PACKAGE_BYTES:
                raise ValueError('package too large')
            package = decode_package(data)
            if (data != _encode(package) or package.sha256 != meta['sha256'] or package.signature != meta['signature']
                    or package.key_id != meta['key_id'] or package.manifest != meta['manifest']
                    or package.manifest['skill_id'] != skill_id or package.manifest['version'] != version
                    or not self.verify_skill_package(package).ok):
                raise ValueError('package verification failed')
        except Exception:
            raise SkillError(409, 'package verification failed') from None
        # Re-check after the (slow) fetch: a revoked grant must not return bytes.
        if not self._can(principal, skill_id, 'read'):
            raise SkillError(404, 'not found')
        return package

    @staticmethod
    def view(package: SkillPackage) -> dict:
        return {'skill_id': package.manifest['skill_id'], 'version': package.manifest['version'],
                'sha256': package.sha256, 'manifest': package.manifest}

    def search(self, principal: str, query: str = '') -> list[dict]:
        """Skills the principal may read. Authorization happens in SQL before LIMIT, then per artifact."""
        if not isinstance(query, str) or len(query) > 200:
            raise SkillError(400, 'invalid skill search')
        records = self.call('list_records', principal_id=principal, query=query or None,
                            artifact_type=ARTIFACT_TYPE, limit=50, offset=0)
        out = []
        for record in records:
            aid = record['artifact_id']
            if not self._can(principal, aid, 'read'):
                continue
            for row in reversed(self._skill_versions(aid)):
                meta = row['provenance']['skill']
                out.append({'skill_id': aid, 'version': meta['version'], 'sha256': meta['sha256'],
                            'name': meta['manifest'].get('name'),
                            'capabilities': meta['manifest'].get('capabilities', [])})
        return out
