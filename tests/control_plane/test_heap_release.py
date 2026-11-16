"""release_heap(): returns freed Python heap to the OS; control_plane.heap_release config."""
import gc
import hashlib
import logging
import os
import sys

import pytest
import yaml

from herdr_engineering.control_plane.heap_release import (
    HeapRelease,
    heap_release_enabled,
    parse_heap_release,
    release_heap,
)
from herdr_engineering.fleet_config import FleetConfigError, FleetConfigStore

needs_glibc = pytest.mark.skipif(
    not os.path.exists("/proc/self/statm") or not sys.platform.startswith("linux")
    or not os.path.exists("/lib/x86_64-linux-gnu/libc.so.6") and not os.path.exists("/lib/aarch64-linux-gnu/libc.so.6"),
    reason="needs Linux /proc and glibc malloc_trim")


def _allocate_and_free(count=1_500_000):
    rows = [{"event_id": i, "payload": {"k": "v" * 8, "n": i}} for i in range(count)]
    return rows


@needs_glibc
def test_release_heap_returns_memory_after_freeing_many_small_dicts(caplog):
    rows = _allocate_and_free()
    held = len(rows)
    del rows
    gc.collect()
    with caplog.at_level(logging.INFO):
        result = release_heap()
    assert held and isinstance(result, HeapRelease)
    assert result.trimmed is True
    assert result.rss_before_bytes > 0
    assert result.rss_after_bytes <= result.rss_before_bytes
    assert any("heap release: rss" in r.getMessage() and "trim=True" in r.getMessage() for r in caplog.records)


@needs_glibc
def test_release_heap_shows_a_real_rss_drop():
    # Rows over pymalloc's 512-byte limit live in glibc's heap; freeing most of them while a few survive leaves
    # the freed pages resident until malloc_trim hands them back (the replay's temporary row dicts).
    rows = [{"id": i, "blob": bytes(2048), "cols": list(range(60))} for i in range(200_000)]
    survivors = rows[::50]
    rows = None
    result = release_heap()
    assert survivors and rows is None
    assert result.trimmed is True
    assert result.rss_before_bytes - result.rss_after_bytes >= 50 * 1024 * 1024


def test_release_heap_never_raises_when_the_library_is_missing():
    result = release_heap(libc_name="definitely-not-a-real-libc.so.99")
    assert result.trimmed is False
    assert result.rss_after_bytes >= 0 and result.rss_before_bytes >= 0


def test_release_heap_never_raises_with_unresolvable_library_name():
    assert release_heap(libc_name="").trimmed is False


def test_parse_heap_release_defaults_true():
    assert parse_heap_release(None) == {"enabled": True}
    assert parse_heap_release({}) == {"enabled": True}
    assert parse_heap_release({"enabled": False}) == {"enabled": False}


@pytest.mark.parametrize("bad", ["yes", 1, 0, "true", []])
def test_parse_heap_release_rejects_non_boolean(bad):
    with pytest.raises(ValueError, match="heap_release.enabled must be a boolean"):
        parse_heap_release({"enabled": bad})


@pytest.mark.parametrize("bad", ["on", 3, [True]])
def test_parse_heap_release_rejects_non_mapping(bad):
    with pytest.raises(ValueError, match="heap_release must be a mapping"):
        parse_heap_release(bad)


def test_parse_heap_release_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown key"):
        parse_heap_release({"enabled": True, "interval": 5})


def test_heap_release_enabled_reads_the_control_plane_mapping():
    assert heap_release_enabled({}) is True
    assert heap_release_enabled({"heap_release": {"enabled": False}}) is False


def _fleet(tmp_path, heap_release):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {"control_plane": {"roles": ["control"], "values": {"heap_release": heap_release}}},
        "hosts": {"h": {"roles": ["node"], "token_sha256": hashlib.sha256(b"t").hexdigest()}}}))
    os.chmod(path, 0o600)
    return path


def test_fleet_config_accepts_valid_heap_release(tmp_path):
    FleetConfigStore(_fleet(tmp_path, {"enabled": False})).current()


def test_fleet_config_rejects_non_boolean_heap_release(tmp_path):
    with pytest.raises(FleetConfigError, match="heap_release.enabled must be a boolean"):
        FleetConfigStore(_fleet(tmp_path, {"enabled": "false"})).current()
