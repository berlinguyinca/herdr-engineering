import hashlib
import io

import pytest

from herdr_engineering.control_plane.rustfs import IntegrityError, RustFSClient


class _StreamingS3:
    def __init__(self):
        self.objects = {}
        self.read_sizes = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        chunks = []
        while True:
            chunk = fileobj.read(3)
            self.read_sizes.append(len(chunk))
            if not chunk:
                break
            chunks.append(chunk)
        self.objects[key] = b"".join(chunks)

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


def _client():
    s3 = _StreamingS3()
    return RustFSClient(endpoint="http://x", bucket="b", s3=s3), s3


def test_put_stream_hashes_and_uploads_without_reading_whole_source():
    client, s3 = _client()
    data = b"streamed artifact payload"
    stored = client.put_stream(io.BytesIO(data), expected_size=len(data),
                               expected_sha256=hashlib.sha256(data).hexdigest())
    assert stored["sha256"] == hashlib.sha256(data).hexdigest()
    assert s3.objects[stored["key"]] == data
    assert max(s3.read_sizes) <= 3


def test_put_stream_rejects_digest_or_size_before_upload():
    client, s3 = _client()
    with pytest.raises(IntegrityError):
        client.put_stream(io.BytesIO(b"payload"), expected_sha256="0" * 64)
    with pytest.raises(IntegrityError):
        client.put_stream(io.BytesIO(b"payload"), expected_size=8)
    assert s3.objects == {}


def test_iter_get_verifies_chunks_and_legacy_get_still_returns_bytes():
    client, _ = _client()
    data = b"verified read"
    stored = client.put_stream(io.BytesIO(data))
    assert b"".join(client.iter_get(stored["key"], stored["sha256"], chunk_size=4)) == data
    assert client.get(stored["key"], stored["sha256"]) == data

