"""Migration 0046 and ``AnnotationStore`` on an isolated PostgreSQL with every migration applied (no mocks)."""
# ruff: noqa: F811 - pytest fixtures are imported by name
import asyncio
import json
import re
import secrets
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from herdr_engineering.control_plane import artifact_shares
from herdr_engineering.control_plane.artifact_annotations import AnnotationError, AnnotationStore
from herdr_engineering.control_plane.artifact_share_api import shared_file_names
from herdr_engineering.control_plane.artifact_shares import ShareStore, new_token
from herdr_engineering.control_plane.jsonl_ingest import JsonlIngestor
from herdr_engineering.control_plane.share_bundle import BundleFile, BundleIndex
from tests.control_plane.registry_pg import ROOT, registry_pg, uid  # noqa: F401 - imported fixture

KW = dict(owner_emails=frozenset({"boss@x.org"}), per_user_per_day=2000, max_per_share=5000, max_comment_chars=4000)
ANCHOR = {"kind": "text_quote", "file": "index.html", "exact": "hello"}
NEW_KINDS = {"annotated", "annotation_edited", "annotation_withdrawn", "annotation_dismissed", "annotation_addressed"}


def run(pg, body):
    async def go():
        conn = await asyncpg.connect(pg["dsn"])
        try:
            await conn.execute("DELETE FROM artifact_annotation_ops; DELETE FROM artifact_annotations")
            return await body(AnnotationStore(conn), conn)
        finally:
            await conn.close()
    return asyncio.run(go())


_SALT = [""]


@pytest.fixture(autouse=True)
def _fresh_ids():
    """The database is shared by the module's tests, so ids must differ per test."""
    _SALT[0] = secrets.token_hex(8)


def ann_id(n: int) -> str:
    return "ann_" + _SALT[0] + f"{n:06d}"


async def seed(conn, *, names=("index.html", "a.png"), report=False, live=True, audience="lab"):
    aid, vid = uid("art"), uid("ver")
    await conn.execute("INSERT INTO artifacts (artifact_id, created_by) VALUES ($1,'owner')", aid)
    manifest = [{"name": n} for n in names]
    await conn.execute(
        "INSERT INTO artifact_versions (version_id, artifact_id, version_number, state, output_manifest, "
        "report_descriptor) VALUES ($1,$2,1,'ready',$3::jsonb,$4::jsonb)",
        vid, aid, json.dumps(manifest), json.dumps({"k": 1}) if report else None)
    shares = ShareStore(conn)
    kwargs = dict(artifact_id=aid, version_id=vid, audience=audience, created_by="owner")
    if audience == "lab":
        kwargs["allow_emails"] = ["a@x.org"]
    else:
        kwargs["expires_at"] = datetime.now(UTC) + timedelta(days=5)
    share = await shares.create(**kwargs)
    if live:
        await shares.mark_live(share["share_id"], token_hash=new_token()[1],
                               index=BundleIndex("index.html", [BundleFile("index.html", "a" * 64, 1, "text/html")], 1, []),
                               approved_by="op")
    return aid, vid, share["share_id"]


def create(share_id, n, **over):
    rec = {"op": "create", "share_id": share_id, "annotation_id": ann_id(n), "anchor": dict(ANCHOR), "body": "note",
           "author_identity": "u1", "author_email": "A@x.org", "at": datetime.now(UTC).isoformat()}
    rec.update(over)
    return rec


async def denied_reasons(conn, share_id):
    rows = await conn.fetch("SELECT detail::text d FROM artifact_share_events WHERE share_id=$1 AND kind='denied'",
                            share_id)
    return [json.loads(r["d"])["reason"] for r in rows]


async def kinds(conn, share_id):
    return [r["kind"] for r in await conn.fetch(
        "SELECT kind FROM artifact_share_events WHERE share_id=$1 ORDER BY event_id", share_id)]


def test_migration_runs_twice_and_kinds_agree(registry_pg):
    async def body(store, conn):
        sql = (ROOT / "deploy/control-plane/migrations/0046_artifact_annotations.sql").read_text()
        await conn.execute(sql)
        await conn.execute(sql)
        definition = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                         "WHERE conname='artifact_share_events_kind_check'")
        assert set(re.findall(r"'([a-z_]+)'::text", definition)) == set(artifact_shares.EVENT_KINDS)
        assert len(artifact_shares.EVENT_KINDS) == 11 and NEW_KINDS <= set(artifact_shares.EVENT_KINDS)
        assert artifact_shares.INGESTIBLE_KINDS == ("opened", "denied")
    run(registry_pg, body)


