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
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL]) is False
    assert cbf.unlink_beneath(root, ["staging", AGENT], "sentinel") is False
    assert _snapshot(outside) == before
    assert (outside / "sentinel").read_bytes() == SENTINEL


def test_symlinked_upload_dir_is_never_followed(tree):
    root, outside = tree
    (root / "staging" / AGENT).mkdir(parents=True)
    (root / "staging" / AGENT / UPL).symlink_to(outside)
    before = _snapshot(outside)
    assert cbf.write_marker_beneath(root, ["staging", AGENT, UPL], "verified") is False
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL]) is False
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
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL]) is True
    assert not os.path.lexists(d)
    assert (root / "staging" / AGENT).is_dir()
    assert _snapshot(outside) == before


def test_remove_tree_of_the_owner_dir_sweeps_symlinked_children(tree):
    root, outside = tree
    (root / "staging" / AGENT).mkdir(parents=True)
    (root / "staging" / AGENT / UPL).symlink_to(outside)
    before = _snapshot(outside)
    assert cbf.remove_tree_beneath(root, ["staging", AGENT]) is True
    assert not os.path.lexists(root / "staging" / AGENT)
    assert _snapshot(outside) == before


def test_remove_tree_refuses_a_deep_nest(tree):
    root, _ = tree
    d = _upload_dir(root)
    p = d
    for _ in range(cbf.MAX_TREE_DEPTH + 2):
        p = p / "n"
        p.mkdir()
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL]) is False


def test_remove_tree_refuses_root_itself_and_missing(tree):
    root, _ = tree
    assert cbf.remove_tree_beneath(root, []) is False
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL]) is False
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
    assert cbf.remove_tree_beneath(root, [*parts, name]) is False
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
    cbf.remove_tree_beneath(root, ["staging", AGENT, UPL])
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
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL]) is True
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
    assert cbf.StagedUpload.from_dict(meta.to_dict()) == meta
    forged = {**meta.to_dict(), "artifact_id": "artifact_other"}
    with pytest.raises(BridgeError):
        cbf.StagedUpload.from_dict(forged)
    with pytest.raises(BridgeError):
        cbf.StagedUpload.from_dict({**meta.to_dict(), "total": 3})
    with pytest.raises(BridgeError):
        cbf.StagedUpload.from_dict({**meta.to_dict(), "extra": 1})


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
    assert cbf.list_parts(root, AGENT, upl) == [0, 2]
    assert cbf.upload_complete(root, AGENT, upl) is False
    (d / "0000001").symlink_to(outside / "sentinel")
    assert cbf.list_parts(root, AGENT, upl) == [0, 2]
    assert cbf.upload_complete(root, AGENT, upl) is False
    os.unlink(d / "0000001")
    cbf.link_new_bytes(d, "0000001", b"x")
    (d / "junk").write_bytes(b"")
    (d / "0000009").write_bytes(b"")  # beyond total: listed but irrelevant
    assert cbf.upload_complete(root, AGENT, upl) is True


def test_upload_complete_by_verified_marker_and_symlinked_dirs(tree):
    root, outside = tree
    upl, d = _stage(root, size=2 * CHUNK_BYTES, parts=[], verified=True)
    assert cbf.upload_complete(root, AGENT, upl) is True
    other = cbf.upload_id_for(AGENT, ART, "y.bin", "d" * 64, 1)
    (root / "staging" / AGENT / other).symlink_to(d)
    assert cbf.upload_complete(root, AGENT, other) is False
    assert cbf.list_parts(root, AGENT, other) == []


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
    assert cbf.list_parts(root, AGENT, upl) == [0]
    assert cbf.upload_complete(root, AGENT, upl) is False


def test_remove_tree_removes_a_dangling_link_without_following(tree):
    root, outside = tree
    d = _upload_dir(root)
    (d / "0000000").symlink_to(outside / "new")
    (d / "dirlink").symlink_to(outside)
    before = _snapshot(outside)
    assert cbf.remove_tree_beneath(root, ["staging", AGENT, UPL]) is True
    assert not os.path.lexists(d)
    assert not os.path.lexists(outside / "new")
    assert _snapshot(outside) == before


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
    assert cbf.list_parts(root, AGENT, upl) == [0]
    assert cbf.remove_tree_beneath(root, cbf.staging_parts(AGENT, upl)) is True
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


def test_directory_entry_caps(tree, monkeypatch):
    root, _ = tree
    upl, d = _stage(root, size=3 * CHUNK_BYTES, parts=[0, 1, 2])
    monkeypatch.setattr(cbf, "MAX_DIR_ENTRIES", 3)  # meta.json + three parts = 4 entries
    with pytest.raises(BridgeError):
        cbf.list_parts(root, AGENT, upl)
    assert cbf.upload_complete(root, AGENT, upl) is False
    assert cbf.remove_tree_beneath(root, cbf.staging_parts(AGENT, upl)) is False
    assert d.is_dir()
    monkeypatch.setattr(cbf, "MAX_DIR_ENTRIES", 4)
    assert cbf.list_parts(root, AGENT, upl) == [0, 1, 2]
    assert cbf.remove_tree_beneath(root, cbf.staging_parts(AGENT, upl)) is True
