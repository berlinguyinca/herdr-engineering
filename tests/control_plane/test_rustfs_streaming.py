import hashlib
import io
import os
import tempfile

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



def test_put_stream_bounds_source_reads_and_preserves_media_type():
    class BoundedSource(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 4
            return super().read(size)

    class S3(_StreamingS3):
        def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
            assert ExtraArgs == {"ContentType": "text/plain"}
            super().upload_fileobj(fileobj, bucket, key, ExtraArgs)

    s3 = S3()
    client = RustFSClient(endpoint="http://x", bucket="b", s3=s3)
    result = client.put_stream(BoundedSource(b"bounded stream"), chunk_size=4, media_type="text/plain")
    assert s3.objects[result["key"]] == b"bounded stream"
    assert result["size"] == len(b"bounded stream")


def test_put_stream_rejects_oversized_source_early():
    class Source:
        reads = 0

        def read(self, size):
            self.reads += 1
            assert self.reads <= 2, "must stop reading as soon as the declared size is exceeded"
            return b"xxxx"

    client, s3 = _client()
    with pytest.raises(IntegrityError, match="size"):
        client.put_stream(Source(), expected_size=4, chunk_size=4)
    assert s3.objects == {}


def test_put_stream_file_body_fallback_and_legacy_put():
    class S3:
        def __init__(self):
            self.objects = {}

        def put_object(self, *, Bucket, Key, Body, **kwargs):
            self.objects[Key] = Body.read() if hasattr(Body, "read") else Body
            self.kwargs = kwargs

        def get_object(self, *, Bucket, Key):
            return {"Body": self.objects[Key]}

    s3 = S3()
    client = RustFSClient(endpoint="http://x", bucket="b", s3=s3)
    streamed = client.put_stream(io.BytesIO(b"stream"), media_type="text/plain")
    assert s3.kwargs == {"ContentType": "text/plain"}
    assert client.get(streamed["key"], streamed["sha256"]) == b"stream"
    key, digest = client.put(b"old put")
    assert client.get(key, digest) == b"old put"


@pytest.mark.parametrize("corrupt", [False, True])
def test_iter_get_bounds_reads_and_closes_on_completion_or_integrity_error(corrupt):
    class Body(io.BytesIO):
        def read(self, size=-1):
            assert size == 3
            return super().read(size)

    body = Body(b"download")
    class S3:
        def get_object(self, **kwargs):
            return {"Body": body}

    client = RustFSClient(endpoint="http://x", bucket="b", s3=S3())
    digest = "0" * 64 if corrupt else hashlib.sha256(b"download").hexdigest()
    if corrupt:
        with pytest.raises(IntegrityError):
            b"".join(client.iter_get("key", digest, chunk_size=3))
    else:
        assert b"".join(client.iter_get("key", digest, chunk_size=3)) == b"download"
    assert body.closed


def test_iter_get_closes_response_when_consumer_stops_early():
    body = io.BytesIO(b"early close")
    class S3:
        def get_object(self, **kwargs):
            return {"Body": body}

    client = RustFSClient(endpoint="http://x", bucket="b", s3=S3())
    stream = client.iter_get("key", hashlib.sha256(b"early close").hexdigest(), chunk_size=3)
    assert next(stream) == b"ear"
    stream.close()
    assert body.closed


@pytest.mark.parametrize("size", [0, -1])
def test_streaming_rejects_nonpositive_chunks(size):
    client, s3 = _client()
    with pytest.raises(ValueError, match="chunk_size"):
        client.put_stream(io.BytesIO(b"data"), chunk_size=size)
    with pytest.raises(ValueError, match="chunk_size"):
        list(client.iter_get("key", "digest", chunk_size=size))
    assert s3.objects == {}


class _SpoolRecordingS3(_StreamingS3):
    def __init__(self):
        super().__init__()
        self.spool_dirs = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.spool_dirs.append(os.path.dirname(fileobj.name))
        super().upload_fileobj(fileobj, bucket, key, ExtraArgs=ExtraArgs)


def test_put_stream_spools_into_the_given_dir_and_defaults_to_the_temp_dir(tmp_path):
    s3 = _SpoolRecordingS3()
    client = RustFSClient(endpoint="http://x", bucket="b", s3=s3)
    spool = tmp_path / "spool"
    spool.mkdir()
    client.put_stream(io.BytesIO(b"in the spool dir"), spool_dir=spool)
    client.put_stream(io.BytesIO(b"in the default dir"))
    assert s3.spool_dirs == [str(spool), tempfile.gettempdir()]
    assert list(spool.iterdir()) == []
