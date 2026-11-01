"""Task 3: the collab artifact bridge file contract (limits, staging layout, intents) and its fd-relative safe I/O."""
from __future__ import annotations

import base64
import errno
import hashlib
import math
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering.control_plane import collab_bridge_files as cbf
from herdr_engineering.control_plane.collab_bridge_files import (
    CHUNK_BYTES,
    BridgeError,
    BridgeLimits,
    IntentError,
)

AGENT = "agent_0000000000000000000000abcd"
ART = "artifact_00000000000000000000000001"
VER = "version_000000000000000000000000001"
SENTINEL = b"do not touch"
LIMITS = BridgeLimits()
TREE_CAP = 1000


def _snapshot(path: Path) -> dict:
    """Every entry under ``path`` (no-follow) with its bytes, so 'untouched' is checked exactly."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = os.lstat(p)
            out[str(p.relative_to(path))] = (stat.S_IFMT(st.st_mode),
                                              p.read_bytes() if stat.S_ISREG(st.st_mode) else None)
    return out


@pytest.fixture
def tree(tmp_path):
    """``outside/`` (a sentinel) beside the outbox ``root``."""
    root = tmp_path / "outbox"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "sentinel").write_bytes(SENTINEL)
    return root, outside


@pytest.fixture
def no_hang():
    """Fail fast (instead of hanging the suite) if a FIFO open ever blocks."""
    class Hung(BaseException):  # not an OSError, so no helper's ``except OSError`` can swallow it
        pass

    def boom(signum, frame):
        raise Hung("blocked on a FIFO")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(5)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


@pytest.fixture
def registry():
    saved = dict(cbf._INTENT_KINDS)
    yield
    cbf._INTENT_KINDS.clear()
    cbf._INTENT_KINDS.update(saved)


# ----------------------------------------------------------------------------- safe reads

def test_read_json_beneath_reads_a_regular_file(tree):
    root, _ = tree
    (root / "intents").mkdir()
    (root / "intents" / "a.json").write_text('{"x": "é"}')
    assert cbf.read_json_beneath(root, ["intents", "a.json"], max_bytes=100) == {"x": "é"}


def test_read_json_beneath_refuses_a_symlinked_file(tree):
    root, outside = tree
    (outside / "x.json").write_text('{"x": 1}')
    (root / "intents").mkdir()
    (root / "intents" / "a.json").symlink_to(outside / "x.json")
    assert cbf.read_json_beneath(root, ["intents", "a.json"], max_bytes=100) is None


def test_read_json_beneath_refuses_a_symlinked_parent(tree):
    root, outside = tree
    (outside / "a.json").write_text('{"x": 1}')
    (root / "intents").symlink_to(outside)
    assert cbf.read_json_beneath(root, ["intents", "a.json"], max_bytes=100) is None


def test_read_json_beneath_refuses_a_fifo_without_blocking(tree, no_hang):
    root, _ = tree
    os.mkfifo(root / "a.json")
    assert cbf.read_json_beneath(root, ["a.json"], max_bytes=100) is None


def test_read_json_beneath_refuses_an_oversize_file(tree):
    root, _ = tree
    (root / "a.json").write_text('{"x": "' + "y" * 200 + '"}')
    assert cbf.read_json_beneath(root, ["a.json"], max_bytes=100) is None


def test_read_json_beneath_refuses_a_hardlinked_file(tree):
    root, outside = tree
    (outside / "x.json").write_text('{"x": 1}')
    os.link(outside / "x.json", root / "a.json")
    assert cbf.read_json_beneath(root, ["a.json"], max_bytes=100) is None


@pytest.mark.parametrize("body", ["[1]", "not json", '{"x": NaN}', "[" * 100000 + "]" * 100000, '"\\ud800"'])
def test_read_json_beneath_refuses_non_objects_and_bad_json(tree, body):
    root, _ = tree
    (root / "a.json").write_text(body)
    assert cbf.read_json_beneath(root, ["a.json"], max_bytes=1_000_000) is None


@pytest.mark.parametrize("parts", [["..", "outside", "sentinel"], ["../outside/sentinel"], ["", "x"], ["."], ["a\0b"],
                                   [], [1]])
def test_reads_refuse_unsafe_components(tree, parts):
    root, _ = tree
    assert cbf.read_bytes_beneath(root, parts, max_bytes=100) is None
    assert cbf.open_beneath(root, parts) is None
    assert cbf._open_beneath(root, parts) is None


def test_read_bytes_beneath_and_open_beneath(tree):
    root, outside = tree
    (root / "p").mkdir()
    (root / "p" / "0000000").write_bytes(b"abc")
    (root / "p" / "0000001").symlink_to(outside / "sentinel")
    assert cbf.read_bytes_beneath(root, ["p", "0000000"], max_bytes=3) == b"abc"
    assert cbf.read_bytes_beneath(root, ["p", "0000000"], max_bytes=2) is None
    assert cbf.read_bytes_beneath(root, ["p", "0000001"], max_bytes=100) is None
    with cbf.open_beneath(root, ["p", "0000000"]) as fh:
        assert fh.read() == b"abc"
    assert cbf.open_beneath(root, ["p", "0000001"]) is None


def test_share_public_uses_the_moved_open_beneath():
    from herdr_engineering.control_plane import share_public
    assert share_public._open_beneath is cbf._open_beneath


def test_module_never_imports_asyncpg():
    code = ("import sys; import herdr_engineering.control_plane.collab_bridge_files; "
            "assert 'asyncpg' not in sys.modules, 'asyncpg imported'")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parents[2])


# ----------------------------------------------------------------------------- writes by the owning side

def test_atomic_write_json_writes_with_share_modes(tree):
    root, _ = tree
    cbf.atomic_write_json(root / "a" / "b", "x.json", {"k": "é"})
    target = root / "a" / "b" / "x.json"
    assert target.read_bytes() == '{"k": "é"}'.encode()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(root / "a").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(root / "a" / "b").st_mode) == 0o755
    assert [p.name for p in (root / "a" / "b").iterdir()] == ["x.json"]


def test_atomic_write_json_never_leaves_a_partial_target(tree, monkeypatch):
    root, _ = tree
    cbf.atomic_write_json(root, "x.json", {"v": 1})

    def boom(fd, data):
        os.write(fd, data[:3])
        raise OSError("disk full")
    monkeypatch.setattr(cbf, "_write_all", boom)
    with pytest.raises(OSError):
        cbf.atomic_write_json(root, "x.json", {"v": 2, "pad": "z" * 100})
    assert cbf.read_json_beneath(root, ["x.json"], max_bytes=1000) == {"v": 1}
    assert sorted(p.name for p in root.iterdir()) == ["x.json"]


def test_atomic_write_bytes_replaces(tree):
    root, _ = tree
    cbf.atomic_write_bytes(root, "b", b"one")
    cbf.atomic_write_bytes(root, "b", b"two")
    assert (root / "b").read_bytes() == b"two"
    assert stat.S_IMODE(os.stat(root / "b").st_mode) == 0o644


def test_atomic_write_refuses_unsafe_names(tree):
    root, _ = tree
    for name in ("..", "a/b", "", "."):
        with pytest.raises(BridgeError):
            cbf.atomic_write_bytes(root, name, b"x")


def test_link_new_bytes_never_replaces(tree):
    root, _ = tree
    assert cbf.link_new_bytes(root, "0000000", b"first") is True
    assert stat.S_IMODE(os.stat(root / "0000000").st_mode) == 0o600
    assert os.stat(root / "0000000").st_nlink == 1
    assert cbf.link_new_bytes(root, "0000000", b"second") is False
    assert (root / "0000000").read_bytes() == b"first"
    assert sorted(p.name for p in root.iterdir()) == ["0000000"]


def test_link_new_bytes_refuses_a_dangling_symlink_name(tree):
    root, outside = tree
    (root / "audited").symlink_to(outside / "new")
    assert cbf.link_new_bytes(root, "audited", b"x") is False
    assert not (outside / "new").exists()


def test_link_new_beneath_creates_dirs_no_follow_and_never_replaces(tree):
    root, _ = tree
    parts = ["staging", AGENT, "upl_" + "1" * 24]
    with pytest.raises(BridgeError):                         # without create_dirs a missing dir is refused
        cbf.link_new_beneath(root, parts, "meta.json", b"{}")
    assert not (root / "staging").exists()
    assert cbf.link_new_beneath(root, parts, "meta.json", b"{}", create_dirs=True) is True
    d = root.joinpath(*parts)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / "meta.json").st_mode) == 0o600 and os.stat(d / "meta.json").st_nlink == 1
    assert cbf.link_new_beneath(root, parts, "0000000", b"part") is True
    assert cbf.link_new_beneath(root, parts, "0000000", b"other") is False
    assert (d / "0000000").read_bytes() == b"part"
    for bad in ("", ".tmp-x", "../x", "a/b"):
        with pytest.raises(BridgeError):
            cbf.link_new_beneath(root, parts, bad, b"x")


@pytest.mark.parametrize("create_dirs", [False, True])
def test_link_new_beneath_never_follows_a_symlinked_component(tree, create_dirs):
    root, outside = tree
    (root / "staging").mkdir()
    (outside / UPL).mkdir()
    (root / "staging" / AGENT).symlink_to(outside)
    before = _snapshot(outside)
    with pytest.raises((BridgeError, OSError)):
        cbf.link_new_beneath(root, ["staging", AGENT, UPL], "0000000", b"x", create_dirs=create_dirs)
    assert _snapshot(outside) == before


# ----------------------------------------------------------------------------- root-safe mutation in a hostile tree

UPL = "upl_" + "0" * 24


def _upload_dir(root: Path, owner: str = AGENT, upl: str = UPL) -> Path:
    d = root / "staging" / owner / upl
    d.mkdir(parents=True)
    return d


def test_write_marker_beneath_creates_once(tree):
    root, _ = tree
    d = _upload_dir(root)
    assert cbf.write_marker_beneath(root, ["staging", AGENT, UPL], "verified", b"ok") is True
    assert (d / "verified").read_bytes() == b"ok"
    assert stat.S_IMODE(os.stat(d / "verified").st_mode) == 0o644
    assert cbf.write_marker_beneath(root, ["staging", AGENT, UPL], "verified", b"again") is False
    assert (d / "verified").read_bytes() == b"ok"


def test_symlinked_owner_dir_is_never_followed(tree):
    root, outside = tree
    (root / "staging").mkdir()
    (outside / UPL).mkdir()
    (root / "staging" / AGENT).symlink_to(outside)
    before = _snapshot(outside)
    assert cbf.write_marker_beneath(root, ["staging", AGENT, UPL], "verified") is False
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP) is False
    assert cbf.unlink_beneath(root, ["staging", AGENT], "sentinel") is False
    assert _snapshot(outside) == before
    assert (outside / "sentinel").read_bytes() == SENTINEL


def test_symlinked_upload_dir_is_never_followed(tree):
    root, outside = tree
    (root / "staging" / AGENT).mkdir(parents=True)
    (root / "staging" / AGENT / UPL).symlink_to(outside)
    before = _snapshot(outside)
    assert cbf.write_marker_beneath(root, ["staging", AGENT, UPL], "verified") is False
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP) is False
    assert cbf.unlink_beneath(root, ["staging", AGENT, UPL], "sentinel") is False
    assert _snapshot(outside) == before


def test_remove_tree_removes_links_not_their_targets(tree):
    root, outside = tree
    d = _upload_dir(root)
    (d / "0000000").write_bytes(b"x")
    (d / "0000001").symlink_to(outside / "sentinel")
    (d / "sub").symlink_to(outside)
    (d / "nested").mkdir()
    (d / "nested" / "deep").symlink_to(outside / "sentinel")
    (d / "nested" / "f").write_bytes(b"y")
    os.link(outside / "sentinel", d / "hard")
    before = _snapshot(outside)
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP) is True
    assert not os.path.lexists(d)
    assert (root / "staging" / AGENT).is_dir()
    assert _snapshot(outside) == before


def test_remove_tree_of_the_owner_dir_sweeps_symlinked_children(tree):
    root, outside = tree
    (root / "staging" / AGENT).mkdir(parents=True)
    (root / "staging" / AGENT / UPL).symlink_to(outside)
    before = _snapshot(outside)
    assert cbf.remove_tree_beneath(root, ["staging", AGENT], max_entries=TREE_CAP) is True
    assert not os.path.lexists(root / "staging" / AGENT)
    assert _snapshot(outside) == before


def test_remove_tree_refuses_a_deep_nest(tree):
    root, _ = tree
    d = _upload_dir(root)
    p = d
    for _ in range(cbf.MAX_TREE_DEPTH + 2):
        p = p / "n"
        p.mkdir()
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP) is False


def test_remove_tree_refuses_root_itself_and_missing(tree):
    root, _ = tree
    assert cbf.remove_tree_beneath(root, [], max_entries=TREE_CAP) is False
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP) is False
    assert root.is_dir()


def test_unlink_beneath_refuses_a_symlinked_intent(tree):
    root, outside = tree
    (root / "intents").mkdir()
    (root / "intents" / "ict.json").symlink_to(outside / "sentinel")
    assert cbf.unlink_beneath(root, ["intents"], "ict.json") is False
    assert os.path.islink(root / "intents" / "ict.json")
    assert (outside / "sentinel").read_bytes() == SENTINEL


def test_unlink_beneath_refuses_dirs_and_fifos_and_removes_files(tree, no_hang):
    root, _ = tree
    (root / "intents" / "d").mkdir(parents=True)
    os.mkfifo(root / "intents" / "f")
    (root / "intents" / "ok.json").write_text("{}")
    assert cbf.unlink_beneath(root, ["intents"], "d") is False
    assert cbf.unlink_beneath(root, ["intents"], "f") is False
    assert cbf.unlink_beneath(root, ["intents"], "missing") is False
    assert cbf.unlink_beneath(root, ["intents"], "ok.json") is True
    assert sorted(p.name for p in (root / "intents").iterdir()) == ["d", "f"]


def test_write_marker_refuses_a_dangling_symlink_marker(tree):
    root, outside = tree
    d = _upload_dir(root)
    (d / "verified").symlink_to(outside / "new")
    assert cbf.write_marker_beneath(root, ["staging", AGENT, UPL], "verified") is False
    assert not os.path.lexists(outside / "new")


@pytest.mark.parametrize("parts,name", [(["staging", ".."], "x"), (["staging/" + AGENT], "x"), (["staging"], ".."),
                                        (["staging"], "a/b"), (["staging"], "")])
def test_mutators_refuse_unsafe_components(tree, parts, name):
    root, outside = tree
    (root / "staging").mkdir()
    before = _snapshot(outside)
    assert cbf.write_marker_beneath(root, parts, name) is False
    assert cbf.unlink_beneath(root, parts, name) is False
    assert cbf.remove_tree_beneath(root, [*parts, name], max_entries=TREE_CAP) is False
    assert _snapshot(outside) == before


def _swap_after_open(monkeypatch, root: Path, outside: Path, component: str, when: str):
    """Swap ``staging/<owner>`` for a symlink to ``outside`` right before (``when='before'``) or right after
    (``when='after'``) the helper opens ``component`` — the check-then-use window of a path-based implementation."""
    real_open, real_rename, real_symlink = os.open, os.rename, os.symlink
    owner_dir = root / "staging" / AGENT
    state = {"done": False}

    def swap():
        if not state["done"]:
            state["done"] = True
            real_rename(owner_dir, root / "staging" / "moved")
            real_symlink(outside, owner_dir)

    def shim(path, flags, mode=0o777, *, dir_fd=None):
        if path == component and when == "before":
            swap()
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == component and when == "after":
            swap()
        return fd
    monkeypatch.setattr(cbf.os, "open", shim)
    return state


@pytest.mark.parametrize("when", ["before", "after"])
def test_write_marker_survives_a_dir_swapped_for_a_symlink(tree, monkeypatch, when):
    root, outside = tree
    _upload_dir(root)
    (outside / UPL).mkdir()
    before = _snapshot(outside)
    state = _swap_after_open(monkeypatch, root, outside, AGENT, when)
    ok = cbf.write_marker_beneath(root, ["staging", AGENT, UPL], "verified", b"v")
    monkeypatch.undo()
    assert state["done"]
    assert _snapshot(outside) == before
    if when == "before":
        assert ok is False
    else:  # the walk holds the real dir's fd, so the marker lands in the moved original, inside root
        assert ok is True
        assert (root / "staging" / "moved" / UPL / "verified").read_bytes() == b"v"


@pytest.mark.parametrize("when", ["before", "after"])
def test_remove_tree_survives_a_dir_swapped_for_a_symlink(tree, monkeypatch, when):
    root, outside = tree
    d = _upload_dir(root)
    (d / "0000000").write_bytes(b"x")
    (outside / UPL).mkdir()
    (outside / UPL / "0000000").write_bytes(b"keep")
    before = _snapshot(outside)
    state = _swap_after_open(monkeypatch, root, outside, AGENT, when)
    cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP)
    monkeypatch.undo()
    assert state["done"]
    assert _snapshot(outside) == before


def test_remove_tree_survives_a_child_dir_swapped_for_a_symlink(tree, monkeypatch):
    root, outside = tree
    d = _upload_dir(root)
    (d / "nested").mkdir()
    (d / "nested" / "f").write_bytes(b"y")
    before = _snapshot(outside)
    real_open, real_rename, real_symlink = os.open, os.rename, os.symlink

    def shim(path, flags, mode=0o777, *, dir_fd=None):
        if path == "nested":  # classified as a directory, then swapped before the no-follow open
            real_rename(d / "nested", root / "elsewhere")
            real_symlink(outside, d / "nested")
        return real_open(path, flags, mode, dir_fd=dir_fd)
    monkeypatch.setattr(cbf.os, "open", shim)
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP) is True
    monkeypatch.undo()
    assert not os.path.lexists(d)
    assert _snapshot(outside) == before


# ----------------------------------------------------------------------------- staging layout

def test_staging_layout():
    assert cbf.staging_parts(AGENT, UPL) == ["staging", AGENT, UPL]
    assert cbf.staging_path(Path("/r"), AGENT, UPL) == Path("/r/staging") / AGENT / UPL
    for bad_owner, bad_upl in [("..", UPL), ("a/b", UPL), (AGENT, "upl_../x"), (AGENT, "upl_" + "0" * 23)]:
        with pytest.raises(BridgeError):
            cbf.staging_parts(bad_owner, bad_upl)


def test_agent_owner_key():
    assert cbf.agent_owner_key(AGENT) == AGENT
    for bad in ("m_abc", "", "..", "a/b", "a\0", 5):
        with pytest.raises(BridgeError):
            cbf.agent_owner_key(bad)


def test_upload_id_for_is_deterministic_and_per_owner():
    sha = "a" * 64
    one = cbf.upload_id_for(AGENT, ART, "x.txt", sha, 10)
    assert one == cbf.upload_id_for(AGENT, ART, "x.txt", sha, 10)
    assert cbf.UPLOAD_RE and __import__("re").match(cbf.UPLOAD_RE, one)
    assert one != cbf.upload_id_for("agent_other", ART, "x.txt", sha, 10)
    assert one != cbf.upload_id_for(AGENT, ART, "y.txt", sha, 10)
    assert one != cbf.upload_id_for(AGENT, ART, "x.txt", sha, 11)
    expected = "upl_" + hashlib.sha256(f"{AGENT}|{ART}|x.txt|{sha}|10".encode()).hexdigest()[:24]
    assert one == expected


def test_staged_upload_meta_round_trip_and_binding():
    sha = "b" * 64
    upl = cbf.upload_id_for(AGENT, ART, "d/x.bin", sha, CHUNK_BYTES + 1)
    meta = cbf.StagedUpload(upload_id=upl, owner_key=AGENT, artifact_id=ART, name="d/x.bin",
                            size_bytes=CHUNK_BYTES + 1, file_sha256=sha, total=2, created_at=1.5)
    assert cbf.StagedUpload.from_dict(meta.to_dict(), limits=LIMITS) == meta
    forged = {**meta.to_dict(), "artifact_id": "artifact_other"}
    with pytest.raises(BridgeError):
        cbf.StagedUpload.from_dict(forged, limits=LIMITS)
    with pytest.raises(BridgeError):
        cbf.StagedUpload.from_dict({**meta.to_dict(), "total": 3}, limits=LIMITS)
    with pytest.raises(BridgeError):
        cbf.StagedUpload.from_dict({**meta.to_dict(), "extra": 1}, limits=LIMITS)


def _stage(root: Path, *, size: int, parts: list[int], verified: bool = False):
    sha = "c" * 64
    upl = cbf.upload_id_for(AGENT, ART, "x.bin", sha, size)
    d = cbf.staging_path(root, AGENT, upl)
    d.mkdir(parents=True)
    meta = cbf.StagedUpload(upload_id=upl, owner_key=AGENT, artifact_id=ART, name="x.bin", size_bytes=size,
                            file_sha256=sha, total=math.ceil(size / CHUNK_BYTES), created_at=1.0)
    cbf.atomic_write_json(d, cbf.META_NAME, meta.to_dict())
    for i in parts:
        cbf.link_new_bytes(d, f"{i:07d}", b"x")
    if verified:
        cbf.write_marker_beneath(root, cbf.staging_parts(AGENT, upl), cbf.VERIFIED_MARKER)
    return upl, d


def test_upload_complete_and_list_parts(tree):
    root, outside = tree
    upl, d = _stage(root, size=3 * CHUNK_BYTES, parts=[0, 2])
    assert cbf.list_parts(root, AGENT, upl, max_parts=3) == [0, 2]
    assert cbf.upload_complete(root, AGENT, upl, LIMITS) is False
    (d / "0000001").symlink_to(outside / "sentinel")
    assert cbf.list_parts(root, AGENT, upl, max_parts=3) == [0, 2]
    assert cbf.upload_complete(root, AGENT, upl, LIMITS) is False
    os.unlink(d / "0000001")
    cbf.link_new_bytes(d, "0000001", b"x")
    (d / "junk").write_bytes(b"")
    (d / "0000009").write_bytes(b"")  # beyond total: listed but irrelevant
    assert cbf.upload_complete(root, AGENT, upl, LIMITS) is True


def test_upload_complete_by_verified_marker_and_symlinked_dirs(tree):
    root, outside = tree
    upl, d = _stage(root, size=2 * CHUNK_BYTES, parts=[], verified=True)
    assert cbf.upload_complete(root, AGENT, upl, LIMITS) is True
    other = cbf.upload_id_for(AGENT, ART, "y.bin", "d" * 64, 1)
    (root / "staging" / AGENT / other).symlink_to(d)
    assert cbf.upload_complete(root, AGENT, other, LIMITS) is False
    assert cbf.list_parts(root, AGENT, other, max_parts=3) == []


# ----------------------------------------------------------------------------- chunks

def _chunk(**over):
    data = over.pop("raw", b"z" * 10)
    size = over.pop("size_bytes", len(data))
    args = {"artifact_id": ART, "name": "x.bin", "size_bytes": size, "file_sha256": "e" * 64,
            "total": math.ceil(size / CHUNK_BYTES), "index": 0, "data": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest()}
    args.update(over)
    return args


def test_validate_chunk_accepts_a_good_chunk():
    c = cbf.validate_chunk(_chunk(name="d/x.bin"), LIMITS)
    assert (c.index, c.total, c.data, c.name) == (0, 1, b"z" * 10, "d/x.bin")


def test_validate_chunk_full_and_last():
    full = b"f" * CHUNK_BYTES
    size = CHUNK_BYTES + 5
    assert cbf.validate_chunk(_chunk(raw=full, size_bytes=size, index=0), LIMITS).index == 0
    assert cbf.validate_chunk(_chunk(raw=b"l" * 5, size_bytes=size, index=1), LIMITS).index == 1


@pytest.mark.parametrize("over", [
    {"sha256": "0" * 64},
    {"data": "!!!!"},
    {"data": "eHh4eA"},           # missing padding
    {"data": "eHh4eB=="},          # non-canonical trailing bits
    {"data": "eHh4\neA=="},
    {"data": 5},
    {"index": 1},                  # index == total
    {"index": -1},
    {"index": True},
    {"total": 2},
    {"size_bytes": 0, "raw": b""},
    {"size_bytes": True},
    {"name": "../x"},
    {"name": "/abs"},
    {"file_sha256": "E" * 64},
    {"extra": 1},
    {"artifact_id": "../a"},
])
def test_validate_chunk_refuses(over):
    with pytest.raises(BridgeError):
        cbf.validate_chunk(_chunk(**over), LIMITS)


def test_validate_chunk_refuses_wrong_lengths_and_oversize():
    size = 2 * CHUNK_BYTES
    with pytest.raises(BridgeError):  # a non-last chunk one byte short
        cbf.validate_chunk(_chunk(raw=b"a" * (CHUNK_BYTES - 1), size_bytes=size, index=0), LIMITS)
    with pytest.raises(BridgeError):  # the last chunk with a wrong remainder
        cbf.validate_chunk(_chunk(raw=b"a" * 3, size_bytes=CHUNK_BYTES + 5, index=1), LIMITS)
    small = BridgeLimits.from_mapping({"max_file_bytes": 5})
    with pytest.raises(BridgeError):
        cbf.validate_chunk(_chunk(raw=b"a" * 6), small)
    with pytest.raises(BridgeError):  # missing key
        args = _chunk()
        del args["sha256"]
        cbf.validate_chunk(args, LIMITS)


# ----------------------------------------------------------------------------- limits

def test_limit_defaults_equal_the_configuration_table():
    assert BridgeLimits().to_dict() == {
        "daily_bytes_per_agent": 100_000_000_000, "commits_per_minute": 30, "chunks_per_minute": 1200,
        "max_changes_per_commit": 100, "max_pending_intents_per_agent": 4, "max_file_bytes": 100_000_000_000,
        "staging_ttl_seconds": 86400, "staging_bytes_per_agent": 200_000_000_000,
        "staging_bytes_total": 2_000_000_000_000, "max_text_merge_bytes": 2_097_152,
        "max_conflict_text_bytes": 8_388_608, "mirror_history_versions": 5, "mirror_max_manifests": 100000,
        "mirror_max_file_bytes": 1_073_741_824, "mirror_max_artifact_bytes": 17_179_869_184, "apply_concurrency": 4,
            "audit_coalesce_seconds": 60,
    }
    assert BridgeLimits.from_mapping({}) == BridgeLimits()
    assert BridgeLimits.from_mapping(BridgeLimits().to_dict()) == BridgeLimits()
    assert BridgeLimits.from_mapping({"commits_per_minute": 7}).commits_per_minute == 7


@pytest.mark.parametrize("bad", [{"daily_bytes_per_agent": 0}, {"commits_per_minute": -1}, {"apply_concurrency": True},
                                 {"nope": 1}, {"max_file_bytes": 1.5}, {"max_file_bytes": "5"}, {"enabled": True}])
def test_limits_refuse_bad_values(bad):
    with pytest.raises(BridgeError):
        BridgeLimits.from_mapping(bad)


def test_protocol_constants():
    assert (cbf.CHUNK_BYTES, cbf.MCP_UPLOAD_MAX_BODY, cbf.MAX_MESSAGE) == (524_288, 786_432, 1000)
    assert (cbf.INTENT_MAX_AGE, cbf.FEED_MAX_AGE, cbf.MAX_CHUNK_INDEX) == (60.0, 120.0, 9_999_999)
    assert cbf.TICKET_RE == r"ict_[0-9a-f]{24}\Z"
    assert cbf.UPLOAD_RE == r"upl_[0-9a-f]{24}\Z"
    assert cbf.SHA_RE == r"[0-9a-f]{64}\Z"
    assert __import__("re").match(cbf.TICKET_RE, cbf.new_ticket())


# ----------------------------------------------------------------------------- intents and changes

def _intent(**over):
    ticket = over.pop("ticket", "ict_" + "1" * 24)
    raw = {"ticket": ticket, "kind": "commit", "actor": {"kind": "agent", "id": AGENT}, "artifact_id": ART,
           "idempotency_key": "k-1", "submitted_at": 1000.0,
           "payload": {"base_version_id": VER, "message": "m",
                       "changes": [{"name": "a.txt", "op": "put", "text": "hi"}]}}
    raw.update(over)
    return raw


def test_validate_intent_commit():
    it = cbf.validate_intent(_intent(), filename="ict_" + "1" * 24 + ".json", limits=LIMITS)
    assert it.kind == "commit" and it.actor == cbf.Actor("agent", AGENT) and it.artifact_id == ART
    assert isinstance(it.payload, cbf.CommitPayload)
    assert it.payload.changes == (cbf.Change("a.txt", "put", "hi", None),)
    assert it.payload.inline_bytes == 2
    assert it.request_sha256 == cbf.request_digest(_intent())


@pytest.mark.parametrize("raw,filename", [
    (_intent(), "ict_" + "2" * 24 + ".json"),
    (_intent(ticket="ict_x"), "ict_x.json"),
    (_intent(kind="share_create"), "ict_" + "1" * 24 + ".json"),
    (_intent(extra=1), "ict_" + "1" * 24 + ".json"),
    (_intent(actor={"kind": "user", "id": "user_1"}), "ict_" + "1" * 24 + ".json"),
    (_intent(actor={"kind": "agent", "id": "m_1"}), "ict_" + "1" * 24 + ".json"),
    (_intent(actor={"kind": "robot", "id": "x"}), "ict_" + "1" * 24 + ".json"),
    (_intent(actor={"kind": "agent", "id": AGENT, "x": 1}), "ict_" + "1" * 24 + ".json"),
    (_intent(artifact_id=None), "ict_" + "1" * 24 + ".json"),
    (_intent(submitted_at=True), "ict_" + "1" * 24 + ".json"),
    (_intent(submitted_at=float("nan")), "ict_" + "1" * 24 + ".json"),
    (_intent(idempotency_key="a b"), "ict_" + "1" * 24 + ".json"),
    (_intent(payload={"base_version_id": VER, "changes": [{"name": "a", "op": "delete"}], "x": 1}),
     "ict_" + "1" * 24 + ".json"),
    (_intent(payload={"base_version_id": VER, "message": "m" * 1001,
                      "changes": [{"name": "a", "op": "delete"}]}), "ict_" + "1" * 24 + ".json"),
    ([], "ict_" + "1" * 24 + ".json"),
])
def test_validate_intent_refuses(raw, filename):
    with pytest.raises(IntentError):
        cbf.validate_intent(raw, filename=filename, limits=LIMITS)


def test_unknown_kind_and_wrong_actor_share_a_reason():
    with pytest.raises(IntentError) as one:
        cbf.validate_intent(_intent(kind="share_create"), filename="ict_" + "1" * 24 + ".json", limits=LIMITS)
    with pytest.raises(IntentError) as two:
        cbf.validate_intent(_intent(actor={"kind": "user", "id": "user_1"}),
                            filename="ict_" + "1" * 24 + ".json", limits=LIMITS)
    assert one.value.reason == two.value.reason == "unknown intent kind"


def test_registered_kind_uses_its_own_validator(registry):
    seen = []

    def validate(payload, limits):
        seen.append(payload)
        if payload.get("ok") is not True:
            raise IntentError("bad x.test payload")
        return ("validated", payload["ok"])
    cbf.register_intent_kind("x.test", validate, actors=frozenset({"user"}))
    raw = _intent(kind="x.test", actor={"kind": "user", "id": "user_1"}, artifact_id=None, payload={"ok": True})
    it = cbf.validate_intent(raw, filename="ict_" + "1" * 24 + ".json", limits=LIMITS)
    assert it.payload == ("validated", True) and it.artifact_id is None
    with pytest.raises(IntentError):
        cbf.validate_intent({**raw, "payload": {"ok": False}}, filename="ict_" + "1" * 24 + ".json", limits=LIMITS)
    with pytest.raises(IntentError):
        cbf.validate_intent({**raw, "actor": {"kind": "agent", "id": AGENT}}, filename="ict_" + "1" * 24 + ".json",
                            limits=LIMITS)
    with pytest.raises(ValueError):
        cbf.register_intent_kind("x.test", validate, actors=frozenset({"user"}))
    for bad_kind, actors in [("X", {"user"}), ("1a", {"user"}), ("a" * 49, {"user"}), ("ok", set()),
                             ("ok", {"robot"})]:
        with pytest.raises(ValueError):
            cbf.register_intent_kind(bad_kind, validate, actors=frozenset(actors))


def test_only_commit_is_registered_by_plan_3():
    assert set(cbf._INTENT_KINDS) == {"commit"}


def test_request_digest_ignores_ticket_and_time():
    a = _intent()
    b = _intent(ticket="ict_" + "f" * 24, submitted_at=2000.0)
    assert cbf.request_digest(a) == cbf.request_digest(b)
    c = _intent(payload={**a["payload"], "changes": [{"name": "a.txt", "op": "put", "text": "ho"}]})
    assert cbf.request_digest(a) != cbf.request_digest(c)
    expected = hashlib.sha256(__import__("json").dumps(
        {"kind": a["kind"], "actor": a["actor"], "artifact_id": a["artifact_id"], "payload": a["payload"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert cbf.request_digest(a) == expected


@pytest.mark.parametrize("changes", [
    [{"name": "../x", "op": "delete"}],
    [{"name": "/abs", "op": "delete"}],
    [{"name": "a\0b", "op": "delete"}],
    [{"name": "a\rb", "op": "delete"}],
    [{"name": "a", "op": "delete"}, {"name": "a/b", "op": "put", "text": ""}],
    [{"name": "a", "op": "delete"}, {"name": "a", "op": "delete"}],
    [{"name": "a", "op": "put", "text": "x", "storage_key": "k"}],
    [{"name": "a", "op": "put", "text": "x", "sha256": "0" * 64}],
    [{"name": "a", "op": "put", "text": "x", "size_bytes": 1}],
    [{"name": "a", "op": "put", "text": "x", "media_type": "text/plain"}],
    [{"name": "a", "op": "put", "text": "x", "entry": {}}],
    [{"name": "a", "op": "put", "text": "x", "upload_id": UPL}],
    [{"name": "a", "op": "put"}],
    [{"name": "a", "op": "put", "upload_id": "upl_../x"}],
    [{"name": "a", "op": "delete", "text": "x"}],
    [{"name": "a", "op": "move"}],
    [{"name": "a", "op": "put", "text": "\x01"}],
    [{"name": "a", "op": "put", "text": 5}],
    [{"name": "a", "op": "put", "text": None}],
    [{"name": "a", "op": "put", "text": "\ud800"}],
    [{"op": "delete"}],
    ["a"],
    [],
    "a",
])
def test_validate_changes_refuses(changes):
    with pytest.raises(IntentError):
        cbf.validate_changes(changes, LIMITS)


def test_validate_changes_accepts_and_bounds():
    out = cbf.validate_changes([{"name": "a", "op": "put", "text": "x\ty\nz\r"},
                                {"name": "d/b.bin", "op": "put", "upload_id": UPL},
                                {"name": "c", "op": "delete"}], LIMITS)
    assert out == (cbf.Change("a", "put", "x\ty\nz\r", None), cbf.Change("d/b.bin", "put", None, UPL),
                   cbf.Change("c", "delete", None, None))
    n = LIMITS.max_changes_per_commit
    many = [{"name": f"f{i}", "op": "delete"} for i in range(n)]
    assert len(cbf.validate_changes(many, LIMITS)) == n
    with pytest.raises(IntentError):
        cbf.validate_changes(many + [{"name": "extra", "op": "delete"}], LIMITS)


def test_change_names_are_nfc_normalised():
    out = cbf.validate_changes([{"name": "é", "op": "delete"}], LIMITS)
    assert out[0].name == "é"


def test_error_reasons_never_echo_names_or_text():
    secret = "SECRET-TOKEN-xyz"
    for changes in ([{"name": "../" + secret, "op": "delete"}], [{"name": "a", "op": "put", "text": secret + "\x01"}]):
        with pytest.raises(IntentError) as err:
            cbf.validate_changes(changes, LIMITS)
        assert secret not in str(err.value)


# ----------------------------------------------------------------------------- review fixes

def test_read_cap_holds_when_fstat_under_reports(tree, monkeypatch):
    root, _ = tree
    (root / "p").write_bytes(b"x" * 11)
    real_fstat = os.fstat

    class Small:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return 5 if name == "st_size" else getattr(self._st, name)
    monkeypatch.setattr(cbf.os, "fstat", lambda fd: Small(real_fstat(fd)))
    assert cbf.read_bytes_beneath(root, ["p"], max_bytes=10) is None
    assert cbf.read_bytes_beneath(root, ["p"], max_bytes=11) == b"x" * 11


def test_open_beneath_cap_holds_when_the_file_grows(tree):
    root, _ = tree
    (root / "p").write_bytes(b"y" * 10)
    fh = cbf.open_beneath(root, ["p"], max_bytes=10)
    with open(root / "p", "ab") as grow:
        grow.write(b"z" * 100)
    with fh, pytest.raises(BridgeError):
        fh.read()
    with cbf.open_beneath(root, ["p"], max_bytes=110) as ok:
        assert ok.read() == b"y" * 10 + b"z" * 100
    assert cbf.open_beneath(root, ["p"], max_bytes=10) is None


def test_open_beneath_bounded_reads_in_pieces(tree):
    root, _ = tree
    (root / "p").write_bytes(b"0123456789")
    with cbf.open_beneath(root, ["p"], max_bytes=10) as fh:
        assert fh.read(4) == b"0123"
        assert fh.read() == b"456789"
        assert fh.read() == b""


def test_list_parts_leaves_out_a_hardlinked_part(tree):
    root, outside = tree
    upl, d = _stage(root, size=2 * CHUNK_BYTES, parts=[0])
    os.link(outside / "sentinel", d / "0000001")
    assert cbf.list_parts(root, AGENT, upl, max_parts=2) == [0]
    assert cbf.upload_complete(root, AGENT, upl, LIMITS) is False


def test_remove_tree_removes_a_dangling_link_without_following(tree):
    root, outside = tree
    d = _upload_dir(root)
    (d / "0000000").symlink_to(outside / "new")
    (d / "dirlink").symlink_to(outside)
    before = _snapshot(outside)
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=TREE_CAP) is True
    assert not os.path.lexists(d)
    assert not os.path.lexists(outside / "new")
    assert _snapshot(outside) == before


@pytest.mark.skipif(not hasattr(os, "O_TMPFILE"), reason="O_TMPFILE is Linux-only; other platforms use the named-temp fallback")
def test_link_new_bytes_uses_o_tmpfile_and_never_leaves_a_tmp(tree, monkeypatch):
    root, _ = tree
    calls = []
    real_link = os.link

    def crash_after_link(src, dst, **kw):
        calls.append(src)
        real_link(src, dst, **kw)
        raise KeyboardInterrupt("simulated crash right after the link")
    monkeypatch.setattr(cbf.os, "link", crash_after_link)
    with pytest.raises(KeyboardInterrupt):
        cbf.link_new_bytes(root, "0000000", b"part")
    monkeypatch.undo()
    assert calls and calls[0].startswith("/proc/self/fd/")
    assert sorted(p.name for p in root.iterdir()) == ["0000000"]
    assert os.stat(root / "0000000").st_nlink == 1
    assert stat.S_IMODE(os.stat(root / "0000000").st_mode) == 0o600
    assert cbf.read_bytes_beneath(root, ["0000000"], max_bytes=10) == b"part"
    assert cbf.link_new_bytes(root, "0000000", b"other") is False
    assert (root / "0000000").read_bytes() == b"part"


@pytest.mark.parametrize("how", ["no_tmpfile", "no_proc"])
def test_link_new_bytes_falls_back(tree, monkeypatch, how):
    root, _ = tree
    if how == "no_tmpfile":
        def unsupported(dfd):
            raise OSError(errno.EOPNOTSUPP, "no O_TMPFILE")
        monkeypatch.setattr(cbf, "_open_tmpfile", unsupported)
    else:
        real_link = os.link

        def no_proc(src, dst, **kw):
            if str(src).startswith("/proc/"):
                raise FileNotFoundError(errno.ENOENT, "no /proc")
            return real_link(src, dst, **kw)
        monkeypatch.setattr(cbf.os, "link", no_proc)
    assert cbf.link_new_bytes(root, "0000000", b"part") is True
    assert cbf.link_new_bytes(root, "0000000", b"other") is False
    monkeypatch.undo()
    assert sorted(p.name for p in root.iterdir()) == ["0000000"]
    assert (root / "0000000").read_bytes() == b"part"
    assert os.stat(root / "0000000").st_nlink == 1


def test_stale_fallback_tmp_is_ignored_and_swept(tree):
    root, _ = tree
    upl, d = _stage(root, size=CHUNK_BYTES + 1, parts=[0])
    (d / ".tmp-deadbeef00000000").write_bytes(b"partial")
    assert cbf.list_parts(root, AGENT, upl, max_parts=2) == [0]
    assert cbf.remove_tree_beneath(root, cbf.staging_parts(AGENT, upl), max_entries=TREE_CAP) is True
    assert not os.path.lexists(d)


def test_list_beneath(tree, no_hang):
    root, outside = tree
    d = root / "intents"
    d.mkdir()
    (d / "b.json").write_text("{}")
    (d / "a.json").symlink_to(outside / "sentinel")
    os.mkfifo(d / "f")
    (d / "sub").mkdir()
    entries = cbf.list_beneath(root, ["intents"], max_entries=10)
    assert [name for name, _ in entries] == ["a.json", "b.json", "f", "sub"]
    kinds = {name: stat.S_IFMT(st.st_mode) for name, st in entries}
    assert kinds == {"a.json": stat.S_IFLNK, "b.json": stat.S_IFREG, "f": stat.S_IFIFO, "sub": stat.S_IFDIR}
    with pytest.raises(BridgeError):
        cbf.list_beneath(root, ["intents"], max_entries=3)
    (root / "linked").symlink_to(d)
    assert cbf.list_beneath(root, ["linked"], max_entries=10) is None
    assert cbf.list_beneath(root, ["missing"], max_entries=10) is None
    assert cbf.list_beneath(root, [".."], max_entries=10) is None
    assert cbf.list_beneath(root, ["intents", "b.json"], max_entries=10) is None


def test_list_parts_fails_closed_past_its_bound(tree):
    root, _ = tree
    upl, d = _stage(root, size=3 * CHUNK_BYTES, parts=[0, 1, 2])
    for i in range(cbf.ENTRY_SLACK):  # meta + 3 parts + ENTRY_SLACK empty files > 3 + ENTRY_SLACK
        (d / f"junk{i}").write_bytes(b"")
    with pytest.raises(BridgeError):
        cbf.list_parts(root, AGENT, upl, max_parts=3)
    assert cbf.upload_complete(root, AGENT, upl, LIMITS) is False
    os.unlink(d / "junk0")  # exactly at the bound: still listed
    assert cbf.list_parts(root, AGENT, upl, max_parts=3) == [0, 1, 2]
    os.unlink(d / "junk1")
    assert cbf.list_parts(root, AGENT, upl, max_parts=2) == [0, 1]  # indices past the bound are never kept
    with pytest.raises(BridgeError):
        cbf.list_parts(root, AGENT, upl, max_parts=-1)


def test_a_max_size_upload_part_count_still_works(tree):
    root, _ = tree
    limits = BridgeLimits.from_mapping({"max_file_bytes": 3 * CHUNK_BYTES})
    assert cbf.max_parts_for(limits) == 3
    assert cbf.tree_entry_cap(limits) == 3 + cbf.ENTRY_SLACK
    upl, d = _stage(root, size=3 * CHUNK_BYTES, parts=[0, 1, 2], verified=True)
    cbf.link_new_bytes(d, cbf.AUDITED_MARKER, b"")
    assert cbf.list_parts(root, AGENT, upl, max_parts=cbf.max_parts_for(limits)) == [0, 1, 2]
    assert cbf.remove_tree_beneath(root, cbf.staging_parts(AGENT, upl), max_entries=cbf.tree_entry_cap(limits))
    assert cbf.max_parts_for(BridgeLimits()) == math.ceil(100_000_000_000 / CHUNK_BYTES)


def test_remove_tree_fails_closed_past_its_entry_budget(tree):
    root, _ = tree
    upl, d = _stage(root, size=3 * CHUNK_BYTES, parts=[0, 1, 2])  # 4 entries
    (d / "nested").mkdir()
    (d / "nested" / "f").write_bytes(b"")  # 6 entries in all
    assert cbf.remove_tree_beneath(root, cbf.staging_parts(AGENT, upl), max_entries=5) is False
    assert d.is_dir()
    assert cbf.remove_tree_beneath(root, cbf.staging_parts(AGENT, upl), max_entries=6) is True
    assert not os.path.lexists(d)


def test_empty_dir_returns_false_past_its_budget(tree):
    root, _ = tree
    for i in range(3):
        (root / f"f{i}").write_bytes(b"")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert cbf._empty_dir(fd, 1, [2]) is False
        assert len(list(root.iterdir())) == 3  # nothing removed once over budget
        budget = [3]
        assert cbf._empty_dir(fd, 1, budget) is True
        assert budget == [0]
    finally:
        os.close(fd)


def test_link_new_bytes_applies_mode_on_the_tmpfile_path(tree, monkeypatch):
    root, _ = tree
    used = []
    real = cbf._open_tmpfile
    monkeypatch.setattr(cbf, "_open_tmpfile", lambda dfd: used.append(1) or real(dfd))
    assert cbf.link_new_bytes(root, "m", b"x", mode=0o640) is True
    assert used
    assert stat.S_IMODE(os.stat(root / "m").st_mode) == 0o640


@pytest.mark.skipif(not hasattr(os, "O_TMPFILE"), reason="O_TMPFILE is Linux-only; other platforms use the named-temp fallback")
def test_link_new_bytes_propagates_a_tmpfile_permission_error(tree, monkeypatch):
    root, _ = tree
    real_open = os.open

    def deny(path, flags, mode=0o777, *, dir_fd=None):
        if flags & getattr(os, "O_TMPFILE", 0) == getattr(os, "O_TMPFILE", -1):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, flags, mode, dir_fd=dir_fd)
    monkeypatch.setattr(cbf.os, "open", deny)
    with pytest.raises(PermissionError):
        cbf.link_new_bytes(root, "p", b"x")
    monkeypatch.undo()
    assert list(root.iterdir()) == []


def test_link_new_bytes_falls_back_without_o_tmpfile_constant(tree, monkeypatch):
    root, _ = tree
    monkeypatch.delattr(cbf.os, "O_TMPFILE", raising=False)
    assert cbf.link_new_bytes(root, "p", b"x") is True
    monkeypatch.undo()
    assert sorted(p.name for p in root.iterdir()) == ["p"]


def _hostile_meta(root: Path, size: int) -> str:
    """A self-consistent meta.json (upload id and total derive from its fields) declaring ``size`` bytes."""
    sha = "9" * 64
    upl = cbf.upload_id_for(AGENT, ART, "big.bin", sha, size)
    d = cbf.staging_path(root, AGENT, upl)
    d.mkdir(parents=True)
    cbf.atomic_write_json(d, cbf.META_NAME, {"upload_id": upl, "owner_key": AGENT, "artifact_id": ART,
                                             "name": "big.bin", "size_bytes": size, "file_sha256": sha,
                                             "total": math.ceil(size / CHUNK_BYTES), "created_at": 1.0})
    return upl


def test_hostile_meta_never_sizes_the_part_scan(tree, monkeypatch):
    root, _ = tree
    upl = _hostile_meta(root, 10**18)  # total ~1.9e12: would scan without bound if trusted
    monkeypatch.setattr(cbf, "list_parts", lambda *a, **k: pytest.fail("scanned a hostile upload dir"))
    assert cbf.read_staged_meta(root, AGENT, upl, LIMITS) is None
    assert cbf.upload_complete(root, AGENT, upl, LIMITS) is False
    raw = cbf.read_json_beneath(root, [*cbf.staging_parts(AGENT, upl), cbf.META_NAME], max_bytes=4096)
    with pytest.raises(BridgeError):
        cbf.StagedUpload.from_dict(raw, limits=LIMITS)


def test_meta_just_over_max_file_bytes_is_refused(tree):
    root, _ = tree
    limits = BridgeLimits.from_mapping({"max_file_bytes": 2 * CHUNK_BYTES - 1})  # same part count as 2 * CHUNK_BYTES
    upl = _hostile_meta(root, 2 * CHUNK_BYTES)
    assert cbf.read_staged_meta(root, AGENT, upl, limits) is None
    assert cbf.read_staged_meta(root, AGENT, upl, BridgeLimits.from_mapping({"max_file_bytes": 2 * CHUNK_BYTES}))


def test_a_max_size_upload_completes(tree):
    root, _ = tree
    limits = BridgeLimits.from_mapping({"max_file_bytes": 3 * CHUNK_BYTES})
    upl, _ = _stage(root, size=3 * CHUNK_BYTES, parts=[0, 1, 2])
    assert cbf.upload_complete(root, AGENT, upl, limits) is True
    assert cbf.upload_complete(root, AGENT, upl, BridgeLimits.from_mapping({"max_file_bytes": 3 * CHUNK_BYTES - 1}))\
        is False


def test_remove_tree_requires_a_valid_entry_bound(tree):
    root, _ = tree
    _upload_dir(root)
    with pytest.raises(TypeError):
        cbf.remove_tree_beneath(root, ["staging", AGENT, UPL])
    for bad in (True, -1, "5", None):
        assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL], max_entries=bad) is False
    assert cbf.staging_path(root, AGENT, UPL).is_dir()


# ----------------------------------------------------------------------------- stream_new_beneath (mirror blobs)

def _chunks(*parts):
    yield from parts


def test_stream_new_beneath_writes_creates_dirs_with_modes(tmp_path):
    root = tmp_path / "m"
    root.mkdir()
    old = os.umask(0o077)
    try:
        assert cbf.stream_new_beneath(root, ["a", "b"], "f", _chunks(b"he", b"llo"), max_bytes=10,
                                      accept=cbf.utf8_accept()) is True
    finally:
        os.umask(old)
    assert (root / "a" / "b" / "f").read_bytes() == b"hello"
    assert stat.S_IMODE(os.stat(root / "a").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(root / "a" / "b").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(root / "a" / "b" / "f").st_mode) == 0o644
    assert os.listdir(root / "a" / "b") == ["f"]
    assert cbf.file_exists_beneath(root, ["a", "b"], "f")
    assert not cbf.file_exists_beneath(root, ["a", "b"], "g")


def test_stream_new_beneath_never_replaces_and_does_not_consume(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"old")
    consumed = []

    def gen():
        consumed.append(1)
        yield b"new"
    assert cbf.stream_new_beneath(tmp_path, ["d"], "f", gen(), max_bytes=10) is True
    assert (tmp_path / "d" / "f").read_bytes() == b"old" and consumed == []


def test_stream_new_beneath_lost_race_keeps_existing(tmp_path, monkeypatch):
    (tmp_path / "d").mkdir()

    def gen():
        (tmp_path / "d" / "f").write_bytes(b"winner")      # another writer lands first
        yield b"loser"
    assert cbf.stream_new_beneath(tmp_path, ["d"], "f", gen(), max_bytes=10) is True
    assert (tmp_path / "d" / "f").read_bytes() == b"winner"
    assert os.listdir(tmp_path / "d") == ["f"]


@pytest.mark.parametrize("tmpfile", [True, False])
def test_stream_new_beneath_rejects_leave_nothing(tmp_path, monkeypatch, tmpfile):
    if not tmpfile:
        monkeypatch.setattr(cbf, "_open_tmpfile", lambda dfd: (_ for _ in ()).throw(
            OSError(errno.EOPNOTSUPP, "no")))
    (tmp_path / "d").mkdir()
    assert cbf.stream_new_beneath(tmp_path, ["d"], "bad", _chunks(b"caf\xe9"), max_bytes=10,
                                  accept=cbf.utf8_accept()) is False
    assert cbf.stream_new_beneath(tmp_path, ["d"], "cut", _chunks(b"\xc3"), max_bytes=10,
                                  accept=cbf.utf8_accept()) is False   # truncated multibyte at the end
    assert cbf.stream_new_beneath(tmp_path, ["d"], "big", _chunks(b"12345", b"67"), max_bytes=6) is False

    def crash():
        yield b"part"
        raise RuntimeError("transport died")
    with pytest.raises(RuntimeError):
        cbf.stream_new_beneath(tmp_path, ["d"], "crash", crash(), max_bytes=10)
    assert os.listdir(tmp_path / "d") == []


def test_stream_new_beneath_fallback_tmp_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(cbf, "_open_tmpfile", lambda dfd: (_ for _ in ()).throw(OSError(errno.EOPNOTSUPP, "no")))
    (tmp_path / "d").mkdir()
    modes = []

    def gen():
        for name in os.listdir(tmp_path / "d"):
            modes.append(stat.S_IMODE(os.lstat(tmp_path / "d" / name).st_mode))
        yield b"x"
    old = os.umask(0)
    try:
        assert cbf.stream_new_beneath(tmp_path, ["d"], "f", gen(), max_bytes=10) is True
    finally:
        os.umask(old)
    assert modes == [0o600]
    assert os.listdir(tmp_path / "d") == ["f"]
    assert stat.S_IMODE(os.stat(tmp_path / "d" / "f").st_mode) == 0o644


def test_stream_new_beneath_never_follows_a_planted_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "m"
    root.mkdir()
    (root / "d").symlink_to(outside)
    with pytest.raises(OSError):
        cbf.stream_new_beneath(root, ["d"], "f", _chunks(b"x"), max_bytes=10)
    (root / "e").mkdir()
    (root / "e" / "f").symlink_to(outside / "target")
    assert not cbf.file_exists_beneath(root, ["e"], "f")
    with pytest.raises(OSError):
        cbf.stream_new_beneath(root, ["e"], "f", _chunks(b"x"), max_bytes=10)
    assert list(outside.iterdir()) == []
    for bad in (["..", "x"], ["a/b"], [""]):
        with pytest.raises(BridgeError):
            cbf.stream_new_beneath(root, bad, "f", _chunks(b"x"), max_bytes=10)
    with pytest.raises(BridgeError):
        cbf.stream_new_beneath(root, ["e"], "../f", _chunks(b"x"), max_bytes=10)


def test_stream_new_beneath_completes_without_proc(tmp_path, monkeypatch):
    real_link = os.link

    def no_proc(src, dst, *a, **kw):
        if isinstance(src, str) and src.startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "no /proc")
        return real_link(src, dst, *a, **kw)
    monkeypatch.setattr(cbf.os, "link", no_proc)
    assert cbf.stream_new_beneath(tmp_path, ["x"], "f", _chunks(b"hel", b"lo"), max_bytes=10) is True
    assert (tmp_path / "x" / "f").read_bytes() == b"hello"
    assert os.listdir(tmp_path / "x") == ["f"]
    assert stat.S_IMODE(os.stat(tmp_path / "x" / "f").st_mode) == 0o644
    assert cbf.link_new_bytes(tmp_path / "x", "g", b"bytes") is True
    assert (tmp_path / "x" / "g").read_bytes() == b"bytes"


def test_file_exists_beneath_never_follows_a_symlink_to_an_existing_file(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"real file")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").symlink_to(outside)
    assert not cbf.file_exists_beneath(tmp_path, ["d"], "f")
    (tmp_path / "e").symlink_to(tmp_path / "d")
    (tmp_path / "d" / "g").write_bytes(b"x")
    assert cbf.file_exists_beneath(tmp_path, ["d"], "g")
    assert not cbf.file_exists_beneath(tmp_path, ["e"], "g")


def test_dir_helpers_repair_the_mode_of_existing_dirs(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    os.chmod(tmp_path / "a", 0o700)
    os.chmod(tmp_path / "a" / "b", 0o777)
    cbf.make_dirs_beneath(tmp_path, ["a", "b"])                 # default: existing modes are left alone
    assert stat.S_IMODE(os.stat(tmp_path / "a").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "a" / "b").st_mode) == 0o777
    cbf.make_dirs_beneath(tmp_path, ["a", "b"], public_mode=True)
    assert stat.S_IMODE(os.stat(tmp_path / "a").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(tmp_path / "a" / "b").st_mode) == 0o755
    (tmp_path / "c").mkdir()
    os.chmod(tmp_path / "c", 0o700)
    old = os.umask(0o077)
    try:
        cbf.ensure_dir(tmp_path / "c" / "d" / "e")
    finally:
        os.umask(old)
    for d in ("c/d", "c/d/e"):
        assert stat.S_IMODE(os.stat(tmp_path / d).st_mode) == 0o755, d
    assert stat.S_IMODE(os.stat(tmp_path / "c").st_mode) == 0o700     # an existing parent is never widened
    cbf.ensure_dir(tmp_path / "c")
    assert stat.S_IMODE(os.stat(tmp_path / "c").st_mode) == 0o700
    cbf.atomic_write_bytes(tmp_path / "c", "f", b"x")
    cbf.link_new_bytes(tmp_path / "c", "g", b"x")
    assert stat.S_IMODE(os.stat(tmp_path / "c").st_mode) == 0o700
    cbf.ensure_dir(tmp_path / "c", public_mode=True)
    assert stat.S_IMODE(os.stat(tmp_path / "c").st_mode) == 0o755


def test_stream_new_beneath_never_counts_a_symlink_to_an_existing_file_as_present(tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"real file")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").symlink_to(outside)
    with pytest.raises(FileExistsError):
        cbf.stream_new_beneath(tmp_path, ["d"], "f", _chunks(b"x"), max_bytes=10)
    assert outside.read_bytes() == b"real file"
