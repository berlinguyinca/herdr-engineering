"""Object-key privacy and manifest integrity at the actual S3 adapter boundary."""
import copy
import hashlib
import importlib
import io
import json
import traceback

import pytest
from botocore.exceptions import ClientError

ROOM = "room_01arz3ndektsv4rrffq69g5fav"
FILE = "fbd5fbef-6ae2-497e-9833-1576c44f36f0"
UPLOAD = "4bc281b2-2479-443c-a752-370149b1b5d9"
ARCHIVE = "890ad6f5-76a8-44c7-a83a-56cd5cda18eb"
PREFIX = f"env/test/archives/{ARCHIVE}/"
HELLO_SHA = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def storage():
    # Import inside tests so an absent implementation produces explicit test failures.
    return importlib.import_module("herdr_engineering.control_plane.collab_storage")


class ChunkedBody(io.BytesIO):
    def read(self, size=-1):
        assert 0 < size <= 1024 * 1024, "verification must use bounded reads"
        return super().read(min(size, 2))


class MemoryS3:
    """Only the external S3 transport is replaced; adapter and hashing stay real."""

    def __init__(self):
        self.objects = {}
        self.bodies = []
        self.requests = []

    def put_object(self, *, Bucket, Key, Body):
        self.requests.append(("put", Bucket, Key))
        self.objects[Bucket, Key] = Body

    def get_object(self, *, Bucket, Key):
        self.requests.append(("get", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = ChunkedBody(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, "ETag": '"not-a-sha256-2"'}


def manifest():
    return {
        "schema_version": 1,
        "archive_id": ARCHIVE,
        "original_room_id": ROOM,
        "created_at": "2026-10-24T12:00:00Z",
        "encryption": {"mode": "provider-managed"},
        "integrity": {"algorithm": "sha256"},
        "objects": [{"key": PREFIX + "room.json", "size_bytes": 5, "sha256": HELLO_SHA}],
    }


def adapter(s3, **options):
    mod = storage()
    return mod.CollabStorage(mod.S3Config(environment="test", **options), s3=s3)


def test_keys_match_spec_and_keep_names_out_of_upload_paths():
    mod = storage()
    base = f"env/test/rooms/{ROOM}"
    assert mod.live_key("test", ROOM, FILE) == f"{base}/files/{FILE}/blob"
    assert mod.live_key("test", ROOM, FILE, "thumbnail") == (
        f"{base}/files/{FILE}/preview/thumbnail"
    )
    path = "Secret Research/alice@example.org.csv"
    key = mod.upload_key("test", ROOM, UPLOAD, path)
    assert key == f"{base}/uploads/{UPLOAD}/{hashlib.sha256(path.encode()).hexdigest()}"
    assert key == mod.upload_key("test", ROOM, UPLOAD, path)
    assert key != mod.upload_key("test", ROOM, UPLOAD, "Other/" + path)
    assert "Secret" not in key and "alice" not in key and "@" not in key
    assert mod.archive_key("test", ARCHIVE, "manifest.json") == PREFIX + "manifest.json"
    assert mod.archive_key("test", ARCHIVE, f"files/{FILE}/blob") == (
        PREFIX + f"files/{FILE}/blob"
    )


@pytest.mark.parametrize("value", ["", "../x", "room/other", "alice@example.org", "Research Room", "research-room"])
def test_keys_reject_names_or_unsafe_identifiers(value):
    mod = storage()
    for call in (
        lambda: mod.live_key("test", value, FILE),
        lambda: mod.live_key("test", ROOM, value),
        lambda: mod.upload_key("test", ROOM, value, "data.csv"),
        lambda: mod.archive_key("test", value, "manifest.json"),
    ):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("path", ["", "/root", "../secret", "a/../b", "a/./b", "a//b", "a/", "a\\b", "a\x00b"])
def test_upload_paths_cannot_escape_or_alias(path):
    with pytest.raises(ValueError):
        storage().upload_key("test", ROOM, UPLOAD, path)


@pytest.mark.parametrize("value", ["../prod", "a/b", "alice@example.org", "team room", "", "%2e%2e"])
def test_environment_and_variant_are_single_safe_segments(value):
    mod = storage()
    with pytest.raises(ValueError):
        mod.live_key(value, ROOM, FILE)
    with pytest.raises(ValueError):
        mod.live_key("test", ROOM, FILE, value)


@pytest.mark.parametrize("name", ["../manifest.json", "/room.json", "alice@example.org.json", "Research Room.json", "files/research-room/blob"])
def test_archive_names_do_not_leak_identity_or_escape(name):
    with pytest.raises(ValueError):
        storage().archive_key("test", ARCHIVE, name)


def test_manifest_write_is_canonical_and_returns_exact_object_reference():
    s3 = MemoryS3()
    client = adapter(s3, archive_bucket="custom-archive", live_bucket="custom-live")
    doc = manifest()
    doc["label"] = "résumé"
    before = copy.deepcopy(doc)
    ref = client.write_manifest(ARCHIVE, doc)
    expected = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert s3.objects == {("custom-archive", PREFIX + "manifest.json"): expected}
    assert ref.bucket == "custom-archive"
    assert ref.key == PREFIX + "manifest.json"
    assert ref.sha256 == hashlib.sha256(expected).hexdigest()
    assert ref.size_bytes == len(expected)
    reordered = dict(reversed(list(doc.items())))
    reordered["objects"] = [dict(reversed(list(doc["objects"][0].items())))]
    assert client.write_manifest(ARCHIVE, reordered) == ref
    assert doc == before


def test_verification_reads_all_bytes_and_closes_streams():
    s3 = MemoryS3()
    s3.objects["custom-archive", PREFIX + "room.json"] = b"hello"
    result = storage().verify_manifest(manifest(), adapter(s3, archive_bucket="custom-archive"))
    assert result.ok and result.objects_checked == 1 and result.errors == ()
    assert s3.bodies and all(body.closed for body in s3.bodies)
    assert all(request[0] == "get" for request in s3.requests)


def test_verification_reports_corruption_size_and_missing_objects():
    s3 = MemoryS3()
    doc = manifest()
    doc["objects"].extend([
        {"key": PREFIX + "messages.ndjson", "size_bytes": 6, "sha256": HELLO_SHA},
        {"key": PREFIX + "tasks.ndjson", "size_bytes": 5, "sha256": HELLO_SHA},
    ])
    s3.objects["herdr-collab-archive", PREFIX + "room.json"] = b"HELLO"
    s3.objects["herdr-collab-archive", PREFIX + "messages.ndjson"] = b"hello"
    result = storage().verify_manifest(doc, adapter(s3))
    assert not result.ok and result.objects_checked == 3
    assert {(error.key, error.reason) for error in result.errors} == {
        (PREFIX + "room.json", "sha256_mismatch"),
        (PREFIX + "messages.ndjson", "size_mismatch"),
        (PREFIX + "tasks.ndjson", "unavailable"),
    }
    assert all(body.closed for body in s3.bodies)


@pytest.mark.parametrize("change", [
    {"key": "env/prod/archives/other/room.json"},
    {"key": PREFIX + "../room.json"},
    {"key": PREFIX + "manifest.json"},
    {"sha256": "not-a-checksum"},
    {"size_bytes": -1},
    {"size_bytes": True},
    {"size_bytes": 5.0},
])
def test_invalid_object_entries_are_rejected_before_io(change):
    s3 = MemoryS3()
    client = adapter(s3)
    doc = manifest()
    doc["objects"][0].update(change)
    with pytest.raises(ValueError):
        client.write_manifest(ARCHIVE, doc)
    with pytest.raises(ValueError):
        storage().verify_manifest(doc, client)
    assert not s3.requests


@pytest.mark.parametrize("case", ["duplicate", "wrong_archive", "missing_objects", "bad_version", "nan"])
def test_invalid_manifests_are_rejected_before_writing(case):
    doc = manifest()
    if case == "duplicate":
        doc["objects"] *= 2
    elif case == "wrong_archive":
        doc["archive_id"] = FILE
    elif case == "missing_objects":
        del doc["objects"]
    elif case == "bad_version":
        doc["schema_version"] = 99
    else:
        doc["extra"] = float("nan")
    s3 = MemoryS3()
    with pytest.raises(ValueError):
        adapter(s3).write_manifest(ARCHIVE, doc)
    assert not s3.requests


def test_s3_configuration_reaches_provider_without_exposing_credentials(monkeypatch):
    import boto3

    calls = []
    s3 = MemoryS3()

    def factory(service, **kwargs):
        calls.append((service, kwargs))
        return s3

    monkeypatch.setattr(boto3, "client", factory)
    mod = storage()
    config = mod.S3Config(environment="test", endpoint="https://objects.example.org",
                          region="eu-west-1", access_key="private-access",
                          secret_key="private-secret", session_token="private-token",
                          addressing_style="path")
    mod.CollabStorage(config).write_manifest(ARCHIVE, manifest())
    assert calls
    for service, kwargs in calls:
        assert service == "s3"
        assert kwargs["endpoint_url"] == "https://objects.example.org"
        assert kwargs["region_name"] == "eu-west-1"
        assert kwargs["aws_access_key_id"] == "private-access"
        assert kwargs["aws_secret_access_key"] == "private-secret"
        assert kwargs["aws_session_token"] == "private-token"
        assert kwargs["config"].s3["addressing_style"] == "path"
    assert "private-" not in repr(config)
    assert ("herdr-collab-archive", PREFIX + "manifest.json") in s3.objects


def test_default_credentials_use_sdk_chain_and_buckets_are_separate(monkeypatch):
    import boto3

    calls = []

    def factory(service, **kwargs):
        calls.append(kwargs)
        return MemoryS3()

    monkeypatch.setattr(boto3, "client", factory)
    mod = storage()
    mod.CollabStorage(mod.S3Config(environment="test"))
    assert calls
    assert all(call.get("aws_access_key_id") is None for call in calls)
    assert all(call.get("aws_secret_access_key") is None for call in calls)
    with pytest.raises(ValueError):
        mod.S3Config(environment="test", live_bucket="same", archive_bucket="same")


@pytest.mark.parametrize("extra", [{1: "not a JSON object key"}, {"nested": {False: "bad"}},
                                  {"nested": (1, 2)}, {"value": float("nan")},
                                  {"value": float("inf")}])
def test_manifest_rejects_non_json_values_for_both_writes_and_verification(extra):
    doc = manifest()
    doc["extra"] = extra
    s3 = MemoryS3()
    client = adapter(s3)
    with pytest.raises(ValueError):
        client.write_manifest(ARCHIVE, doc)
    with pytest.raises(ValueError):
        storage().verify_manifest(doc, client)
    assert not s3.requests


def test_interrupted_stream_is_closed_and_error_details_are_not_reported():
    class BrokenBody(ChunkedBody):
        def read(self, size=-1):
            if self.tell():
                raise OSError("secret signed URL must not escape")
            return super().read(size)

    class BrokenS3(MemoryS3):
        def get_object(self, *, Bucket, Key):
            return {"Body": body}

    body = BrokenBody(b"hello")
    report = storage().verify_manifest(manifest(), adapter(BrokenS3()))
    assert not report.ok and report.objects_checked == 1
    assert report.errors == (storage().VerificationFailure(PREFIX + "room.json", "unavailable"),)
    assert body.closed
    assert "secret" not in repr(report)


@pytest.mark.parametrize("error_type", ["provider", "transport"])
def test_failed_write_does_not_return_an_object_reference(error_type):
    token = "raw-private-signed-token"
    detail = f"https://objects.example.org/archive?X-Amz-Security-Token={token}"
    provider_error = (ClientError({"Error": {"Code": "AccessDenied", "Message": detail}},
                                  "PutObject") if error_type == "provider" else OSError(detail))

    class UnwritableS3(MemoryS3):
        def put_object(self, *, Bucket, Key, Body):
            raise provider_error

    client = adapter(UnwritableS3())
    ref = None
    with pytest.raises(Exception) as caught:
        ref = client.write_manifest(ARCHIVE, manifest())
    assert ref is None
    error = caught.value
    assert token not in str(error)
    assert token not in repr(error)
    assert token not in "".join(traceback.format_exception(error))
    assert isinstance(error, storage().StorageError)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


@pytest.mark.parametrize("operation", ["write", "verify"])
@pytest.mark.parametrize("field", ["created_at", "encryption", "integrity"])
def test_manifest_requires_metadata_before_io(operation, field):
    doc = manifest()
    del doc[field]
    s3 = MemoryS3()
    client = adapter(s3)
    with pytest.raises(ValueError):
        if operation == "write":
            client.write_manifest(ARCHIVE, doc)
        else:
            storage().verify_manifest(doc, client)
    assert not s3.requests


@pytest.mark.parametrize("operation", ["write", "verify"])
@pytest.mark.parametrize(("field", "value"), [
    ("created_at", None),
    ("created_at", 1792843200),
    ("created_at", {}),
    ("created_at", ""),
    ("created_at", "not-a-timestamp"),
    ("created_at", "2026-02-30T12:00:00Z"),
    ("created_at", "2026-10-24"),
    ("created_at", "2026-10-24T12:00:00"),
    ("encryption", None),
    ("encryption", "provider-managed"),
    ("encryption", []),
    ("encryption", {}),
    ("encryption", {"mode": None}),
    ("encryption", {"mode": True}),
    ("encryption", {"mode": ""}),
    ("encryption", {"mode": "   "}),
    ("integrity", None),
    ("integrity", "sha256"),
    ("integrity", []),
    ("integrity", {}),
    ("integrity", {"algorithm": None}),
    ("integrity", {"algorithm": True}),
    ("integrity", {"algorithm": ""}),
    ("integrity", {"algorithm": "md5"}),
])
def test_manifest_rejects_malformed_metadata_before_io(operation, field, value):
    doc = manifest()
    doc[field] = value
    s3 = MemoryS3()
    client = adapter(s3)
    with pytest.raises(ValueError):
        if operation == "write":
            client.write_manifest(ARCHIVE, doc)
        else:
            storage().verify_manifest(doc, client)
    assert not s3.requests


def test_valid_metadata_is_preserved_with_an_explicit_timestamp_offset():
    doc = manifest()
    doc["created_at"] = "2026-10-24T05:00:00.123456-07:00"
    doc["encryption"]["algorithm"] = "AES256"
    doc["integrity"]["description"] = "full object SHA-256"
    s3 = MemoryS3()
    s3.objects["herdr-collab-archive", PREFIX + "room.json"] = b"hello"
    client = adapter(s3)
    ref = client.write_manifest(ARCHIVE, doc)
    assert json.loads(s3.objects[ref.bucket, ref.key]) == doc
    assert storage().verify_manifest(doc, client).ok


@pytest.mark.parametrize("name", ["room.json", "members.ndjson", "messages.ndjson", "tasks.ndjson",
                                  f"sessions/{FILE}.ndjson", f"files/{FILE}/metadata.json",
                                  f"files/{FILE}/preview/thumbnail"])
def test_spec_archive_names_are_supported(name):
    assert storage().archive_key("test", ARCHIVE, name) == PREFIX + name


def test_empty_objects_manifest_has_a_deterministic_successful_report():
    doc = manifest()
    doc["objects"] = []
    s3 = MemoryS3()
    result = storage().verify_manifest(doc, adapter(s3))
    assert result.ok and result.objects_checked == 0 and result.errors == ()
    assert not s3.requests


@pytest.mark.parametrize('read_case', ['success', 'checksum_failure', 'read_failure'])
def test_archive_metadata_close_errors_are_sanitized_including_tracebacks(read_case):
    token = 'private-close-token-123'
    detail = f'https://objects.example.org/archive?X-Amz-Security-Token={token}'

    class CloseFailure(ChunkedBody):
        def read(self, size=-1):
            if read_case == 'read_failure':
                raise OSError(detail)
            return super().read(size)

        def close(self):
            super().close()
            raise OSError(detail)

    body = CloseFailure(b'hello')

    class CloseFailureS3(MemoryS3):
        def get_object(self, *, Bucket, Key):
            return {'Body': body}

    client = adapter(CloseFailureS3())
    digest = '0' * 64 if read_case == 'checksum_failure' else HELLO_SHA
    with pytest.raises(Exception) as caught:
        client.read_archive(ARCHIVE, 'room.json', digest)
    error = caught.value
    assert body.closed
    assert token not in str(error)
    assert token not in repr(error)
    assert token not in ''.join(traceback.format_exception(error))
    assert isinstance(error, storage().StorageError)
    assert error.__cause__ is None and error.__suppress_context__ is True
