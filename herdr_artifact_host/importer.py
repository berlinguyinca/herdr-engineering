"""One-time import of member-owned artifacts from the control plane, and the host-side verify of that import.

Run by the operator, never by the host.  The source is read through a read-only, repeatable-read transaction on a plain
PostgreSQL DSN plus the source blob store (a directory laid out as the control plane's RustFS bucket, or the bucket
itself through boto3); nothing here imports the control-plane package.

What is copied (the host stores BARE member ids, so a leading ``member:`` is stripped from owners, creators, ACL
principals, grantors, claim members and the like):

* artifacts whose ``created_by LIKE 'member:%'`` with their versions, ACL, library items, shares (token digests carried
  unchanged, so existing share URLs stay valid), share events, claims and annotations; library folders;
* the per-member usage, audit events and idempotency rows;
* every blob referenced by a non-purged imported version, by hash.  Each blob is hashed while it is staged; a byte that
  differs from its address aborts the run before anything is written (fail closed).

Non-member artifacts, and the shares and claims that point at them, are not imported.

Idempotent: rows are inserted ``ON CONFLICT DO NOTHING`` and, in the same transaction, every source row is compared with
what the host holds under its key, so a re-run adds nothing and a conflicting pre-existing row aborts (rolled back).

The manifest (``--manifest``) is written atomically after the commit: per-table counts and a digest per row key, blob
count, total bytes and the sha256 of every blob.  ``verify`` recomputes all of it on the host side and exits non-zero on
any difference.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import asyncpg

from .blobs import BlobCorrupt, BlobStore, validate_sha
from .db import LOCK_NS_BLOB

MANIFEST_VERSION = 1
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
_CHUNK = 1 << 20
_SHOWN = 20  # differences printed by verify


class ImportFailed(Exception):
    """The import (or verify) must abort; nothing was committed."""


class SourceBlobMissing(ImportFailed):
    pass


# --- source blob stores ------------------------------------------------------------------------------------------


class SourceBlobs(Protocol):
    def open(self, sha: str) -> BinaryIO: ...


class DirSource:
    """``<root>/sha256/xx/yy/<sha>``: the control plane's content key, under a directory (a mounted or synced bucket)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def open(self, sha: str) -> BinaryIO:
        validate_sha(sha)
        try:
            return (self._root / "sha256" / sha[:2] / sha[2:4] / sha).open("rb")
        except FileNotFoundError as exc:
            raise SourceBlobMissing(f"source blob {sha} is missing") from exc


class S3Source:
    """The source RustFS/S3 bucket through boto3 (imported lazily; the host image does not need it)."""

    def __init__(self, endpoint: str, bucket: str, region: str, access_key: str, secret_key: str) -> None:
        import boto3

        self._bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    @classmethod
    def from_file(cls, path: Path) -> S3Source:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(raw["endpoint"], raw["bucket"], raw.get("region", "us-east-1"), raw["access_key"], raw["secret_key"])

    def open(self, sha: str) -> BinaryIO:
        validate_sha(sha)
        try:
            body = self._s3.get_object(Bucket=self._bucket, Key=f"sha256/{sha[:2]}/{sha[2:4]}/{sha}")["Body"]
        except self._s3.exceptions.NoSuchKey as exc:
            raise SourceBlobMissing(f"source blob {sha} is missing") from exc
        return body  # type: ignore[no-any-return]  # StreamingBody: read(n) and close()


# --- table specs ---------------------------------------------------------------------------------------------------


def _bare(col: str) -> str:
    return f"regexp_replace({col}, '^member:', '')"


MEMBER_ARTIFACTS = "(SELECT artifact_id FROM artifacts WHERE created_by LIKE 'member:%')"
IMPORTED_SHARES = f"(SELECT share_id FROM artifact_shares WHERE artifact_id IN {MEMBER_ARTIFACTS})"
_LIVE_VERSION = "v.purged_at IS NULL AND v.state <> 'purged'"
BLOB_SHAS = (
    "SELECT e->>'sha256' FROM artifact_versions v, jsonb_array_elements(v.output_manifest) e "
    f"WHERE v.artifact_id IN {MEMBER_ARTIFACTS} AND {_LIVE_VERSION}"
)


