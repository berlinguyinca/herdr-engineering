"""Annotation settings, viewer projections and the worker's annotation housekeeper (real PostgreSQL, real files)."""
# ruff: noqa: F811 - pytest fixtures are imported by name
import hashlib
import json
import stat
from pathlib import Path

import pytest

from herdr_engineering.control_plane.annotation_anchor import (
    ANNOTATION_DEFAULTS,
    annotation_settings,
)
from herdr_engineering.control_plane.share_bundle import write_own_projection
from herdr_engineering.control_plane.worker import AnnotationHousekeeper

from .test_artifact_annotations_store import _fresh_ids, ann_id, create, registry_pg, run, seed  # noqa: F401

OWNERS = frozenset({"boss@x.org"})


def sha(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def append(path: Path, *records: dict) -> None:
    with path.open("a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def projection(root: Path, share_id: str, identity: str) -> dict | None:
    path = root / share_id / "annotations" / (sha(identity) + ".json")
    return json.loads(path.read_text()) if path.exists() else None


class Recorder:
    """A real object with the mirror's ``publish_annotations`` shape; Task 15 adds the production one."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    async def publish_annotations(self, artifact_id: str, rows: list[dict]) -> None:
        self.calls.append((artifact_id, rows))


def housekeeper(tmp_path, **over):
    for name in ("shares", "events", "markers"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    kwargs = dict(owner_emails=OWNERS, interval=0.0)
    kwargs.update(over)
    return AnnotationHousekeeper(tmp_path / "shares", tmp_path / "events", tmp_path / "markers",
                                 annotation_settings(None), **kwargs)


# ------------------------------------------------------------------------------------------------ settings

def test_settings_defaults_equal_the_constant():
    assert annotation_settings(None) == ANNOTATION_DEFAULTS == annotation_settings({})
    assert annotation_settings({"max_per_share": 5})["max_per_share"] == 5


@pytest.mark.parametrize("bad", [{"enabled": "yes"}, {"enabled": 1}, {"per_user_per_minute": 0},
                                 {"per_user_per_minute": True}, {"global_per_minute": "3"},
                                 {"max_comment_chars": 4001}, {"max_comment_chars": 0}, {"nope": 1}, []])
def test_settings_refuse(bad):
    with pytest.raises(ValueError):
        annotation_settings(bad)


# ------------------------------------------------------------------------------------------------ projection file

def test_projection_is_atomic_0644_and_skipped_without_the_share_dir(tmp_path):
    (tmp_path / "share_a").mkdir()
    write_own_projection(tmp_path, "share_a", "u1", [{"annotation_id": "x"}])
    path = tmp_path / "share_a" / "annotations" / (sha("u1") + ".json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert json.loads(path.read_text())["annotations"] == [{"annotation_id": "x"}]
    assert [p.name for p in path.parent.iterdir()] == [path.name]          # no temp file left
    write_own_projection(tmp_path, "share_gone", "u1", [{"annotation_id": "x"}])
    assert not (tmp_path / "share_gone").exists()


def test_projection_never_follows_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "share_l").symlink_to(outside)
    write_own_projection(tmp_path, "share_l", "u1", [])
    assert list(outside.iterdir()) == []
    (tmp_path / "share_b").mkdir()
    (tmp_path / "share_b" / "annotations").symlink_to(outside)
    write_own_projection(tmp_path, "share_b", "u1", [])
    assert list(outside.iterdir()) == []


def test_projection_is_capped_newest_first_with_a_truncated_flag(tmp_path):
    (tmp_path / "share_c").mkdir()
    rows = [{"annotation_id": f"r{i}", "body": "x" * 100_000} for i in range(30)]       # ~3 MB, newest first
    write_own_projection(tmp_path, "share_c", "u1", rows)
    path = tmp_path / "share_c" / "annotations" / (sha("u1") + ".json")
    assert path.stat().st_size <= 2 * 1024 * 1024
    doc = json.loads(path.read_text())
    assert doc["truncated"] is True and 0 < len(doc["annotations"]) < 30
    assert [r["annotation_id"] for r in doc["annotations"]] == [f"r{i}" for i in range(len(doc["annotations"]))]
    write_own_projection(tmp_path, "share_c", "u1", rows[:2])
    assert json.loads(path.read_text()).get("truncated", False) is False


# ------------------------------------------------------------------------------------------------ housekeeper

def test_create_edit_delete_dismiss_reach_only_the_authors_projection(registry_pg, tmp_path):
    async def body(store, conn):
        aid, _, sid = await seed(conn)
        (tmp_path / "shares").mkdir()
        (tmp_path / "shares" / sid).mkdir()
        hk = housekeeper(tmp_path)
        events = tmp_path / "events" / "annotations.jsonl"
        append(events, create(sid, 1, author_identity="u1"), create(sid, 2, author_identity="u2", body="mine"))
        out = await hk.tick(conn)
        assert out["ingested"] == 2 and out["projected"] >= 1
        mine = projection(tmp_path / "shares", sid, "u1")
        assert [r["body"] for r in mine["annotations"]] == ["note"]
        assert [r["body"] for r in projection(tmp_path / "shares", sid, "u2")["annotations"]] == ["mine"]
        assert projection(tmp_path / "shares", sid, "stranger") is None
        append(events, {"op": "edit", "share_id": sid, "annotation_id": ann_id(1), "author_identity": "u1",
                        "body": "edited", "at": create(sid, 1)["at"]})
        await hk.tick(conn)
        assert projection(tmp_path / "shares", sid, "u1")["annotations"][0]["body"] == "edited"
        await store.dismiss(aid, ann_id(2), actor="owner")
        await hk.tick(conn)
        assert projection(tmp_path / "shares", sid, "u2")["annotations"][0]["status"] == "dismissed"
        append(events, {"op": "delete", "share_id": sid, "annotation_id": ann_id(1), "author_identity": "u1",
                        "at": create(sid, 1)["at"]})
        await hk.tick(conn)
        assert projection(tmp_path / "shares", sid, "u1")["annotations"] == []     # withdrawn rows leave the file
    run(registry_pg, body)


def test_a_revoked_shares_directory_is_not_recreated(registry_pg, tmp_path):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        hk = housekeeper(tmp_path)                                          # no <shares>/<sid> directory
        append(tmp_path / "events" / "annotations.jsonl", create(sid, 3))
        await hk.tick(conn)
        assert not (tmp_path / "shares" / sid).exists()
    run(registry_pg, body)


def test_only_touched_shares_are_rewritten_after_the_first_tick(registry_pg, tmp_path):
    async def body(store, conn):
        _, _, sid = await seed(conn)
        (tmp_path / "shares").mkdir()
        (tmp_path / "shares" / sid).mkdir()
        hk = housekeeper(tmp_path)
        append(tmp_path / "events" / "annotations.jsonl", create(sid, 4))
        await hk.tick(conn)
        path = tmp_path / "shares" / sid / "annotations" / (sha("u1") + ".json")
        path.unlink()
        out = await hk.tick(conn)                       # inside the overlap margin: still touched, rewritten
        assert path.exists() and out["projected"] >= 1
        await conn.execute("UPDATE artifact_annotations SET updated_at = now() - interval '1 hour'")
        path.unlink()
        out = await hk.tick(conn)
        assert not path.exists() and out["projected"] == 0
    run(registry_pg, body)


def test_mirror_gets_touched_artifacts_and_no_mirror_skips_the_step(registry_pg, tmp_path):
    async def body(store, conn):
        aid, _, sid = await seed(conn)
        mirror = Recorder()
        hk = housekeeper(tmp_path, mirror=mirror)
        append(tmp_path / "events" / "annotations.jsonl", create(sid, 5))
        out = await hk.tick(conn)
        calls = {a: rows for a, rows in mirror.calls}
        assert [r["body"] for r in calls[aid]] == ["note"] and "author_email" not in calls[aid][0]
        assert out["published"] >= 1
        bare = housekeeper(tmp_path / "bare")
        assert (await bare.tick(conn))["published"] == 0
    run(registry_pg, body)


def test_tick_is_rate_limited_never_raises_and_honours_enabled(registry_pg, tmp_path):
    async def body(store, conn):
        hk = housekeeper(tmp_path, interval=3600.0, clock=lambda: 1000.0)
        assert isinstance(await hk.tick(conn), dict)
        assert await hk.tick(conn) == "skipped"
        broken = housekeeper(tmp_path / "x", mirror=object())               # no publish_annotations: logged, not raised
        _, _, sid = await seed(conn)
        append(tmp_path / "x" / "events" / "annotations.jsonl", create(sid, 6))
        assert isinstance(await broken.tick(conn), dict)
        off = AnnotationHousekeeper(tmp_path / "shares", tmp_path / "events", tmp_path / "markers",
                                    annotation_settings({"enabled": False}), owner_emails=OWNERS)
        assert await off.tick(conn) == "disabled"
    run(registry_pg, body)


def test_run_one_pass_ticks_the_annotation_housekeeper(registry_pg, tmp_path):
    from herdr_engineering.control_plane import worker

    async def body(store, conn):
        summary = await worker.run_one_pass(conn, annotation_housekeeper=housekeeper(tmp_path))
        assert isinstance(summary["annotation_housekeeping"], dict)
        assert "annotation_housekeeping" not in await worker.run_one_pass(conn)
    run(registry_pg, body)
