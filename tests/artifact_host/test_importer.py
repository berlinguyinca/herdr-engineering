"""Import and verify: a real source Postgres built from the control plane's real migrations, a real host Postgres and
real blob directories (no mocks)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from herdr_artifact_host import importer, share_store
from herdr_artifact_host.cli import main
from herdr_artifact_host.db import create_pool

MIGRATIONS = Path(__file__).resolve().parents[2] / "deploy" / "control-plane" / "migrations"
ALIASES = {  # renamed copies of 0003-0007 (see control_plane.db.MIGRATION_ALIASES): replaying them would fail
    "0008_artifact_registry.sql", "0009_artifact_publications.sql", "0010_artifact_binding_context.sql",
    "0011_fabric_scheduler.sql", "0012_artifact_acl.sql", "0013_handoffs.sql",
}
TOKEN = hashlib.sha256(b"share-token-1").hexdigest()
ANN = "ann_" + "A" * 22
FLD = "fld_" + "0" * 24


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run[T](coro: Any) -> T:
    return asyncio.run(coro)  # type: ignore[no-any-return]


async def _exec(dsn: str, sql: str, *args: Any) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


async def _fetch(dsn: str, sql: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def sql(dsn: str, text: str, *args: Any) -> None:
    run(_exec(dsn, text, *args))


def rows(dsn: str, text: str, *args: Any) -> list[asyncpg.Record]:
    return run(_fetch(dsn, text, *args))


@dataclass
class Env:
    source: str
    host: str
    src_blobs: Path
    host_blobs: Path
    manifest: Path
    blobs: dict[str, bytes]

    def args(self, command: str = "import", *extra: str) -> list[str]:
        base = [command, "--manifest", str(self.manifest), "--host-dsn", self.host, "--blob-dir", str(self.host_blobs)]
        if command == "import":
            base += ["--source-dsn", self.source, "--source-blob-dir", str(self.src_blobs)]
        return base + list(extra)

    def run(self, command: str = "import", *extra: str) -> int:
        return main(self.args(command, *extra))

    def src_path(self, data: bytes) -> Path:
        h = sha(data)
        return self.src_blobs / "sha256" / h[:2] / h[2:4] / h

    def host_blob(self, data: bytes) -> Path:
        h = sha(data)
        return self.host_blobs / "sha256" / h[:2] / h[2:4] / h

    def host_count(self, table: str) -> int:
        return int(rows(self.host, f"SELECT count(*) AS n FROM {table}")[0]["n"])


def put_source_blob(env_dir: Path, data: bytes) -> None:
    h = sha(data)
    path = env_dir / "sha256" / h[:2] / h[2:4] / h
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def seed_blob(dsn: str, root: Path, data: bytes) -> None:
    put_source_blob(root, data)
    h = sha(data)
    sql(dsn, "INSERT INTO artifact_blobs (blob_sha256, storage_key, size_bytes, media_type) VALUES ($1,$2,$3,'text/html')",
        h, f"sha256/{h[:2]}/{h[2:4]}/{h}", len(data))


def manifest_json(*entries: tuple[str, bytes]) -> str:
    return json.dumps([{"name": n, "sha256": sha(d), "size_bytes": len(d), "media_type": "text/html",
                        "storage_key": f"sha256/{sha(d)[:2]}/{sha(d)[2:4]}/{sha(d)}"} for n, d in entries])


def seed_member(env: Env) -> None:
    s = env.source
    b1, b2 = env.blobs["b1"], env.blobs["b2"]
    sql(s, "INSERT INTO artifacts (artifact_id, created_by, display_name, mime_type, size_bytes, artifact_type) "
           "VALUES ('art_m1','member:alice','Report','text/html',10,'html')")
    sql(s, "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, output_manifest, created_by) "
           "VALUES ('ver_1','art_m1',1,'ready',$1::jsonb,'member:alice')", manifest_json(("index.html", b1)))
    sql(s, "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, output_manifest, created_by, "
           "parent_version_ids) VALUES ('ver_2','art_m1',2,'ready',$1::jsonb,'member:alice','{ver_1}')",
        manifest_json(("index.html", b2)))
    sql(s, "UPDATE artifacts SET current_version_id='ver_2' WHERE artifact_id='art_m1'")
    sql(s, "INSERT INTO artifact_acl (artifact_id, principal_id, permissions, granted_by) "
           "VALUES ('art_m1','member:bob','[\"read\",\"write\"]','member:alice')")
    sql(s, "INSERT INTO artifact_library_folders (folder_id, owner, name) VALUES ($1,'member:alice','Papers')", FLD)
    sql(s, "INSERT INTO artifact_library_items (folder_id, artifact_id, source) VALUES ($1,'art_m1','manual')", FLD)
    sql(s, "INSERT INTO artifact_shares (share_id, artifact_id, version_id, audience, token_hash, state, created_by, "
           "approved_by) VALUES ('shr_1','art_m1','ver_2','public',$1,'live','member:alice','member:alice')", TOKEN)
    sql(s, "INSERT INTO artifact_share_events (share_id, kind, actor, detail) VALUES ('shr_1','created','member:alice','{\"a\":1}')")
    sql(s, "INSERT INTO artifact_share_events (share_id, kind, actor, ingest_key) VALUES ('shr_1','opened','anon','k1')")
    sql(s, "INSERT INTO artifact_share_claims (member_id, share_id, title, sharer) VALUES ('member:bob','shr_1','T','member:alice')")
    sql(s, "INSERT INTO artifact_annotations (annotation_id, artifact_id, version_id, share_id, file_name, anchor, body, "
           "author_identity, author_email, author_role, created_at) VALUES ($1,'art_m1','ver_2','shr_1','index.html',"
           "'{\"x\":1}','hello','bob','bob@example.test','member', now())", ANN)
    sql(s, "INSERT INTO artifact_member_usage (member_principal, bytes_used, display_name) VALUES ('member:alice', 10, 'Alice')")
    sql(s, "INSERT INTO artifact_member_events (member_principal, actor, action, outcome) VALUES ('member:alice','{}','publish','ok')")
    sql(s, "INSERT INTO artifact_member_creates (member_principal, idempotency_key, request_sha256, artifact_id) "
           "VALUES ('member:alice','idem-1','abc','art_m1')")


def seed_non_member(env: Env) -> None:
    s = env.source
    sql(s, "INSERT INTO artifacts (artifact_id, created_by) VALUES ('art_ci','api-key:ci')")
    sql(s, "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, output_manifest) "
           "VALUES ('ver_ci','art_ci',1,'ready',$1::jsonb)", manifest_json(("r.html", env.blobs["b3"])))
    sql(s, "INSERT INTO artifact_shares (share_id, artifact_id, version_id, audience, token_hash, state) "
           "VALUES ('shr_ci','art_ci','ver_ci','public',$1,'live')", sha(b"ci-token"))
    sql(s, "INSERT INTO artifact_share_claims (member_id, share_id) VALUES ('member:bob','shr_ci')")


@pytest.fixture
def env(host_dsn_factory: Callable[[], str], migrated_dsn: str, tmp_path: Path) -> Env:
    source = host_dsn_factory()

    async def build() -> None:
        conn = await asyncpg.connect(source)
        try:
            for p in sorted(MIGRATIONS.glob("*.sql")):
                if p.name not in ALIASES:
                    await conn.execute(p.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    run(build())
    blobs = {"b1": b"<h1>one</h1>", "b2": b"<h1>two</h1>" * 50, "b3": b"<p>ci report</p>"}
    e = Env(source, migrated_dsn, tmp_path / "srcblobs", tmp_path / "hostblobs", tmp_path / "out" / "manifest.json", blobs)
    for data in blobs.values():
        seed_blob(source, e.src_blobs, data)
    return e


def test_import_copies_member_data_and_matches(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    seed_member(env)
    seed_non_member(env)
    assert env.run() == 0
    assert env.host_count("artifacts") == 1
    assert env.host_count("artifact_versions") == 2
    assert env.host_count("shares") == 1
    assert env.host_count("artifact_acl") == 1 and env.host_count("annotations") == 1
    assert env.host_count("share_events") == 2 and env.host_count("share_claims") == 1
    assert env.host_count("library_folders") == 1 and env.host_count("library_items") == 1
    art = rows(env.host, "SELECT owner, created_by, current_version_id, media_type FROM artifacts")[0]
    assert (art["owner"], art["created_by"], art["current_version_id"], art["media_type"]) == ("alice", "alice", "ver_2", "text/html")
    assert rows(env.host, "SELECT token_hash FROM shares")[0]["token_hash"] == TOKEN
    assert rows(env.host, "SELECT permissions FROM artifact_acl")[0]["permissions"] == ["read", "write"]
    m = json.loads(env.manifest.read_text())
    assert m["blobs"]["count"] == 2 and m["blobs"]["total_bytes"] == len(env.blobs["b1"]) + len(env.blobs["b2"])
    assert set(m["blobs"]["items"]) == {sha(env.blobs["b1"]), sha(env.blobs["b2"])}
    assert m["tables"]["artifacts"]["count"] == 1 and m["tables"]["artifact_versions"]["count"] == 2
    assert all("storage_key" not in e for r in rows(env.host, "SELECT output_manifest::text AS m FROM artifact_versions")
               for e in json.loads(r["m"]))
    for data in (env.blobs["b1"], env.blobs["b2"]):  # byte-identical blobs on the host
        h = sha(data)
        assert (env.host_blobs / "sha256" / h[:2] / h[2:4] / h).read_bytes() == data
    assert env.run("verify") == 0
    assert "identical" in capsys.readouterr().out


def test_member_prefix_stripped_everywhere(env: Env) -> None:
    seed_member(env)
    assert env.run() == 0
    checks = {
        "artifacts": "owner, created_by", "artifact_versions": "created_by", "artifact_acl": "principal_id, granted_by",
        "library_folders": "owner", "shares": "created_by, approved_by", "share_claims": "member_id, sharer",
        "share_events": "actor", "member_usage": "member_id", "artifact_member_events": "member_id",
        "artifact_member_creates": "member_id",
    }
    for table, cols in checks.items():
        for r in rows(env.host, f"SELECT {cols} FROM {table}"):
            assert not any(str(v).startswith("member:") for v in r.values()), (table, dict(r))
    assert rows(env.host, "SELECT member_id FROM share_claims")[0]["member_id"] == "bob"
    assert rows(env.host, "SELECT principal_id FROM artifact_acl")[0]["principal_id"] == "bob"


def test_non_member_artifacts_and_shares_not_imported(env: Env) -> None:
    seed_member(env)
    seed_non_member(env)
    sql(env.source, "INSERT INTO artifact_library_items (folder_id, artifact_id, source) VALUES ($1,'art_ci','auto')", FLD)
    assert env.run() == 0
    m = json.loads(env.manifest.read_text())
    assert [r["artifact_id"] for r in rows(env.host, "SELECT artifact_id FROM artifacts")] == ["art_m1"]
    assert [r["share_id"] for r in rows(env.host, "SELECT share_id FROM shares")] == ["shr_1"]
    assert [r["member_id"] for r in rows(env.host, "SELECT member_id FROM share_claims")] == ["bob"]
    assert sha(env.blobs["b3"]) not in m["blobs"]["items"]
    assert env.host_count("artifact_blobs") == 2
    assert not env.host_blob(env.blobs["b3"]).exists()
    skipped = m["skipped"]
    assert skipped["artifacts"] == 1 and skipped["shares"] == 1 and skipped["share_claims"] == 1
    assert skipped["library_items"] == 1 and skipped["annotations"] == 0


def test_zero_member_source_imports_cleanly(env: Env) -> None:
    seed_non_member(env)  # production today: only non-member artifacts and their shares
    assert env.run() == 0
    for spec in importer.SPECS:
        assert env.host_count(spec.host) == 0
    m = json.loads(env.manifest.read_text())
    assert m["blobs"] == {"count": 0, "total_bytes": 0, "items": {}}
    assert all(t["count"] == 0 for t in m["tables"].values())
    assert env.run("verify", "--exact") == 0


def test_rerun_is_a_no_op(env: Env) -> None:
    seed_member(env)
    assert env.run() == 0
    counts = {s.host: env.host_count(s.host) for s in importer.SPECS}
    before = rows(env.host, "SELECT share_id, token_hash FROM shares")
    first = json.loads(env.manifest.read_text())
    assert env.run() == 0
    assert {s.host: env.host_count(s.host) for s in importer.SPECS} == counts
    assert [dict(r) for r in rows(env.host, "SELECT share_id, token_hash FROM shares")] == [dict(r) for r in before]
    second = json.loads(env.manifest.read_text())
    assert (first["tables"], first["blobs"]) == (second["tables"], second["blobs"])
    assert env.run("verify", "--exact") == 0


def test_event_identity_continues_after_import(env: Env) -> None:
    seed_member(env)
    assert env.run() == 0
    sql(env.host, "INSERT INTO share_events (share_id, kind, actor) VALUES ('shr_1','denied','x')")
    ids = [r["event_id"] for r in rows(env.host, "SELECT event_id FROM share_events ORDER BY event_id")]
    assert len(ids) == len(set(ids)) == 3


def test_corrupted_source_blob_fails_closed(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    seed_member(env)
    path = env.src_path(env.blobs["b2"])
    data = bytearray(path.read_bytes())
    data[3] ^= 0xFF
    path.write_bytes(bytes(data))
    assert env.run() != 0
    assert "corrupt" in capsys.readouterr().err
    for spec in importer.SPECS:
        assert env.host_count(spec.host) == 0
    assert not env.manifest.exists()
    assert list((env.host_blobs / "sha256").rglob(sha(env.blobs["b1"]))) == []  # nothing installed before the check
    assert list((env.host_blobs / "tmp").iterdir()) == []


def test_missing_source_blob_fails_closed(env: Env) -> None:
    seed_member(env)
    env.src_path(env.blobs["b1"]).unlink()
    assert env.run() != 0
    assert env.host_count("artifacts") == 0 and not env.manifest.exists()


def test_row_failure_rolls_back_rows_and_new_blobs(env: Env) -> None:
    seed_member(env)
    # a host row that conflicts with the source share's token digest (different share): the import must abort
    sql(env.host, "INSERT INTO artifacts (artifact_id, owner) VALUES ('other','zed')")
    sql(env.host, "INSERT INTO artifact_versions (version_id, artifact_id, version_number) VALUES ('ov','other',1)")
    sql(env.host, "INSERT INTO shares (share_id, artifact_id, version_id, audience, token_hash) "
                  "VALUES ('shr_x','other','ov','public',$1)", TOKEN)
    assert env.run() != 0
    assert env.host_count("artifacts") == 1 and env.host_count("artifact_versions") == 1
    assert env.host_count("artifact_blobs") == 0
    assert [p for p in (env.host_blobs / "sha256").rglob("*") if p.is_file()] == []  # the installed blobs were removed
    assert not env.manifest.exists()


def test_manifest_write_failure_aborts_before_any_row_or_blob_is_committed(env: Env) -> None:
    seed_member(env)
    env.manifest.mkdir(parents=True)  # a directory where the file goes: the atomic rename cannot succeed
    assert env.run() != 0
    assert env.host_count("artifacts") == 0 and env.host_count("artifact_blobs") == 0
    assert [p for p in (env.host_blobs / "sha256").rglob("*") if p.is_file()] == []


def test_verify_fails_on_deleted_row(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    seed_member(env)
    assert env.run() == 0
    sql(env.host, "DELETE FROM annotations")
    assert env.run("verify") != 0
    assert "annotations" in capsys.readouterr().err


def test_verify_fails_on_changed_row(env: Env) -> None:
    seed_member(env)
    assert env.run() == 0
    sql(env.host, "UPDATE shares SET state='revoked'")
    assert env.run("verify") != 0


def test_verify_fails_on_changed_blob(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    seed_member(env)
    assert env.run() == 0
    h = sha(env.blobs["b1"])
    p = env.host_blobs / "sha256" / h[:2] / h[2:4] / h
    p.write_bytes(b"tampered")
    assert env.run("verify") != 0
    assert "does not hash" in capsys.readouterr().err


def test_verify_fails_on_missing_blob(env: Env) -> None:
    seed_member(env)
    assert env.run() == 0
    h = sha(env.blobs["b2"])
    (env.host_blobs / "sha256" / h[:2] / h[2:4] / h).unlink()
    assert env.run("verify") != 0


def test_verify_exact_flags_extra_rows_default_allows_them(env: Env) -> None:
    seed_member(env)
    assert env.run() == 0
    sql(env.host, "INSERT INTO artifacts (artifact_id, owner) VALUES ('later','zed')")
    assert env.run("verify") == 0
    assert env.run("verify", "--exact") != 0


def test_import_repairs_a_corrupt_host_blob(env: Env) -> None:
    seed_member(env)
    assert env.run() == 0
    h = sha(env.blobs["b1"])
    p = env.host_blobs / "sha256" / h[:2] / h[2:4] / h
    p.write_bytes(b"x")
    assert env.run() == 0
    assert p.read_bytes() == env.blobs["b1"]


def test_verify_without_manifest_fails(env: Env) -> None:
    assert env.run("verify") != 0


def test_manifest_write_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "m.json"
    importer.write_manifest(target, {"version": 1, "a": 1})
    importer.write_manifest(target, {"version": 1, "a": 2})
    assert json.loads(target.read_text())["a"] == 2
    assert [p.name for p in tmp_path.iterdir()] == ["m.json"]  # no temp litter
    assert os.stat(target).st_size > 0


def test_source_connection_is_read_only(env: Env) -> None:
    async def attempt() -> str:
        conn = await asyncpg.connect(env.source, server_settings={"default_transaction_read_only": "on"})
        try:
            await conn.execute("DELETE FROM artifacts")
        except asyncpg.ReadOnlySQLTransactionError:
            return "refused"
        finally:
            await conn.close()
        return "wrote"

    assert run(attempt()) == "refused"
    seed_member(env)
    assert env.run() == 0
    assert rows(env.source, "SELECT count(*) AS n FROM artifacts")[0]["n"] == 1


def test_share_on_retracted_version_is_imported_revoked_and_not_served(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    seed_member(env)
    token = "retracted-token"
    sql(env.source, "UPDATE artifact_versions SET state='retracted' WHERE version_id='ver_2'")
    sql(env.source, "INSERT INTO artifact_shares (share_id, artifact_id, version_id, audience, token_hash, state, "
                    "expires_at, created_by) VALUES ('shr_r','art_m1','ver_2','public',$1,'live', now() + interval '1 day', "
                    "'member:alice')", share_store.token_digest(token))
    assert env.run() == 0
    assert "revoked" in capsys.readouterr().out
    assert rows(env.host, "SELECT state FROM artifact_versions WHERE version_id='ver_2'")[0]["state"] == "ready"
    got = rows(env.host, "SELECT state, revoked_at FROM shares WHERE share_id='shr_r'")[0]
    assert got["state"] == "revoked" and got["revoked_at"] is not None
    assert rows(env.host, "SELECT state FROM shares WHERE share_id='shr_1'")[0]["state"] == "revoked"
    assert json.loads(env.manifest.read_text())["revoked_shares"] == 2

    async def lookup() -> share_store.Loaded:
        pool = await create_pool(env.host)
        try:
            return await share_store.load_share(pool, token, time.time())
        finally:
            await pool.close()

    loaded = run(lookup())
    assert loaded.state is None and loaded.reason == "not_live"
    assert env.run("verify") == 0
    assert env.run() == 0  # re-run keeps the same revoked rows
    assert rows(env.host, "SELECT state FROM shares WHERE share_id='shr_r'")[0]["state"] == "revoked"


def test_share_on_ready_version_stays_live_and_served(env: Env) -> None:
    seed_member(env)
    sql(env.source, "UPDATE artifact_shares SET expires_at = now() + interval '1 day'")
    assert env.run() == 0
    assert json.loads(env.manifest.read_text())["revoked_shares"] == 0

    async def lookup() -> share_store.Loaded:
        pool = await create_pool(env.host)
        try:
            return await share_store.load_share(pool, "share-token-1", time.time())
        finally:
            await pool.close()

    assert run(lookup()).state is not None


def test_purged_version_blobs_are_not_imported(env: Env) -> None:
    seed_member(env)
    sql(env.source, "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, purged_at, "
                    "output_manifest) VALUES ('ver_3','art_m1',3,'purged', now(), $1::jsonb)",
        manifest_json(("gone.html", env.blobs["b3"])))
    env.src_path(env.blobs["b3"]).unlink()  # purged bytes are gone from the source
    assert env.run() == 0
    m = json.loads(env.manifest.read_text())
    assert sha(env.blobs["b3"]) not in m["blobs"]["items"] and m["blobs"]["count"] == 2
    assert rows(env.host, "SELECT state FROM artifact_versions WHERE version_id='ver_3'")[0]["state"] == "purged"


@pytest.mark.parametrize("entry", ['{"name":"x"}', '{"name":"x","sha256":"nope"}', '{"name":"x","sha256":null}'])
def test_manifest_entry_without_valid_sha256_fails_closed_exit_1(
    env: Env, entry: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_member(env)
    sql(env.source, "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, output_manifest) "
                    "VALUES ('ver_bad','art_m1',9,'ready',$1::jsonb)", f"[{entry}]")
    assert env.run() == 1
    assert "sha256" in capsys.readouterr().err
    assert env.host_count("artifacts") == 0 and not env.manifest.exists()


def test_importer_source_connection_is_read_only(env: Env) -> None:
    seed_member(env)

    async def probe() -> tuple[str, str, str]:
        async with importer.open_source(env.source) as conn:
            tx = await conn.fetchval("SHOW transaction_read_only")
            default = await conn.fetchval("SHOW default_transaction_read_only")
            try:
                await conn.execute("DELETE FROM artifacts")
                outcome = "wrote"
            except asyncpg.ReadOnlySQLTransactionError:
                outcome = "refused"
            return tx, default, outcome

    assert run(probe()) == ("on", "on", "refused")
    assert rows(env.source, "SELECT count(*) AS n FROM artifacts")[0]["n"] == 1


def test_import_works_with_a_select_only_source_role(env: Env) -> None:
    seed_member(env)
    sql(env.source, "CREATE ROLE imp_ro LOGIN")
    sql(env.source, "GRANT SELECT ON ALL TABLES IN SCHEMA public TO imp_ro")
    ro_dsn = env.source.replace("postgresql:///", "postgresql://imp_ro@/", 1)
    env.source = ro_dsn
    assert env.run() == 0
    assert env.host_count("artifacts") == 1


def _s3_config(tmp_path: Path, **over: Any) -> Path:
    cfg = {"endpoint": "http://127.0.0.1:1", "bucket": "b", "region": "us-east-1", "access_key": "a", "secret_key": "s"}
    cfg.update(over)
    path = tmp_path / "s3.json"
    path.write_text(json.dumps({k: v for k, v in cfg.items() if v is not None}))
    return path


def _s3_args(env: Env, cfg: Path) -> list[str]:
    return ["import", "--manifest", str(env.manifest), "--host-dsn", env.host, "--blob-dir", str(env.host_blobs),
            "--source-dsn", env.source, "--source-s3-config", str(cfg)]


def test_s3_config_missing_key_fails_cleanly(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_member(env)
    assert main(_s3_args(env, _s3_config(tmp_path, secret_key=None))) == 1
    err = capsys.readouterr().err
    assert "secret_key" in err and "Traceback" not in err
    assert env.host_count("artifacts") == 0


def test_s3_unreachable_endpoint_fails_cleanly(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_member(env)
    assert main(_s3_args(env, _s3_config(tmp_path))) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "ImportFailed" in err
    assert env.host_count("artifacts") == 0 and not env.manifest.exists()


def test_s3_bad_config_json_fails_cleanly(env: Env, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert main(_s3_args(env, bad)) == 1
