"""A stalled (network) mount must never block HostSampler.sample()."""
import platform
import threading
import time

import pytest

from herdr_engineering import host_metrics
from herdr_engineering.host_metrics import HostSampler


class _V:
    f_blocks, f_frsize, f_bfree = 1000, 1, 500


MOUNTS = "\n".join([
    "/dev/sda1 / ext4 rw 0 0",
    "/dev/sdb1 /mnt/hung xfs rw 0 0",
    "/dev/sdc1 /data ext4 rw 0 0",
])


@pytest.fixture
def linux(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_metrics, "_herdr_data_dir", lambda: str(tmp_path))
    s = HostSampler()
    monkeypatch.setattr(s, "_read", lambda p: MOUNTS if p == "/proc/mounts"
                        else (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(s, "_st_dev", lambda p: p)
    return s


def _blocking_statvfs(s, monkeypatch, hung_path, release):
    calls = []

    def statvfs(p):
        calls.append(p)
        if p == hung_path:
            release.wait(30)
        return _V
    monkeypatch.setattr(s, "_statvfs", statvfs)
    return calls


def test_hung_mount_is_omitted_and_sample_returns(linux, monkeypatch, tmp_path):
    release = threading.Event()
    calls = _blocking_statvfs(linux, monkeypatch, "/mnt/hung", release)
    try:
        t0 = time.monotonic()
        out = linux.sample()
        assert time.monotonic() - t0 < 3.5
        assert [m["path"] for m in out["mounts"]] == ["/", "/data"]
        assert "disk" in out and "mem" not in out  # other fields unaffected

        # still stuck: not re-probed, and the next sample is fast
        t0 = time.monotonic()
        out = linux.sample()
        assert time.monotonic() - t0 < 1.0
        assert calls.count("/mnt/hung") == 1
        assert [m["path"] for m in out["mounts"]] == ["/", "/data"]
    finally:
        release.set()
    # once the stuck probe finishes, the mount is probed again
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        out = linux.sample()
        if "/mnt/hung" in [m["path"] for m in out.get("mounts", [])]:
            break
        time.sleep(0.05)
    assert [m["path"] for m in out["mounts"]] == ["/", "/data", "/mnt/hung"]


def test_hung_disk_headline_is_omitted(linux, monkeypatch, tmp_path):
    release = threading.Event()
    calls = _blocking_statvfs(linux, monkeypatch, str(tmp_path), release)
    try:
        t0 = time.monotonic()
        out = linux.sample()
        assert time.monotonic() - t0 < 3.5
        assert "disk" not in out
        assert [m["path"] for m in out["mounts"]] == ["/", "/data", "/mnt/hung"]
        linux.sample()
        assert calls.count(str(tmp_path)) == 1
    finally:
        release.set()


def test_several_hung_mounts_still_bound_one_sample(linux, monkeypatch):
    lines = [f"/dev/sd{i} /mnt/h{i} xfs rw 0 0" for i in range(5)] + ["/dev/sdz / ext4 rw 0 0"]
    monkeypatch.setattr(linux, "_read", lambda p: "\n".join(lines) if p == "/proc/mounts"
                        else (_ for _ in ()).throw(OSError()))
    release = threading.Event()

    def statvfs(p):
        if p.startswith("/mnt/h"):
            release.wait(30)
        return _V
    monkeypatch.setattr(linux, "_statvfs", statvfs)
    try:
        t0 = time.monotonic()
        linux.sample()
        assert time.monotonic() - t0 < 6.0
    finally:
        release.set()


def test_network_filesystems_are_never_statvfsd(linux, monkeypatch):
    net = [
        "server:/export /mnt/nfs nfs rw 0 0",
        "server:/export4 /mnt/nfs4 nfs4 rw 0 0",
        "//nas/share /mnt/smb cifs rw 0 0",
        "//nas/share3 /mnt/smb3 smb3 rw 0 0",
        "/dev/fuse /quobyte fuse.quobyte rw 0 0",
        "/dev/fuse /mnt/ssh fuse.sshfs rw 0 0",
        "/dev/fuse /mnt/mfs fuse.mfs rw 0 0",
        "/dev/ceph /mnt/ceph ceph rw 0 0",
        "/dev/gl /mnt/gluster glusterfs rw 0 0",
        "/dev/gl /mnt/glusterfuse fuse.glusterfs rw 0 0",
        "/dev/afs /afs afs rw 0 0",
        "/dev/9p /mnt/9p 9p rw 0 0",
        "/dev/lu /lustre lustre rw 0 0",
        "/dev/bg /mnt/beegfs beegfs rw 0 0",
        "/dev/smbfs /mnt/smbfs smbfs rw 0 0",
    ]
    monkeypatch.setattr(linux, "_read", lambda p: "\n".join(net + [MOUNTS])
                        if p == "/proc/mounts" else (_ for _ in ()).throw(OSError()))
    calls = []
    monkeypatch.setattr(linux, "_statvfs", lambda p: calls.append(p) or _V)
    out = linux.sample()
    assert [m["path"] for m in out["mounts"]] == ["/", "/data", "/mnt/hung"]
    probed = {c for c in calls}
    for line in net:
        assert line.split()[1] not in probed


def test_macos_df_lists_local_filesystems_only(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    s = HostSampler()
    seen = []

    def run(argv):
        seen.append(argv)
        if argv[0] == "df":
            return "Filesystem 1024-blocks Used Available Capacity iused ifree %iused Mounted on\n" \
                   "/dev/disk3s1 100 50 50 50% 1 1 1% /\n"
        raise OSError("unavailable")
    monkeypatch.setattr(s, "_run", run)
    monkeypatch.setattr(s, "_statvfs", lambda p: _V)
    out = s.sample()
    assert ["df", "-k", "-l"] in seen
    assert [m["path"] for m in out["mounts"]] == ["/"]
