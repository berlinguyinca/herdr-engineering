import hashlib

from herdr_engineering.control_plane import rustfs


class _FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, *, Bucket, Key, Body):
        self.calls.append(("put", Key))
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": self.objects[Key]}

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Key))
        return Key in self.objects

    def delete_object(self, *, Bucket, Key):
        self.calls.append(("del", Key))
        self.objects.pop(Key, None)


def _client():
    return rustfs.RustFSClient(endpoint="http://x", bucket="herdr-artifacts",
                               s3=_FakeS3())


def test_content_key_layout():
    data = b"hello"
    hex_ = hashlib.sha256(data).hexdigest()
    assert rustfs.content_key(data) == f"sha256/{hex_[:2]}/{hex_[2:4]}/{hex_}"


def test_put_get_roundtrip_and_integrity():
    c = _client()
    data = b"payload-bytes"
    key, sha = c.put(data)
    assert c.get(key, sha) == data
    assert c.head(key) is True


def test_get_rejects_corruption():
    c = _client()
    data = b"payload-bytes"
    key, _ = c.put(data)
    c._s3.objects[key] = b"tampered"  # corrupt after store
    try:
        c.get(key, hashlib.sha256(data).hexdigest())
        raise AssertionError("expected IntegrityError")
    except rustfs.IntegrityError:
        pass