@dataclass(frozen=True)
class TableSpec:
    host: str  # host table
    cols: tuple[str, ...]  # host columns written, in the order the source query returns them
    keys: tuple[str, ...]  # primary key columns (a subset of cols)
    select: str  # source query returning exactly ``cols``
    identity: str | None = None  # a GENERATED ALWAYS identity column whose source values are kept


SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "artifact_blobs",
        ("sha256", "size_bytes", "media_type", "created_at"),
        ("sha256",),
        "SELECT DISTINCT ON (blob_sha256) blob_sha256, size_bytes, media_type, created_at FROM artifact_blobs "
        f"WHERE blob_sha256 IN ({BLOB_SHAS}) ORDER BY blob_sha256, created_at",
    ),
    TableSpec(
        "artifacts",
        (
            "artifact_id", "owner", "original_filename", "display_name", "artifact_type", "media_type", "size_bytes",
            "intake", "lifecycle", "legal_hold", "current_version_id", "tombstoned_at", "purge_after", "created_by",
            "created_at", "updated_at", "created_via",
        ),
        ("artifact_id",),
        f"SELECT artifact_id, {_bare('created_by')}, original_filename, display_name, artifact_type, mime_type, "
        "coalesce(size_bytes, 0), intake, lifecycle, legal_hold, current_version_id, tombstoned_at, purge_after, "
        f"{_bare('created_by')}, created_at, created_at, CASE WHEN intake = 'pending' THEN 'agent' ELSE 'web' END "
        "FROM artifacts WHERE created_by LIKE 'member:%'",
    ),
    TableSpec(
        "artifact_versions",
        (
            "version_id", "artifact_id", "version_number", "parent_version_ids", "state", "output_manifest",
            "provenance", "created_via", "collaborator_agent_id", "created_by", "created_at", "purged_at",
        ),
        ("version_id",),
        "SELECT version_id, artifact_id, version_number, parent_version_ids, "
        "CASE WHEN state IN ('draft', 'ready', 'purged') THEN state WHEN purged_at IS NOT NULL THEN 'purged' "
        "ELSE 'ready' END, "
        "coalesce((SELECT jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' THEN e - 'storage_key' ELSE e END "
        "ORDER BY o) FROM jsonb_array_elements(output_manifest) WITH ORDINALITY t(e, o)), '[]'::jsonb), "
        f"provenance, created_via, collaborator_agent_id, {_bare('created_by')}, created_at, purged_at "
        f"FROM artifact_versions WHERE artifact_id IN {MEMBER_ARTIFACTS}",
    ),
    TableSpec(
        "artifact_acl",
        ("artifact_id", "principal_id", "permissions", "expires_at", "granted_by", "created_at"),
        ("artifact_id", "principal_id"),
        f"SELECT artifact_id, {_bare('principal_id')}, "
        "ARRAY(SELECT jsonb_array_elements_text(permissions))::text[], expires_at, "
        f"{_bare('granted_by')}, created_at FROM artifact_acl WHERE artifact_id IN {MEMBER_ARTIFACTS}",
    ),
    TableSpec(
        "library_folders",
        ("folder_id", "owner", "name", "created_at"),
        ("folder_id",),
        f"SELECT folder_id, {_bare('owner')}, name, created_at FROM artifact_library_folders",
    ),
    TableSpec(
        "library_items",
        ("folder_id", "artifact_id", "source", "filed_at"),
        ("folder_id", "artifact_id"),
        "SELECT folder_id, artifact_id, source, filed_at FROM artifact_library_items "
        f"WHERE artifact_id IN {MEMBER_ARTIFACTS}",
    ),
    TableSpec(
        "shares",
        (
            "share_id", "artifact_id", "version_id", "audience", "token_hash", "allow_emails", "allow_groups", "state",
            "entry", "file_count", "total_bytes", "title", "expires_at", "created_by", "created_at", "approved_by",
            "approved_at", "revoked_at",
        ),
        ("share_id",),
        "SELECT share_id, artifact_id, version_id, audience, token_hash, allow_emails, allow_groups, state, entry, "
        f"file_count, total_bytes, title, expires_at, {_bare('created_by')}, created_at, {_bare('approved_by')}, "
        f"approved_at, revoked_at FROM artifact_shares WHERE artifact_id IN {MEMBER_ARTIFACTS}",
    ),
    TableSpec(
        "share_events",
        ("event_id", "share_id", "at", "kind", "actor", "detail", "ingest_key"),
        ("event_id",),
        f"SELECT event_id, share_id, at, kind, {_bare('actor')}, detail, ingest_key FROM artifact_share_events "
        f"WHERE share_id IN {IMPORTED_SHARES}",
        identity="event_id",
    ),
    TableSpec(
        "share_claims",
        ("member_id", "share_id", "title", "sharer", "claimed_at"),
        ("member_id", "share_id"),
        f"SELECT {_bare('member_id')}, share_id, title, {_bare('sharer')}, claimed_at FROM artifact_share_claims "
        f"WHERE share_id IN {IMPORTED_SHARES}",
    ),
    TableSpec(
        "annotations",
        (
            "annotation_id", "artifact_id", "version_id", "share_id", "file_name", "anchor", "body", "author_identity",
            "author_email", "author_role", "status", "addressed_in_version_id", "resolved_by", "resolved_at",
            "edited_at", "withdrawn_at", "created_at", "ingested_at", "updated_at", "ingest_key",
        ),
        ("annotation_id",),
        "SELECT annotation_id, artifact_id, version_id, share_id, file_name, anchor, body, author_identity, "
        "author_email, author_role, status, addressed_in_version_id, resolved_by, resolved_at, edited_at, "
        "withdrawn_at, created_at, ingested_at, updated_at, ingest_key FROM artifact_annotations "
        f"WHERE artifact_id IN {MEMBER_ARTIFACTS}",
    ),
    TableSpec(
        "member_usage",
        (
            "member_id", "max_artifact_bytes", "max_member_bytes", "max_artifacts", "bytes_used", "bytes_retracted",
            "sharing_disabled", "display_name", "last_used_at", "updated_at",
        ),
        ("member_id",),
        f"SELECT {_bare('member_principal')}, max_artifact_bytes, max_member_bytes, max_artifacts, bytes_used, "
        "bytes_retracted, sharing_disabled, display_name, last_used_at, updated_at FROM artifact_member_usage "
        "WHERE member_principal LIKE 'member:%'",
    ),
    TableSpec(
        "artifact_member_events",
        ("id", "member_id", "action", "outcome", "code", "share_id", "audience", "created_at"),
        ("id",),
        f"SELECT event_id, {_bare('member_principal')}, action, outcome, code, share_id, audience, at "
        "FROM artifact_member_events WHERE member_principal LIKE 'member:%'",
        identity="id",
    ),
    TableSpec(
        "artifact_member_creates",
        ("member_id", "idempotency_key", "request_hash", "artifact_id", "created_at"),
        ("member_id", "idempotency_key"),
        f"SELECT {_bare('member_principal')}, idempotency_key, request_sha256, artifact_id, created_at "
        "FROM artifact_member_creates WHERE member_principal LIKE 'member:%'",
    ),
)
SPEC_BY_TABLE = {s.host: s for s in SPECS}