def test_shared_file_names():
    assert shared_file_names({"report_descriptor": {"a": 1}, "output_manifest": [{"name": "x.html"}]}) == {"preview.html"}
    assert shared_file_names({"output_manifest": [{"name": "d/a.html"}, {"name": "../bad"}, {"x": 1}, "s"]}) == {"d/a.html"}
    assert shared_file_names({}) == frozenset()


def test_create_uses_share_row_and_is_idempotent(registry_pg):
    async def body(store, conn):
        aid, vid, sid = await seed(conn)
        rec = create(sid, 1, artifact_id="other", version_id="other", body="  hi  ")
        assert await store.ingest(conn, rec, "k1", **KW) == "inserted"
        assert await store.ingest(conn, rec, "k1", **KW) == "duplicate"
        row = await conn.fetchrow("SELECT * FROM artifact_annotations")
        assert (row["artifact_id"], row["version_id"], row["file_name"]) == (aid, vid, "index.html")
        assert row["author_email"] == "a@x.org" and row["author_role"] == "member" and row["body"] == "hi"
        assert json.loads(row["anchor"]) == ANCHOR | {"prefix": "", "suffix": ""}
        assert await kinds(conn, sid) == ["created", "approved", "annotated"]
        assert await store.ingest(conn, create(sid, 2, author_email="boss@x.org"), "k2", **KW) == "inserted"
        assert await conn.fetchval("SELECT author_role FROM artifact_annotations WHERE ingest_key='k2'") == "owner"
    run(registry_pg, body)


def test_create_refusals_leave_denied_events(registry_pg):
    async def body(store, conn):
        aid, vid, sid = await seed(conn)
        _, _, public = await seed(conn, audience="public")
        _, _, pending = await seed(conn, live=False)
        cases = [
            ("unknown_share", create("share_nope", 1)),
            ("not_lab", create(public, 2)),
            ("not_live", create(pending, 3)),
            ("file_not_in_version", create(sid, 4, anchor={**ANCHOR, "file": "zzz.html"})),
            ("invalid", create(sid, 5, body="")),
            ("invalid", create(sid, 6, anchor={"kind": "nope"})),
            ("invalid", create(sid, 7, annotation_id="ann_short")),
            ("invalid", create(sid, 8, at="not a date")),
            ("invalid", create(sid, 9, author_identity=5)),
            ("invalid", {**create(sid, 10), "share_id": ["x"]}),
        ]
        for i, (code, rec) in enumerate(cases):
            assert await store.ingest(conn, rec, f"r{i}", **KW) == code, (i, code)
        assert await conn.fetchval("SELECT count(*) FROM artifact_annotations") == 0
        assert await denied_reasons(conn, sid) == ["annotation_file_not_in_version", "annotation_invalid",
                                                   "annotation_invalid", "annotation_invalid", "annotation_invalid",
                                                   "annotation_invalid"]
        assert await denied_reasons(conn, public) == ["annotation_not_lab"]
        assert await denied_reasons(conn, pending) == ["annotation_not_live"]
        # replaying a refused line adds no second event
        await store.ingest(conn, cases[3][1], "r3", **KW)
        assert (await denied_reasons(conn, sid)).count("annotation_file_not_in_version") == 1
    run(registry_pg, body)


def test_report_share_exposes_only_preview(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn, report=True, names=("index.html",))
        ok = create(sid, 1, anchor={**ANCHOR, "file": "preview.html"})
        assert await store.ingest(conn, ok, "p1", **KW) == "inserted"
        assert await store.ingest(conn, create(sid, 2), "p2", **KW) == "file_not_in_version"
    run(registry_pg, body)


