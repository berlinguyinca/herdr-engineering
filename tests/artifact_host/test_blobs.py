from __future__ import annotations

import hashlib
import io
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


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a" * 63, "a" * 65, "A" * 64, ("a" * 63) + "/",
                                 "../" + "a" * 61, "", "g" * 64, ("a" * 64) + "\n"])
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
