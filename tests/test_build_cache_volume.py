"""Volume identity, mount and enrollment logic exercised on any OS with real directories and real plist bytes."""
import json
import plistlib
from pathlib import Path

import pytest
import yaml

from herdr_engineering.build_cache_location import (
    StorageTier,
    select_storage,
    volume_mount_from_plist,
    writable_mount,
)
from herdr_engineering.cargo_host import enroll_volume

UUID = "11111111-2222-4333-8444-555555555555"
MARKER = ".herdr-compiler-cache.json"


def _plist(**overrides) -> bytes:
    info = {"VolumeUUID": UUID.upper(), "WritableVolume": True, "MountPoint": "/"}
    info.update(overrides)
    return plistlib.dumps(info)


def _writable_mount() -> Path:
    """A real mount point this user can write to (the shared-memory tmpfs on Linux)."""
    for candidate in (Path("/dev/shm"), Path.home()):
        if writable_mount(candidate):
            return candidate
    pytest.skip("no writable mount point on this host")


def test_plist_with_matching_identity_resolves_the_mount():
    mount = _writable_mount()
    assert volume_mount_from_plist(_plist(MountPoint=str(mount)), UUID) == mount


@pytest.mark.parametrize("raw", [
    b"not a plist", plistlib.dumps([1, 2]), _plist(VolumeUUID="99999999-2222-4333-8444-555555555555"),
    _plist(WritableVolume=False), _plist(MountPoint=7),
    plistlib.dumps({"VolumeUUID": UUID, "WritableVolume": True}),
])
def test_plist_that_is_malformed_or_foreign_resolves_nothing(raw: bytes):
    assert volume_mount_from_plist(raw, UUID) is None


def test_plist_naming_a_plain_directory_is_not_a_mount(tmp_path: Path):
    assert volume_mount_from_plist(_plist(MountPoint=str(tmp_path)), UUID) is None


def test_writable_mount_requires_a_mount_point(tmp_path: Path):
    mount = _writable_mount()
    assert writable_mount(mount) == mount
    assert writable_mount(tmp_path) is None
    assert writable_mount(tmp_path / "missing") is None


def _tier(directory: str = "cache") -> StorageTier:
    return StorageTier("primary", directory, UUID)


def _select(tmp_path: Path, mount: Path | None, directory: str = "cache"):
    return select_storage((_tier(directory),), tmp_path / "default", 0, resolve_volume=lambda _identity: mount)


def _enrolled(mount: Path, volume_uuid: str = UUID) -> Path:
    cache = mount / "cache"
    cache.mkdir(parents=True)
    (cache / MARKER).write_text(json.dumps({"volume_uuid": volume_uuid}))
    return cache


def test_volume_selection_succeeds_on_an_enrolled_directory(tmp_path: Path):
    mount = tmp_path / "vol"
    cache = _enrolled(mount, UUID.upper())
    selected = _select(tmp_path, mount)
    assert selected.name == "primary" and selected.directory == cache and selected.skipped == ()


def test_unmounted_volume_is_skipped(tmp_path: Path):
    assert _select(tmp_path, None).skipped == ("primary:volume_unavailable",)


def test_missing_cache_directory_is_skipped(tmp_path: Path):
    mount = tmp_path / "vol"
    mount.mkdir()
    assert _select(tmp_path, mount).skipped == ("primary:cache_directory_unavailable",)


def test_symlink_escaping_the_mount_is_skipped(tmp_path: Path):
    mount = tmp_path / "vol"
    mount.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (mount / "cache").symlink_to(outside)
    assert _select(tmp_path, mount).skipped == ("primary:cache_directory_unavailable",)


@pytest.mark.parametrize("content", [json.dumps({"volume_uuid": "other"}), json.dumps([1])])
def test_marker_for_another_volume_is_skipped(tmp_path: Path, content: str):
    mount = tmp_path / "vol"
    (_enrolled(mount) / MARKER).write_text(content)
    assert _select(tmp_path, mount).skipped == ("primary:cache_identity_mismatch",)


def test_missing_or_invalid_marker_means_not_enrolled(tmp_path: Path):
    mount = tmp_path / "vol"
    cache = _enrolled(mount)
    (cache / MARKER).write_text("{not json")
    assert _select(tmp_path, mount).skipped == ("primary:cache_not_enrolled",)
    (cache / MARKER).unlink()
    assert _select(tmp_path, mount).skipped == ("primary:cache_not_enrolled",)