Row = tuple[Any, ...]


def _norm(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def row_key(spec: TableSpec, row: Row) -> str:
    return json.dumps([_norm(row[spec.cols.index(k)]) for k in spec.keys], separators=(",", ":"))


def row_digest(row: Row) -> str:
    blob = json.dumps(_norm(list(row)), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def keyed_digests(spec: TableSpec, rows: Sequence[Row]) -> dict[str, str]:
    return {row_key(spec, r): row_digest(r) for r in rows}


async def _jsonb_codec(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def _host_rows(conn: asyncpg.Connection, spec: TableSpec) -> list[Row]:
    return [tuple(r) for r in await conn.fetch(f"SELECT {', '.join(spec.cols)} FROM {spec.host}")]


# --- blob staging --------------------------------------------------------------------------------------------------


def stage_blob(source: SourceBlobs, sha: str, expected_size: int, scratch: Path) -> Path:
    """Copy one source blob to a scratch file, hashing as it goes; the bytes must match the address and the size."""
    digest = hashlib.sha256()
    size = 0
    fd, name = tempfile.mkstemp(dir=scratch, prefix="imp-")
    try:
        with os.fdopen(fd, "wb") as out, contextlib.closing(source.open(sha)) as src:
            while chunk := src.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != sha:
            raise ImportFailed(f"source blob {sha} is corrupt: its bytes hash to {digest.hexdigest()}")
        if size != expected_size:
            raise ImportFailed(f"source blob {sha} is {size} bytes, the source index says {expected_size}")
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise
    return Path(name)


def _host_has_intact(store: BlobStore, sha: str) -> bool:
    try:
        store.open(sha).close()
    except (FileNotFoundError, BlobCorrupt):
        return False
    return True


# --- import --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    manifest: dict[str, Any]
    inserted: dict[str, int]


async def _read_source(dsn: str) -> dict[str, list[Row]]:
    conn = await asyncpg.connect(dsn, server_settings={"default_transaction_read_only": "on"})
    try:
        await _jsonb_codec(conn)
        out: dict[str, list[Row]] = {}
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            for spec in SPECS:
                out[spec.host] = [tuple(r) for r in await conn.fetch(spec.select)]
            referenced = {r[0] for r in await conn.fetch(BLOB_SHAS)}
        missing = sorted(referenced - {r[0] for r in out["artifact_blobs"]})
        if missing:
            raise ImportFailed(f"{len(missing)} referenced blob(s) have no row in the source blob index: {missing[0]}...")
        for sha in referenced:
            validate_sha(sha)
        return out
    finally:
        await conn.close()


async def run_import(
    source_dsn: str,
    source_blobs: SourceBlobs,
    host_dsn: str,
    store: BlobStore,
    manifest_path: Path,
    *,
    log: Callable[[str], None] = lambda _m: None,
) -> ImportResult:
    data = await _read_source(source_dsn)
    blob_rows = data["artifact_blobs"]
    sizes = {r[0]: int(r[1]) for r in blob_rows}
    scratch = Path(tempfile.mkdtemp(dir=store.tmp_dir, prefix="import-"))
    staged: dict[str, Path] = {}
    newly_installed: list[str] = []
    inserted: dict[str, int] = {}
    try:
        # 1. Stage and verify every blob the host lacks.  Nothing is written to the host until all are good.
        for sha in sorted(sizes):
            if not _host_has_intact(store, sha):
                staged[sha] = await asyncio.to_thread(stage_blob, source_blobs, sha, sizes[sha], scratch)
        # 2. One transaction: per-sha locks, blob install, rows, comparison.
        conn = await asyncpg.connect(host_dsn)
        try:
            await _jsonb_codec(conn)
            try:
                async with conn.transaction():
                    for sha in sorted(sizes):
                        await conn.execute("SELECT pg_advisory_xact_lock($1, hashtext($2))", LOCK_NS_BLOB, sha)
                    for sha, path in staged.items():
                        existed = store.exists(sha)
                        with path.open("rb") as handle:
                            if store.put(handle) != sha:
                                raise ImportFailed(f"blob {sha} changed while it was installed")
                        if not existed:
                            newly_installed.append(sha)
                    for spec in SPECS:
                        inserted[spec.host] = await _insert(conn, spec, data[spec.host])
                        await _compare(conn, spec, data[spec.host])
            except BaseException:
                for sha in newly_installed:  # the rolled-back rows never referenced them
                    store.remove(sha)
                raise
        finally:
            await conn.close()
    finally:
        for path in staged.values():
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        with contextlib.suppress(OSError):
            scratch.rmdir()
    manifest = build_manifest(data, sizes)
    write_manifest(manifest_path, manifest)
    log(f"imported {sum(inserted.values())} new row(s) and {len(newly_installed)} new blob(s)")
    return ImportResult(manifest, inserted)


async def _insert(conn: asyncpg.Connection, spec: TableSpec, rows: Sequence[Row]) -> int:
    if not rows:
        return 0
    cols = ", ".join(spec.cols)
    marks = ", ".join(f"${i + 1}" for i in range(len(spec.cols)))
    override = " OVERRIDING SYSTEM VALUE" if spec.identity else ""
    before = await conn.fetchval(f"SELECT count(*) FROM {spec.host}")
    await conn.executemany(f"INSERT INTO {spec.host} ({cols}){override} VALUES ({marks}) ON CONFLICT DO NOTHING", rows)
    after = await conn.fetchval(f"SELECT count(*) FROM {spec.host}")
    if spec.identity:
        seq = f"pg_get_serial_sequence('{spec.host}', '{spec.identity}')"
        await conn.execute(
            f"SELECT setval({seq}, greatest((SELECT max({spec.identity}) FROM {spec.host}), 1))"
        )
    return int(after - before)


async def _compare(conn: asyncpg.Connection, spec: TableSpec, rows: Sequence[Row]) -> None:
    """Every source row must be on the host exactly as mapped (a conflicting pre-existing row aborts the import)."""
    want = keyed_digests(spec, rows)
    have = keyed_digests(spec, await _host_rows(conn, spec))
    bad = sorted(k for k, d in want.items() if have.get(k) != d)
    if bad:
        raise ImportFailed(f"{spec.host}: {len(bad)} row(s) differ from the source or conflict with host rows, e.g. {bad[0]}")


# --- manifest ------------------------------------------------------------------------------------------------------


def build_manifest(data: dict[str, list[Row]], sizes: dict[str, int]) -> dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "tables": {
            spec.host: {"count": len(data[spec.host]), "rows": keyed_digests(spec, data[spec.host])} for spec in SPECS
        },
        "blobs": {"count": len(sizes), "total_bytes": sum(sizes.values()), "items": dict(sorted(sizes.items()))},
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Temp file in the same directory, fsync, rename: a reader sees the old manifest or the whole new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(manifest, out, indent=1, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --- verify --------------------------------------------------------------------------------------------------------


async def verify(host_dsn: str, store: BlobStore, manifest_path: Path, *, exact: bool = False) -> list[str]:
    """Recompute the manifest on the host side; returns the differences (empty = identical).

    The imported set is checked (every manifest row present with the same content, every blob present, hashing to its
    address, of the recorded size), so rows the live host has added since are not differences.  ``exact`` also
    requires each table to hold exactly the manifest's rows (the state before the cutover)."""
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest unreadable: {exc}"]
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        return ["manifest has an unsupported version"]
    diffs: list[str] = []
    conn = await asyncpg.connect(host_dsn)
    try:
        await _jsonb_codec(conn)
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            for spec in SPECS:
                entry = manifest.get("tables", {}).get(spec.host)
                if entry is None:
                    diffs.append(f"{spec.host}: absent from the manifest")
                    continue
                have = keyed_digests(spec, await _host_rows(conn, spec))
                want: dict[str, str] = entry["rows"]
                diffs += [f"{spec.host}: row {k} is missing" for k in want if k not in have]
                diffs += [f"{spec.host}: row {k} differs" for k, d in want.items() if k in have and have[k] != d]
                if len(want) != entry["count"]:
                    diffs.append(f"{spec.host}: manifest count {entry['count']} does not match its rows")
                if exact and len(have) != len(want):
                    diffs.append(f"{spec.host}: host holds {len(have)} row(s), the manifest {len(want)}")
    finally:
        await conn.close()
    diffs += await asyncio.to_thread(_verify_blobs, store, manifest.get("blobs", {}))
    return diffs


def _verify_blobs(store: BlobStore, blobs: dict[str, Any]) -> list[str]:
    items: dict[str, int] = blobs.get("items", {})
    diffs: list[str] = []
    if blobs.get("count") != len(items) or blobs.get("total_bytes") != sum(items.values()):
        diffs.append("blobs: manifest count or total bytes do not match its items")
    for sha, size in items.items():
        try:
            handle = store.open(sha)
        except FileNotFoundError:
            diffs.append(f"blob {sha} is missing")
            continue
        except BlobCorrupt:
            diffs.append(f"blob {sha} does not hash to its address")
            continue
        with handle:
            actual = os.fstat(handle.fileno()).st_size
        if actual != size:
            diffs.append(f"blob {sha} is {actual} bytes, the manifest says {size}")
    return diffs


# --- command line --------------------------------------------------------------------------------------------------


def add_arguments(sub: Any) -> None:
    from .host_config import BLOB_DIR, DEFAULT_CONFIG_PATH

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--manifest", required=True, type=Path, help="manifest.json path")
        p.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="the migrate slice (schema owner) of the host")
        p.add_argument("--host-dsn", help="host database DSN (overrides --config)")
        p.add_argument("--blob-dir", default=BLOB_DIR, type=Path, help="host blob directory")

    imp = sub.add_parser("import", help="import member artifacts from the control plane", description=__doc__)
    common(imp)
    imp.add_argument("--source-dsn", required=True, help="control-plane DSN of a read-only role")
    group = imp.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-blob-dir", type=Path, help="directory laid out as the source bucket")
    group.add_argument("--source-s3-config", type=Path, help="JSON file: endpoint, bucket, region, access_key, secret_key")
    ver = sub.add_parser("verify", help="verify the host against a manifest", description=verify.__doc__)
    common(ver)
    ver.add_argument("--exact", action="store_true", help="also require no rows beyond the manifest's")


def _host_dsn(ns: argparse.Namespace) -> str:
    if ns.host_dsn:
        return str(ns.host_dsn)
    from .host_config import load

    cfg = load(ns.config)
    if cfg.role != "migrate":
        raise ValueError(f"the config is the {cfg.role} slice, not migrate")
    return cfg.dsn


def run_command(ns: argparse.Namespace) -> int:
    import sys

    name = ns.command
    try:
        host_dsn = _host_dsn(ns)
        store = BlobStore(ns.blob_dir)
        if name == "verify":
            diffs = asyncio.run(verify(host_dsn, store, ns.manifest, exact=ns.exact))
            for d in diffs[:_SHOWN]:
                print(f"herdr-artifact-host verify: {d}", file=sys.stderr)
            if len(diffs) > _SHOWN:
                print(f"herdr-artifact-host verify: ... and {len(diffs) - _SHOWN} more", file=sys.stderr)
            if diffs:
                print(f"herdr-artifact-host verify: FAILED ({len(diffs)} difference(s))", file=sys.stderr)
                return EXIT_FAILED
            print("verify: identical to the manifest")
            return EXIT_OK
        source: SourceBlobs = (
            DirSource(ns.source_blob_dir) if ns.source_blob_dir else S3Source.from_file(ns.source_s3_config)
        )
        result = asyncio.run(
            run_import(ns.source_dsn, source, host_dsn, store, ns.manifest, log=lambda m: print(f"import: {m}"))
        )
        problems = asyncio.run(verify(host_dsn, store, ns.manifest))
        if problems:
            for d in problems[:_SHOWN]:
                print(f"herdr-artifact-host import: post-import verify: {d}", file=sys.stderr)
            return EXIT_FAILED
        counts = ", ".join(f"{t}={v['count']}" for t, v in result.manifest["tables"].items())
        print(f"import: ok; blobs={result.manifest['blobs']['count']}; {counts}")
        return EXIT_OK
    except (ImportFailed, ValueError, OSError, asyncpg.PostgresError) as exc:
        print(f"herdr-artifact-host {name}: {type(exc).__name__}: {re.sub(r'://[^@\s]*@', '://***@', str(exc))}", file=sys.stderr)
        return EXIT_FAILED if not isinstance(exc, ValueError) else EXIT_USAGE
