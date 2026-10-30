"""ShareStore on an isolated, Unix-socket-only PostgreSQL (skips cleanly when none is available)."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from herdr_engineering.control_plane.artifact_shares import ShareError, ShareStore, new_token
from herdr_engineering.control_plane.share_bundle import BundleFile, BundleIndex

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "deploy/control-plane/migrations"
PREREQ = ("0001_control_plane.sql", "0003_artifact_registry.sql", "0005_artifact_binding_context.sql",
          "0006_artifact_acl.sql", "0021_artifact_reports.sql")


@pytest.fixture(scope="module")
def share_postgres():
    if os.getuid() == 0:
        pytest.skip("isolated initdb cannot run as root")
    candidates = []
    config = shutil.which("pg_config")
    if config:
        candidates.append(Path(subprocess.check_output([config, "--bindir"], text=True).strip()) / "initdb")
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    initdb = next((c for c in candidates if c.is_file()), None)
    if initdb is None:
        pytest.skip("PostgreSQL server binaries unavailable")
    bindir = initdb.parent
    with tempfile.TemporaryDirectory(prefix="share-pg-") as folder:
        area = Path(folder)
        data = area / "data"
        subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                       check=True, capture_output=True)
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l", str(area / "server.log"),
                        "-o", f"-c listen_addresses='' -k {area}", "-w", "start"],
                       check=True, capture_output=True)
        try:
            dsn = f"postgresql:///postgres?host={area}"

            async def initialize():
                conn = await asyncpg.connect(dsn)
                try:
                    for name in (*PREREQ, "0036_artifact_shares.sql"):
                        await conn.execute((MIGRATIONS / name).read_text())
                    # re-running must be a no-op (idempotent)
                    await conn.execute((MIGRATIONS / "0036_artifact_shares.sql").read_text())
                finally:
                    await conn.close()

            asyncio.run(initialize())
            yield dsn
        finally:
            subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"],
                           check=True, capture_output=True)


def run(dsn, body):
    """Run ``body(store, conn)`` against a fresh connection."""
    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            return await body(ShareStore(conn), conn)
        finally:
            await conn.close()
    return asyncio.run(go())


async def seed(conn):
    aid, vid = "artifact_" + uuid.uuid4().hex, "version_" + uuid.uuid4().hex
    await conn.execute("INSERT INTO artifacts (artifact_id,created_by) VALUES ($1,'owner')", aid)
    await conn.execute("INSERT INTO artifact_versions (version_id,artifact_id,version_number,state) "
                       "VALUES ($1,$2,1,'ready')", vid, aid)
    return aid, vid


def index(entry="index.html", size=10):
    return BundleIndex(entry, [BundleFile(entry, "a" * 64, size, "text/html")], size, [])


def kw(aid, vid, **over):
    base = dict(artifact_id=aid, version_id=vid, audience="lab", allow_emails=["a@x.org"], allow_groups=[],
                expires_at=None, entry=None, title="T", created_by="owner")
    base.update(over)
    return base


def test_create_pending_validates_audience_rules(share_postgres):
    async def body(store, conn):
        aid, vid = await seed(conn)
        now = datetime.now(UTC)
        ok = await store.create(**kw(aid, vid, allow_emails=["A@X.org ", "a@x.org"]))
        assert ok["state"] == "pending" and ok["allow_emails"] == ["a@x.org"] and "token_hash" not in ok
        assert (await store.create(**kw(aid, vid, allow_emails=[], allow_groups=["g"])))["allow_groups"] == ["g"]
        assert (await store.create(**kw(aid, vid, expires_at=now + timedelta(days=400))))["state"] == "pending"
        for bad in (dict(allow_emails=[], allow_groups=[]), dict(allow_emails=["nope"]),
                    dict(audience="private"), dict(expires_at=now - timedelta(days=1)),
                    dict(expires_at=datetime(2099, 1, 1)),  # naive
                    dict(version_id="version_missing")):
            with pytest.raises(ShareError):
                await store.create(**kw(aid, vid, **bad))
        pub = dict(audience="public", allow_emails=[], allow_groups=[])
        good = await store.create(**kw(aid, vid, **pub, expires_at=now + timedelta(days=30)))
        assert good["audience"] == "public"
        for days in (1, 90):
            await store.create(**kw(aid, vid, **pub, expires_at=now + timedelta(days=days)))
        for bad in (dict(expires_at=None), dict(expires_at=now + timedelta(hours=2)),
                    dict(expires_at=now + timedelta(days=91)), dict(expires_at=now - timedelta(days=5)),
                    dict(expires_at=now + timedelta(days=30), allow_emails=["a@x.org"]),
                    dict(expires_at=now + timedelta(days=30), allow_groups=["g"])):
            with pytest.raises(ShareError):
                await store.create(**kw(aid, vid, **{**pub, **bad}))
        # a version of another artifact is refused
        other, other_v = await seed(conn)
        with pytest.raises(ShareError):
            await store.create(**kw(aid, other_v))
        assert [e["kind"] for e in await store.events(ok["share_id"])] == ["created"]
    run(share_postgres, body)


def test_state_machine(share_postgres):
    async def body(store, conn):
        aid, vid = await seed(conn)
        s = await store.create(**kw(aid, vid))
        sid = s["share_id"]
        token, token_hash = new_token()
        live = await store.mark_live(sid, token_hash=token_hash, index=index(size=42), approved_by="op")
        assert live["state"] == "live" and live["entry"] == "index.html"
        assert live["file_count"] == 1 and live["total_bytes"] == 42 and live["approved_by"] == "op"
        assert live["approved_at"] and "token_hash" not in live
        with pytest.raises(ShareError):  # not pending any more
            await store.mark_live(sid, token_hash=token_hash, index=index(), approved_by="op")
        assert await store.live_total_bytes() >= 42
        revoked = await store.mark_revoked(sid, actor="op")
        assert revoked["state"] == "revoked" and revoked["revoked_at"]
        again = await store.mark_revoked(sid, actor="op2")  # no-op
        assert again["revoked_at"] == revoked["revoked_at"]
        with pytest.raises(ShareError):
            await store.mark_live(sid, token_hash=new_token()[1], index=index(), approved_by="op")
        assert [e["kind"] for e in await store.events(sid)] == ["created", "approved", "revoked"]
        # revoking a pending share is allowed; unknown share is an error
        p = await store.create(**kw(aid, vid))
        assert (await store.mark_revoked(p["share_id"], actor="op"))["state"] == "revoked"
        with pytest.raises(ShareError):
            await store.mark_revoked("share_nope", actor="op")
        # an already-expired share cannot be approved
        e = await store.create(**kw(aid, vid, expires_at=datetime.now(UTC) + timedelta(days=1)))
        await conn.execute("UPDATE artifact_shares SET expires_at=now()-interval '1 minute' WHERE share_id=$1",
                           e["share_id"])
        with pytest.raises(ShareError):
            await store.mark_live(e["share_id"], token_hash=new_token()[1], index=index(), approved_by="op")
    run(share_postgres, body)


def test_live_expired_and_totals(share_postgres):
    async def body(store, conn):
        aid, vid = await seed(conn)
        before = await store.live_total_bytes()
        ids = []
        for size in (5, 7):
            s = await store.create(**kw(aid, vid, expires_at=datetime.now(UTC) + timedelta(days=2)))
            await store.mark_live(s["share_id"], token_hash=new_token()[1], index=index(size=size), approved_by="op")
            ids.append(s["share_id"])
        assert await store.live_total_bytes() == before + 12
        await conn.execute("UPDATE artifact_shares SET expires_at=now()-interval '1 second' WHERE share_id=$1", ids[0])
        due = await store.live_expired(datetime.now(UTC))
        assert ids[0] in [d["share_id"] for d in due] and ids[1] not in [d["share_id"] for d in due]
        row = next(d for d in due if d["share_id"] == ids[0])
        assert re.fullmatch(r"[0-9a-f]{64}", row["token_hash"])  # worker needs it for the state path
        await store.mark_revoked(ids[0], actor="sweep")
        await store.add_event(ids[0], "expired", "sweep", {"reason": "expiry"})
        assert ids[0] not in [d["share_id"] for d in await store.live_expired(datetime.now(UTC))]
        assert await store.live_total_bytes() == before + 7
        got = await store.get(ids[1])
        assert re.fullmatch(r"[0-9a-f]{64}", got["token_hash"]) and await store.get("share_nope") is None
        with pytest.raises(ShareError):
            await store.add_event(ids[0], "bogus", "x", {})
    run(share_postgres, body)


def test_token_hash_unique_and_never_listed(share_postgres):
    token, digest = new_token()
    assert len(token) >= 43 and re.fullmatch(r"[0-9a-f]{64}", digest)
    import hashlib
    assert hashlib.sha256(token.encode()).hexdigest() == digest and new_token()[0] != token

    async def body(store, conn):
        aid, vid = await seed(conn)
        a = await store.create(**kw(aid, vid))
        b = await store.create(**kw(aid, vid))
        await store.mark_live(a["share_id"], token_hash=digest, index=index(), approved_by="op")
        with pytest.raises(ShareError):
            await store.mark_live(b["share_id"], token_hash=digest, index=index(), approved_by="op")
        assert (await store.get(b["share_id"]))["state"] == "pending"  # failed approve left it untouched
        with pytest.raises(ShareError):
            await store.mark_live(b["share_id"], token_hash="not-hex", index=index(), approved_by="op")
        listed = await store.list_for_artifact(aid)
        assert len(listed) == 2 and all("token_hash" not in r for r in listed)
        assert digest not in json.dumps(listed, default=str)
        assert await store.list_for_artifact("artifact_none") == []
    run(share_postgres, body)


def line(sid, kind="opened", actor="public", n=0, **extra):
    return json.dumps({"share_id": sid, "kind": kind, "actor": actor, "at": f"2026-10-30T10:{n // 60:02d}:{n % 60:02d}Z",
                       "detail": {"n": n}, **extra}) + "\n"


def test_ingest_events_file_is_idempotent_and_tolerates_torn_line(share_postgres, tmp_path):
    async def body(store, conn):
        aid, vid = await seed(conn)
        sid = (await store.create(**kw(aid, vid)))["share_id"]
        f = tmp_path / "events.jsonl"
        assert await store.ingest_events_file(f) == 0  # missing file
        f.write_text(line(sid, n=1) + line("share_unknown", n=2) + "not json\n" + line(sid, kind="bogus", n=3)
                     + line(sid, kind="denied", n=4) + '{"share_id": "' + sid + '", "ki')  # torn tail
        assert await store.ingest_events_file(f) == 2
        assert await store.ingest_events_file(f) == 0  # idempotent
        # the writer completes the torn line and appends more
        with f.open("a") as fh:
            fh.write('nd": "opened", "actor": "public", "at": "2026-10-30T10:00:05Z"}\n' + line(sid, n=6))
        assert await store.ingest_events_file(f) == 2
        assert await store.ingest_events_file(f) == 0
        evs = await store.events(sid)
        assert [e["kind"] for e in evs] == ["created", "opened", "denied", "opened", "opened"]
        assert evs[1]["detail"] == {"n": 1} and evs[1]["actor"] == "public"
        # crash after the DB commit but before the offset was saved: replay inserts nothing twice
        for p in tmp_path.iterdir():
            if p.name.startswith("events.jsonl.") and p != f:
                p.unlink()
        assert await store.ingest_events_file(f) == 0
        assert len(await store.events(sid)) == 5
        # rotation (file replaced by a smaller one) restarts from the top
        f.write_text(line(sid, n=9))
        assert await store.ingest_events_file(f) == 1
        # events(limit)
        assert len(await store.events(sid, limit=2)) == 2
    run(share_postgres, body)


def test_ingest_poison_forged_oversize_and_replaced_files(share_postgres, tmp_path, monkeypatch):
    from herdr_engineering.control_plane import artifact_shares as mod

    async def body(store, conn):
        aid, vid = await seed(conn)
        sid = (await store.create(**kw(aid, vid)))["share_id"]
        f = tmp_path / "events.jsonl"
        marker = tmp_path / "private" / "m.json"
        marker.parent.mkdir()
        nul = line(sid, n=1).replace('"public"', '"pub\\u0000lic"')
        sur = line(sid, n=2).replace('"public"', '"\\ud800"')
        sur_detail = line(sid, n=3).replace('{"n": 3}', '{"k\\ud800": 1}')
        forged = line(sid, kind="approved", n=4) + line(sid, kind="revoked", n=5) + line(sid, kind="expired", n=6)
        big = json.dumps({"share_id": sid, "kind": "opened", "actor": "x" * 500, "at": "2026-10-30T10:00:07Z",
                          "detail": {"pad": "y" * 5000}}) + "\n"
        f.write_text(nul + sur + sur_detail + forged + big + line(sid, n=8))
        assert await store.ingest_events_file(f, marker_path=marker) == 2
        assert not (tmp_path / "events.jsonl.offset").exists() and marker.exists()
        evs = await store.events(sid)
        assert [e["kind"] for e in evs] == ["created", "opened", "opened"]
        assert len(evs[1]["actor"]) == 200 and evs[1]["detail"] == {}
        assert await store.ingest_events_file(f, marker_path=marker) == 0

        # a statement-level failure (NUL slipped past parsing) skips only that line and is consumed
        real = mod.ShareStore._parse_line.__func__

        def sloppy(cls, raw):
            parsed = real(cls, raw)
            if parsed and "BAD" in raw.decode():
                return parsed[0], parsed[1], "a\x00b", parsed[3], parsed[4]
            return parsed
        monkeypatch.setattr(mod.ShareStore, "_parse_line", classmethod(sloppy))
        with f.open("a") as fh:
            fh.write(line(sid, n=10, note="BAD") + line(sid, n=11))
        assert await store.ingest_events_file(f, marker_path=marker) == 1
        assert await store.ingest_events_file(f, marker_path=marker) == 0
        monkeypatch.undo()

        # delete + recreate (possibly the same inode), larger than the old offset, different content
        old = f.stat().st_size
        f.unlink()
        f.write_text("".join(line(sid, n=20 + i) for i in range(60)))
        assert f.stat().st_size > old
        assert await store.ingest_events_file(f, marker_path=marker) == 60
        assert await store.ingest_events_file(f, marker_path=marker) == 0
        # in-place rewrite (same inode) with different content of at least the old size
        f.write_text("".join(line(sid, n=40 + i) for i in range(70)))
        assert await store.ingest_events_file(f, marker_path=marker) == 70
        # replacing with the same bytes re-reads from 0 only if the fingerprint changed: never double-inserts
        n_before = len(await store.events(sid, limit=1000))
        new = tmp_path / "other"
        new.write_text(f.read_text())
        os.replace(new, f)
        assert await store.ingest_events_file(f, marker_path=marker) == 0
        assert len(await store.events(sid, limit=1000)) == n_before

        # one huge line without a newline is skipped, later lines still ingest
        monkeypatch.setattr(mod, "_MAX_INGEST_BYTES", 256)
        g = tmp_path / "huge.jsonl"
        gm = tmp_path / "private" / "g.json"
        g.write_text("z" * 700)
        total = await store.ingest_events_file(g, marker_path=gm)
        with g.open("a") as fh:
            fh.write("zzz\n" + line(sid, n=50))
        for _ in range(10):  # 256-byte windows walk through the huge line, then the good line is read
            total += await store.ingest_events_file(g, marker_path=gm)
        assert total == 1
        assert [e["detail"] for e in await store.events(sid, limit=1000)].count({"n": 50}) == 3  # two rewrites of f also hold n=50; g adds one
    run(share_postgres, body)


def test_migration_up_and_down_names():
    files = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert [n for n in files if n.startswith("0036")] == ["0036_artifact_shares.sql"]
    text = (MIGRATIONS / "0036_artifact_shares.sql").read_text()
    assert "-- DOWN:" in text
    down = text.split("-- DOWN:")[1].split("\n\n")[0]
    for table in ("artifact_share_events", "artifact_shares"):
        assert table in down
        assert f"CREATE TABLE IF NOT EXISTS {table}" in text
    assert "token_hash" in text and "'pending', 'live', 'revoked'" in text
    assert not re.findall(r"CREATE (?:UNIQUE )?INDEX (?!IF NOT EXISTS)", text)
    assert not re.findall(r"CREATE TABLE (?!IF NOT EXISTS)", text)
