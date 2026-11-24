from __future__ import annotations

import hashlib
import io
import mmap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from herdr_artifact_host.blobs import BlobCorrupt, BlobMissing, BlobStore, InvalidSha


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_round_trip_and_layout(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    data = b"hello artifact" * 1000
    sha = store.put(io.BytesIO(data))
    assert sha == sha_of(data)
    assert store.exists(sha)
    assert (tmp_path / "sha256" / sha[:2] / sha[2:4] / sha).is_file()
    with store.open(sha) as handle:
        assert handle.read() == data
    assert list((tmp_path / "tmp").iterdir()) == []


def test_empty_blob(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha = store.put(io.BytesIO(b""))
    with store.open(sha) as handle:
        assert handle.read() == b""


def test_flipped_byte_detected_on_read(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha = store.put(io.BytesIO(b"precious bytes"))
    path = tmp_path / "sha256" / sha[:2] / sha[2:4] / sha
    raw = bytearray(path.read_bytes())
    raw[3] ^= 0x01
    path.write_bytes(bytes(raw))
    with pytest.raises(BlobCorrupt):
        store.open(sha)


def test_missing_blob(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    assert not store.exists("0" * 64)
    with pytest.raises(BlobMissing):
        store.open("0" * 64)


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        ("a" * 63) + "/",
        "../" + "a" * 61,
        "",
        "g" * 64,
        ("a" * 64) + "\n",
    ],
)
def test_invalid_sha_refused_before_any_path_use(tmp_path: Path, bad: str) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(InvalidSha):
        store.open(bad)
    with pytest.raises(InvalidSha):
        store.exists(bad)


def test_identical_content_deduped(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    a = store.put(io.BytesIO(b"same"))
    b = store.put(io.BytesIO(b"same"))
    assert a == b
    files = [p for p in (tmp_path / "sha256").rglob("*") if p.is_file()]
    assert len(files) == 1
    assert list((tmp_path / "tmp").iterdir()) == []


def test_concurrent_puts_of_same_content_are_safe(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    data = b"x" * (3 << 20)
    with ThreadPoolExecutor(8) as pool:
        shas = list(pool.map(lambda _: store.put(io.BytesIO(data)), range(16)))
    assert set(shas) == {sha_of(data)}
    with store.open(shas[0]) as handle:
        assert handle.read() == data
    assert list((tmp_path / "tmp").iterdir()) == []


def test_reput_repairs_corrupt_blob(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha = store.put(io.BytesIO(b"good bytes"))
    path = tmp_path / "sha256" / sha[:2] / sha[2:4] / sha
    path.write_bytes(b"rotten")
    with pytest.raises(BlobCorrupt):
        store.open(sha)
    assert store.put(io.BytesIO(b"good bytes")) == sha
    with store.open(sha) as handle:
        assert handle.read() == b"good bytes"


class _Exploding(io.RawIOBase):
    def __init__(self) -> None:
        self.sent = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if not self.sent:
            self.sent = True
            return b"partial"
        raise OSError("connection reset")


def test_failed_stream_leaves_no_temp_or_blob(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(OSError):
        store.put(_Exploding())  # type: ignore[arg-type]
    assert list((tmp_path / "tmp").iterdir()) == []
    assert [p for p in (tmp_path / "sha256").rglob("*") if p.is_file()] == []


def test_verified_entries_expire_so_later_corruption_is_caught(tmp_path: Path) -> None:
    store = BlobStore(tmp_path, verified_ttl=0.5)
    sha = store.put(io.BytesIO(b"x" * 8192))
    handle, hashed = store.open_checked(sha)
    handle.close()
    assert hashed
    path = tmp_path / "sha256" / sha[:2] / sha[2:4] / sha
    # a shared mapping writes through the page cache without touching size or inode; the kernel stamps mtime/ctime
    # lazily, so a same-tick change can leave the identity key looking unchanged
    with path.open("r+b") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_WRITE) as m:
        m[0:1] = b"y"
        try:
            handle, hashed = store.open_checked(sha)  # within the TTL
            handle.close()
            residual = (
                not hashed
            )  # the documented residual: a cache hit may serve changed bytes for <= TTL
        except (
            BlobCorrupt
        ):  # the kernel stamped the write at once; the identity key already caught it
            residual = False
        time.sleep(0.7)
        if residual:
            with pytest.raises(BlobCorrupt):
                store.open_checked(sha)
    with pytest.raises(BlobCorrupt):
        store.open_checked(sha)


def test_an_unchanged_entry_is_hashed_again_after_the_ttl(tmp_path: Path) -> None:
    store = BlobStore(tmp_path, verified_ttl=0.4)
    sha = store.put(io.BytesIO(b"z" * 4096))
    for expected in (True, False):
        handle, hashed = store.open_checked(sha)
        handle.close()
        assert hashed is expected
    time.sleep(0.6)
    handle, hashed = store.open_checked(sha)
    handle.close()
    assert hashed


def test_default_verified_ttl_is_short() -> None:
    from herdr_artifact_host.blobs import VERIFIED_TTL_SECONDS

    assert 0 < VERIFIED_TTL_SECONDS <= 60


def test_a_hash_pass_stops_between_chunks_when_asked(tmp_path: Path) -> None:
    from herdr_artifact_host.blobs import BlobAborted

    store = BlobStore(tmp_path)
    sha = store.put(io.BytesIO(b"q" * (5 * (1 << 20))))
    calls = 0

    def stop_after_two() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    handle = store.open_handle(sha)
    try:
        with pytest.raises(BlobAborted):
            store.verify_open(handle, sha, should_stop=stop_after_two)
    finally:
        handle.close()
    assert calls == 3 and store.aborted == 1 and store.verifications == 0
    handle = store.open_handle(sha)  # an aborted pass remembered nothing
    assert not store.is_verified(handle, sha)
    assert store.verify_open(handle, sha, should_stop=lambda: False)
    assert store.is_verified(handle, sha)
    handle.close()


def test_a_hash_pass_that_is_already_cancelled_reads_nothing(tmp_path: Path) -> None:
    from herdr_artifact_host.blobs import BlobAborted

    store = BlobStore(tmp_path)
    sha = store.put(io.BytesIO(b"q" * 1024))
    handle = store.open_handle(sha)
    try:
        with pytest.raises(BlobAborted):
            store.verify_open(handle, sha, should_stop=lambda: True)
        assert handle.tell() == 0
    finally:
        handle.close()
