import asyncio

from herdr_engineering.control_plane import artifacts
from herdr_engineering.control_plane import rustfs as rustfs_mod


class _FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        return {"Body": self.objects.get(Key, b"")}

    def head_object(self, *, Bucket, Key):
        return Key in self.objects

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)


class _Conn:
    def __init__(self):
        self.art = {}
        self.bindings = []
        self.mats = []

    async def execute(self, sql, *a):
        if "INSERT INTO artifacts" in sql:
            self.art[a[0]] = {
                "artifact_id": a[0], "original_filename": a[1],
                "mime_type": a[2], "artifact_type": a[3], "created_by": a[4],
                "size_bytes": a[5], "sha256": a[6], "storage_key": a[7],
                "retention_class": a[8], "status": "available",
                "storage_backend": "rustfs",
            }
        elif "INSERT INTO artifact_bindings" in sql:
            self.bindings.append(a)
        elif "INSERT INTO artifact_materializations" in sql:
            self.mats.append(a)
        elif "UPDATE artifacts SET status" in sql:
            self.art[a[0]]["status"] = "available"
        return 1

    async def fetchrow(self, sql, *a):
        return self.art.get(a[0])

    async def fetch(self, sql, *a):
        return list(self.art.values())


def _run(coro):
    return asyncio.run(coro)


def _store():
    rf = rustfs_mod.RustFSClient(endpoint="http://x", bucket="b", s3=_FakeS3())
    return artifacts.ArtifactStore(_Conn(), rf), rf


def test_register_makes_available_with_metadata():
    store, _ = _store()
    aid = _run(store.register(original_filename="shot.png",
                              mime_type="image/png", data=b"PNG",
                              artifact_type="screenshot"))
    assert aid.startswith("artifact_")
    row = _run(store.get(aid))
    assert row["status"] == "available" and row["size_bytes"] == 3
    assert row["storage_key"].startswith("sha256/")
    assert _run(store.download(aid)) == b"PNG"


def test_download_rejects_tampered_bytes():
    store, rf = _store()
    aid = _run(store.register(original_filename="a.txt",
                              mime_type="text/plain", data=b"safe"))
    rf._s3.objects[list(rf._s3.objects.keys())[0]] = b"evil"
    try:
        _run(store.download(aid))
        raise AssertionError("expected IntegrityError")
    except artifacts.IntegrityError:
        pass


class _JobPersistenceConn:
    def __init__(self):
        self.rows = {}
        self.calls = []

    async def fetchrow(self, sql, job_id):
        assert "FROM artifact_build_jobs WHERE job_id=$1" in sql
        return self.rows.get(job_id)

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


def test_get_job_reads_current_durable_state_and_normalizes_json():
    from datetime import UTC, datetime

    conn = _JobPersistenceConn()
    store = artifacts.ArtifactStore(conn, None)
    assert _run(store.get_job("missing")) is None
    conn.rows["job_1"] = {"job_id": "job_1", "status": "queued", "request": '{"source":"hi"}'}
    assert _run(store.get_job("job_1"))["status"] == "queued"
    conn.rows["job_1"].update(status="completed", result_version_id="version_2",
                             completed_at=datetime(2026, 1, 1, tzinfo=UTC))
    job = _run(store.get_job("job_1"))
    assert job["status"] == "completed"
    assert job["result_version_id"] == "version_2"
    assert job["request"] == {"source": "hi"}
    assert job["completed_at"] == "2026-01-01T00:00:00+00:00"


def test_job_persistence_converts_epoch_and_reloaded_iso_timestamps():
    from datetime import UTC, datetime

    conn = _JobPersistenceConn()
    store = artifacts.ArtifactStore(conn, None)
    job = {"job_id": "job_1", "artifact_id": "artifact_1", "base_version_id": None,
           "profile": "web", "request": {}, "status": "completed", "requested_by": "owner",
           "started_at": 1767225600.0, "completed_at": "2026-01-01T00:00:01+00:00"}
    _run(store.record_job_event("claimed", {**job, "status": "running"}))
    assert conn.calls[-1][1][1] == datetime(2026, 1, 1, tzinfo=UTC)
    _run(store.record_job_event("completed", job))
    assert conn.calls[-1][1][1] == datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    job.update(status="queued", started_at=None, completed_at=None)
    _run(store.record_job_event("created", job))
    assert conn.calls[-1][1][-1] is None


def test_acl_persistence_converts_epoch_expiry_and_preserves_datetime():
    from datetime import UTC, datetime

    conn = _JobPersistenceConn()
    store = artifacts.ArtifactStore(conn, None)
    grant = {"artifact_id": "artifact_1", "principal_id": "owner", "permissions": ["read"],
             "expires_at": 1767225600.0}
    _run(store.record_acl_event("grant", grant))
    assert conn.calls[-1][1][3] == datetime(2026, 1, 1, tzinfo=UTC)
    grant["expires_at"] = datetime(2026, 2, 1, tzinfo=UTC)
    _run(store.record_acl_event("grant", grant))
    assert conn.calls[-1][1][3] is grant["expires_at"]



def test_binding_context_uses_observed_canonical_records_without_host_fabrication():
    from datetime import UTC, datetime

    conn = _JobPersistenceConn()
    store = artifacts.ArtifactStore(conn, None)
    mission = {"mission_id": "short", "mission_key": "autospec:short", "title": "Observed",
               "status": "blocked", "stage": "testing", "created_at": 1767225600.0,
               "updated_at": "2026-01-01T00:00:01+00:00"}
    session = {"session_id": "session_seen", "mission_id": "short",
               "mission_key": "autospec:short", "host_id": "event_only_host", "status": "idle",
               "created_at": 1767225600.0, "updated_at": "2026-01-01T00:00:01+00:00"}
    _run(store.ensure_binding_context(mission=mission, session=session))
    assert len(conn.calls) == 2
    mission_sql, mission_args = conn.calls[0]
    session_sql, session_args = conn.calls[1]
    assert "ON CONFLICT (mission_id) DO NOTHING" in mission_sql
    assert "ON CONFLICT (session_id) DO NOTHING" in session_sql
    assert mission_args[0] == "autospec:short"
    assert "blocked" in mission_args
    assert datetime(2026, 1, 1, tzinfo=UTC) in mission_args
    assert session_args[:2] == ("session_seen", "autospec:short")
    assert "event_only_host" not in session_args
    assert "idle" in session_args
    assert datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC) in session_args
    conn.calls.clear()
    _run(store.ensure_binding_context())
    assert conn.calls == []


def test_load_reviews_restores_comments_from_sql_findings_for_manager_restart():
    from herdr_engineering.control_plane.artifact_reviews import ArtifactReviewManager

    class Conn:
        async def fetch(self, sql):
            assert sql == "SELECT * FROM artifact_reviews"
            return [
                {"review_id": "review_done", "artifact_id": "artifact_1", "version_id": "v1",
                 "state": "approved", "findings": '["keep this note", "and this one"]'},
                {"review_id": "review_requested", "artifact_id": "artifact_1", "version_id": "v1",
                 "state": "requested", "findings": []},
            ]

    store = artifacts.ArtifactStore(Conn(), None)
    manager = ArtifactReviewManager()
    manager.load(_run(store.load_reviews()))
    assert manager.get("review_done")["comments"] == ["keep this note", "and this one"]
    assert manager.get("review_requested")["comments"] == []
    assert manager.list_for_artifact("artifact_1")[0]["comments"] == ["keep this note", "and this one"]
