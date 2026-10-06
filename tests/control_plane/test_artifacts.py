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