def test_quotas(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        kw = {**KW, "per_user_per_day": 2}
        assert await store.ingest(conn, create(sid, 1), "q1", **kw) == "inserted"
        assert await store.ingest(conn, create(sid, 2), "q2", **kw) == "inserted"
        assert await store.ingest(conn, create(sid, 3), "q3", **kw) == "quota_user_day"
        assert await store.ingest(conn, create(sid, 4, author_identity="u2"), "q4", **kw) == "inserted"
        _, _, sid2 = await seed(conn)
        kw = {**KW, "max_per_share": 3}
        for n in range(3):
            assert await store.ingest(conn, create(sid2, 10 + n, author_identity=f"w{n}"), f"s{n}", **kw) == "inserted"
        assert await store.ingest(conn, create(sid2, 20, author_identity="w9"), "s9", **kw) == "quota_share"
        assert "annotation_quota_share" in await denied_reasons(conn, sid2)
    run(registry_pg, body)


def test_liveness_window_and_clamp(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        await ShareStore(conn).mark_revoked(sid, actor="o")
        revoked = await conn.fetchval("SELECT revoked_at FROM artifact_shares WHERE share_id=$1", sid)
        early = (revoked - timedelta(milliseconds=1)).isoformat()
        late = (revoked + timedelta(seconds=1)).isoformat()
        assert await store.ingest(conn, create(sid, 1, at=early), "l1", **KW) == "inserted"
        assert await store.ingest(conn, create(sid, 2, at=late), "l2", **KW) == "not_live"
        _, _, live = await seed(conn)
        far = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        assert await store.ingest(conn, create(live, 3, at=far), "l3", **KW) == "inserted"
        created = await conn.fetchval("SELECT created_at FROM artifact_annotations WHERE ingest_key='l3'")
        assert created <= datetime.now(UTC) + timedelta(minutes=5, seconds=5)
    run(registry_pg, body)


def test_forged_annotation_events_in_events_jsonl_are_ignored(registry_pg, tmp_path):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        path = tmp_path / "events.jsonl"
        line = {"share_id": sid, "kind": "annotated", "actor": "evil", "at": datetime.now(UTC).isoformat()}
        path.write_text(json.dumps(line) + "\n")
        assert await ShareStore(conn).ingest_events_file(path, marker_path=tmp_path / "m") == 0
        assert "annotated" not in await kinds(conn, sid)
    run(registry_pg, body)


def test_edit_and_delete(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        assert await store.ingest(conn, create(sid, 1), "c1", **KW) == "inserted"
        edit = {"op": "edit", "share_id": sid, "annotation_id": ann_id(1), "author_identity": "u1", "body": "new",
                "at": datetime.now(UTC).isoformat()}
        assert await store.ingest(conn, edit, "e1", **KW) == "edited"
        assert await store.ingest(conn, edit, "e1", **KW) == "duplicate"
        row = await conn.fetchrow("SELECT body, edited_at FROM artifact_annotations")
        assert row["body"] == "new" and row["edited_at"] is not None
        assert (await kinds(conn, sid)).count("annotation_edited") == 1
        assert await store.ingest(conn, {**edit, "author_identity": "u2"}, "e2", **KW) == "not_author"
        assert await store.ingest(conn, {**edit, "annotation_id": ann_id(99)}, "e3", **KW) == "not_found"
        assert await store.ingest(conn, {**edit, "body": ""}, "e4", **KW) == "invalid"
        assert {"annotation_not_author", "annotation_not_found", "annotation_invalid"} <= set(
            await denied_reasons(conn, sid))
        # own_rows / agents / list see it, then a delete withdraws it
        assert len((await store.own_rows(sid))["u1"]) == 1
        aid = await conn.fetchval("SELECT artifact_id FROM artifact_annotations")
        delete = {"op": "delete", "share_id": sid, "annotation_id": ann_id(1), "author_identity": "u1",
                  "at": datetime.now(UTC).isoformat()}
        assert await store.ingest(conn, delete, "d1", **KW) == "withdrawn"
        assert await store.ingest(conn, delete, "d1", **KW) == "duplicate"
        assert await store.ingest(conn, delete, "d2", **KW) == "not_open"
        assert await store.own_rows(sid) == {}
        assert await store.open_for_agents(aid) == []
        assert (await store.list_for_artifact(aid))[0] == []
        withdrawn, _ = await store.list_for_artifact(aid, status="withdrawn")
        assert [r["annotation_id"] for r in withdrawn] == [ann_id(1)] and withdrawn[0]["withdrawn_at"] is not None
        assert "annotation_withdrawn" in await kinds(conn, sid)
    run(registry_pg, body)


def test_edit_refused_when_not_open_or_not_live(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        await store.ingest(conn, create(sid, 1), "c1", **KW)
        aid = await conn.fetchval("SELECT artifact_id FROM artifact_annotations")
        await store.dismiss(aid, ann_id(1), actor="owner")
        edit = {"op": "edit", "share_id": sid, "annotation_id": ann_id(1), "author_identity": "u1", "body": "x",
                "at": datetime.now(UTC).isoformat()}
        assert await store.ingest(conn, edit, "e1", **KW) == "not_open"
        _, _, sid2 = await seed(conn)
        await store.ingest(conn, create(sid2, 2), "c2", **KW)
        await ShareStore(conn).mark_revoked(sid2, actor="o")
        late = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
        assert await store.ingest(conn, {**edit, "share_id": sid2, "annotation_id": ann_id(2), "at": late},
                                  "e2", **KW) == "not_live"
        # an op addressed to another share's annotation is not found
        assert await store.ingest(conn, {**edit, "annotation_id": ann_id(2)}, "e3", **KW) == "not_found"
    run(registry_pg, body)


async def add_version(conn, aid, number):
    vid = uid("ver")
    await conn.execute("INSERT INTO artifact_versions (version_id, artifact_id, version_number, state) "
                       "VALUES ($1,$2,$3,'ready')", vid, aid, number)
    return vid


def test_dismiss_and_resolve(registry_pg):
    async def body(store, conn):
        aid, vid, sid = await seed(conn)
        for n in (1, 2, 3, 4):
            await store.ingest(conn, create(sid, n), f"c{n}", **KW)
        v2 = await add_version(conn, aid, 2)
        other_aid, other_vid, _ = await seed(conn)
        with pytest.raises(AnnotationError) as bad:
            await store.resolve(aid, [ann_id(1)], version_id=other_vid, actor="agent")
        assert bad.value.code == "bad_version"
        before = await conn.fetchval("SELECT updated_at FROM artifact_annotations WHERE annotation_id=$1", ann_id(1))
        out = await store.resolve(aid, [ann_id(1), ann_id(99), ann_id(1)], version_id=v2, actor="agent")
        assert out == {"resolved": [ann_id(1)], "skipped": [{"annotation_id": ann_id(99), "reason": "not_found"}]}
        assert await conn.fetchval("SELECT updated_at FROM artifact_annotations WHERE annotation_id=$1",
                                   ann_id(1)) > before
        row = (await store.list_for_artifact(aid, status="addressed"))[0][0]
        assert row["addressed_in_version_number"] == 2 and row["version_number"] == 1 and row["status"] == "addressed"
        assert (await kinds(conn, sid)).count("annotation_addressed") == 1
        # same-or-older version is refused, withdrawn and addressed rows are not open
        out = await store.resolve(aid, [ann_id(2)], version_id=vid, actor="agent")
        assert out["skipped"] == [{"annotation_id": ann_id(2), "reason": "version_not_newer"}]
        out = await store.resolve(aid, [ann_id(1)], version_id=v2, actor="agent")
        assert out["skipped"][0]["reason"] == "not_open"
        await store.ingest(conn, {"op": "delete", "share_id": sid, "annotation_id": ann_id(3), "author_identity": "u1",
                                  "at": datetime.now(UTC).isoformat()}, "d3", **KW)
        out = await store.resolve(aid, [ann_id(3)], version_id=v2, actor="agent")
        assert out["skipped"][0]["reason"] == "not_open"
        # dismiss
        with pytest.raises(AnnotationError) as err:
            await store.dismiss(aid, ann_id(1), actor="o")
        assert err.value.code == "not_open"
        with pytest.raises(AnnotationError) as err:
            await store.dismiss(aid, ann_id(99), actor="o")
        assert err.value.code == "not_found"
        with pytest.raises(AnnotationError) as err:
            await store.dismiss(other_aid, ann_id(2), actor="o")
        assert err.value.code == "not_found"
        first = await store.dismiss(aid, ann_id(2), actor="o")
        assert first["status"] == "dismissed" and first["resolved_by"] == "o"
        assert await store.dismiss(aid, ann_id(2), actor="someone-else") == first
        assert (await kinds(conn, sid)).count("annotation_dismissed") == 1
    run(registry_pg, body)


def test_paging_agents_view_and_touched(registry_pg):
    async def body(store, conn):
        aid, vid, sid = await seed(conn)
        t0 = datetime.now(UTC) - timedelta(seconds=3)
        for n in (1, 2, 3):
            at = (datetime.now(UTC) + timedelta(seconds=n)).isoformat()
            await store.ingest(conn, create(sid, n, at=at), f"c{n}", **KW)
        page1, cursor = await store.list_for_artifact(aid, limit=2)
        assert [r["annotation_id"] for r in page1] == [ann_id(3), ann_id(2)] and cursor
        page2, cursor2 = await store.list_for_artifact(aid, limit=2, before=cursor)
        assert [r["annotation_id"] for r in page2] == [ann_id(1)] and cursor2 is None
        assert len((await store.list_for_artifact(aid, status="all"))[0]) == 3
        for bad in ({"limit": 0}, {"limit": 501}, {"status": "x"}, {"before": "junk"}):
            with pytest.raises(AnnotationError):
                await store.list_for_artifact(aid, **bad)
        agents = await store.open_for_agents(aid)
        assert len(agents) == 3
        assert set(agents[0]) == {"annotation_id", "version_id", "version_number", "file", "anchor", "body",
                                  "author_role", "created_at", "edited_at"}
        own = (await store.own_rows(sid))["u1"]
        assert set(own[0]) == {"annotation_id", "file", "anchor", "body", "status", "version_number",
                               "addressed_in_version_number", "created_at", "edited_at"}
        shares, artifacts = await store.touched_since(t0)
        assert sid in shares and aid in artifacts
        assert await store.touched_since(datetime.now(UTC) + timedelta(hours=1)) == (set(), set())
    run(registry_pg, body)


def test_hostile_body_round_trips(registry_pg):
    async def body(store, conn):
        aid, _, sid = await seed(conn)
        hostile = "'); DROP TABLE artifact_annotations;-- <script>alert(1)</script> ‮ {{7*7}} \U0001f600 \\u0000 \t\n x"
        assert await store.ingest(conn, create(sid, 1, body=hostile), "h1", **KW) == "inserted"
        assert (await store.open_for_agents(aid))[0]["body"] == hostile
    run(registry_pg, body)


def test_parse_line_and_jsonl_ingestor_wiring(registry_pg, tmp_path):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        path = tmp_path / "annotations.jsonl"
        lines = [json.dumps(create(sid, 1)), "not json", "[1]", "{" * 100000, json.dumps(create(sid, 2, body=5)),
                 json.dumps(create(sid, 3))]
        path.write_text("\n".join(lines) + "\n")

        async def put(c, record, key):
            return await store.ingest(c, record, key, **KW) == "inserted"
        ingestor = JsonlIngestor(conn, parse_line=AnnotationStore.parse_line, store=put)
        assert await ingestor.ingest_file(path, tmp_path / "m") == 2
        assert await conn.fetchval("SELECT count(*) FROM artifact_annotations") == 2
    run(registry_pg, body)


def test_taken_annotation_id_with_new_key_is_invalid_and_denied(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        assert await store.ingest(conn, create(sid, 1), "t1", **KW) == "inserted"
        assert await store.ingest(conn, create(sid, 1, body="other"), "t2", **KW) == "invalid"
        assert await denied_reasons(conn, sid) == ["annotation_invalid"]
        assert await conn.fetchval("SELECT body FROM artifact_annotations WHERE annotation_id=$1", ann_id(1)) == "note"
    run(registry_pg, body)


def test_look_alike_email_never_becomes_owner(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        kelvin = create(sid, 1, author_email="Kim@x.org")
        assert await store.ingest(conn, kelvin, "k1", **{**KW, "owner_emails": frozenset({"kim@x.org"})}) == "invalid"
        assert await conn.fetchval("SELECT count(*) FROM artifact_annotations") == 0
    run(registry_pg, body)


def test_refused_then_accepted_replay_keeps_both_events(registry_pg):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        rec = create(sid, 1)
        assert await store.ingest(conn, rec, "same", **{**KW, "per_user_per_day": 0}) == "quota_user_day"
        assert await store.ingest(conn, rec, "same", **KW) == "inserted"
        assert await denied_reasons(conn, sid) == ["annotation_quota_user_day"]
        assert "annotated" in await kinds(conn, sid)
    run(registry_pg, body)


def test_resolve_rejects_bad_ids_without_database_errors(registry_pg):
    async def body(store, conn):
        aid, vid, sid = await seed(conn)
        v2 = await add_version(conn, aid, 2)
        await store.ingest(conn, create(sid, 1), "c1", **KW)
        out = await store.resolve(aid, [ann_id(1), 5, None, "x" * 5000], version_id=v2, actor="a")
        assert out["resolved"] == [ann_id(1)]
        assert [s["reason"] for s in out["skipped"]] == ["invalid"] * 3
        out = await store.resolve(aid, [ann_id(1)] * 51, version_id=v2, actor="a")
        assert out == {"resolved": [], "skipped": [{"annotation_id": None, "reason": "invalid"}]}
    run(registry_pg, body)


def test_concurrent_change_waits_for_the_row_lock(registry_pg):
    """A withdraw holding the row (uncommitted) must make a racing edit or second withdraw wait, then see 'not_open'."""
    async def go():
        a = await asyncpg.connect(registry_pg["dsn"])
        b = await asyncpg.connect(registry_pg["dsn"])
        try:
            await a.execute("DELETE FROM artifact_annotation_ops; DELETE FROM artifact_annotations")
            _, _, sid = await seed(a)
            store = AnnotationStore(a)
            await store.ingest(a, create(sid, 1), "c1", **KW)
            await store.ingest(a, create(sid, 2), "c2", **KW)
            for n, op in ((1, "edit"), (2, "delete")):
                line = {"op": op, "share_id": sid, "annotation_id": ann_id(n), "author_identity": "u1", "body": "x",
                        "at": datetime.now(UTC).isoformat()}
                tx = a.transaction()
                await tx.start()
                assert await store.ingest(a, {**line, "op": "delete"}, f"w{n}", **KW) == "withdrawn"
                racer = asyncio.create_task(AnnotationStore(b).ingest(b, line, f"r{n}", **KW))
                await asyncio.sleep(0.5)
                assert not racer.done()                      # blocked on the row, not yet decided
                await tx.commit()
                assert await asyncio.wait_for(racer, 10) == "not_open"
            assert [r["status"] for r in await a.fetch("SELECT status FROM artifact_annotations")] == ["withdrawn"] * 2
        finally:
            await a.close()
            await b.close()
    asyncio.run(go())


def test_transient_database_error_aborts_the_pass_and_the_line_is_retried(registry_pg, tmp_path):
    """A lock timeout (a real Postgres provocation) is not a poison line: the marker must not move, the next pass
    ingests the edit. Data and integrity errors are still swallowed (see the sibling tests)."""
    async def body(store, conn):
        _, _, sid = await seed(conn)
        assert await store.ingest(conn, create(sid, 1), "c1", **KW) == "inserted"
        path, marker = tmp_path / "annotations.jsonl", tmp_path / "m"
        edit = {"op": "edit", "share_id": sid, "annotation_id": ann_id(1), "author_identity": "u1", "body": "later",
                "at": datetime.now(UTC).isoformat()}
        path.write_text(json.dumps(edit) + "\n")

        async def put(c, record, key):
            return await store.ingest(c, record, key, **KW) == "edited"
        ingestor = JsonlIngestor(conn, parse_line=AnnotationStore.parse_line, store=put)
        holder = await asyncpg.connect(registry_pg["dsn"])
        tx = holder.transaction()
        await tx.start()
        await holder.fetchrow("SELECT 1 FROM artifact_annotations WHERE annotation_id=$1 FOR UPDATE", ann_id(1))
        await conn.execute("SET lock_timeout = '150ms'")
        try:
            with pytest.raises(asyncpg.PostgresError):
                await ingestor.ingest_file(path, marker)
        finally:
            await tx.rollback()
            await holder.close()
        assert not marker.exists()                      # nothing was consumed
        assert await conn.fetchval("SELECT body FROM artifact_annotations") == "note"
        await conn.execute("SET lock_timeout = 0")
        assert await ingestor.ingest_file(path, marker) == 1
        assert await conn.fetchval("SELECT body FROM artifact_annotations") == "later"
    run(registry_pg, body)