# --- enrollment core -------------------------------------------------------------------------------------------

def _enroll(tmp_path: Path, mount: Path | None, config: Path, directory: str = "herdr/cache", **kwargs):
    return enroll_volume(UUID, kwargs.pop("name", "primary"), directory, config,
                         resolve_volume=lambda _identity: mount, internal_cache=tmp_path / "internal", **kwargs)


def _config(tmp_path: Path, text: str = "fabric_build_cache:\n  enabled: true\n") -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(text)
    return config


def test_enrollment_writes_marker_config_internal_fallback_and_backup(tmp_path: Path):
    mount = tmp_path / "vol"
    mount.mkdir()
    config = _config(tmp_path)
    result = _enroll(tmp_path, mount, config)
    cache = mount / "herdr/cache"
    assert result == {"name": "primary", "volume_uuid": UUID, "cache_directory": str(cache)}
    assert json.loads((cache / MARKER).read_text()) == {"version": 1, "volume_uuid": UUID}
    tiers = yaml.safe_load(config.read_text())["fabric_build_cache"]["local"]["storage_tiers"]
    assert [t["name"] for t in tiers] == ["primary", "internal"]
    assert tiers[1]["directory"] == str(tmp_path / "internal")
    assert list(tmp_path.glob("config.yaml*")) != [config]  # a backup of the prior config exists
    assert select_storage(tuple(StorageTier(t["name"], t["directory"], t.get("volume_uuid")) for t in tiers),
                          tmp_path / "d", 0, resolve_volume=lambda _i: mount).directory == cache


def test_reenrolling_replaces_the_tier_and_honours_priority(tmp_path: Path):
    mount = tmp_path / "vol"
    mount.mkdir()
    config = _config(tmp_path, "fabric_build_cache:\n  enabled: true\n  local:\n    storage_tiers:\n"
                               "      - {name: internal, directory: /var/cache/x}\n")
    _enroll(tmp_path, mount, config)
    _enroll(tmp_path, mount, config, name="renamed", priority=1)
    tiers = yaml.safe_load(config.read_text())["fabric_build_cache"]["local"]["storage_tiers"]
    assert [t["name"] for t in tiers] == ["internal", "renamed"]


def test_enrollment_refuses_an_unmounted_volume(tmp_path: Path):
    with pytest.raises(ValueError, match="not mounted"):
        _enroll(tmp_path, None, _config(tmp_path))


def test_enrollment_refuses_a_symlink_escaping_the_volume(tmp_path: Path):
    mount = tmp_path / "vol"
    mount.mkdir()
    (tmp_path / "outside").mkdir()
    (mount / "herdr").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="escapes"):
        _enroll(tmp_path, mount, _config(tmp_path))


def test_enrollment_refuses_a_foreign_non_empty_directory(tmp_path: Path):
    mount = tmp_path / "vol"
    (mount / "herdr/cache").mkdir(parents=True)
    (mount / "herdr/cache/user-data").write_text("keep")
    with pytest.raises(ValueError, match="not a managed"):
        _enroll(tmp_path, mount, _config(tmp_path))
    assert (mount / "herdr/cache/user-data").read_text() == "keep"


def test_enrollment_accepts_an_existing_managed_directory(tmp_path: Path):
    mount = tmp_path / "vol"
    cache = mount / "herdr/cache"
    cache.mkdir(parents=True)
    (cache / MARKER).write_text("{}")
    (cache / "objects").write_text("x")
    assert _enroll(tmp_path, mount, _config(tmp_path))["cache_directory"] == str(cache)


def test_enrollment_detects_a_volume_that_changed_underneath(tmp_path: Path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    answers = iter([first, second])
    with pytest.raises(ValueError, match="changed during"):
        enroll_volume(UUID, "primary", "c", _config(tmp_path), resolve_volume=lambda _i: next(answers),
                      internal_cache=tmp_path / "internal")


def test_enrollment_detects_concurrent_config_edit(tmp_path: Path):
    mount = tmp_path / "vol"
    mount.mkdir()
    config = _config(tmp_path)
    calls = []

    def resolver(_identity: str) -> Path:
        calls.append(1)
        if len(calls) == 2:  # between the config read and its write
            config.write_text("fabric_build_cache:\n  enabled: true\n# edited\n")
        return mount

    with pytest.raises(ValueError, match="changed concurrently"):
        enroll_volume(UUID, "primary", "c", config, resolve_volume=resolver, internal_cache=tmp_path / "internal")
    assert "# edited" in config.read_text()
