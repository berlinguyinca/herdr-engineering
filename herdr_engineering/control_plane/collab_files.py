"""Room files (C5): a room-scoped shelf of immutable, versioned uploads.

The link approach: people and agents upload into a room; a message attaches files by id; an agent
fetches a file through the ``herdr-eng collab file`` helper into ``.collab/files/<name>`` in its own
working directory. The room never pushes bytes into a harness.

Every upload is immutable. ``parent`` names the file a new version belongs to; the server numbers it
(latest + 1, under the lock), so two agents editing at once get v2 and v3 and nothing is overwritten.
The same name uploaded again without a parent is a different file and gets a de-duplicated name
(``report (2).pdf``). Bytes live in the object store collab already uses for artifacts (RustFS,
content-addressed by sha256); with no RustFS wired, a directory store with the same interface keeps
them next to the collab log. Only metadata enters the event log, so the shelf survives a restart.

Limits are YAML (``control_plane.collab.files``): ``max_bytes`` per file (25 MiB), ``room_bytes`` per room
(500 MiB, the default) and ``room_bytes_max`` (10 GiB), the ceiling a room's own limit is held to. A room owner sets
that room's limit in its properties (``files_room_bytes``, a ``room_settings_set`` event; null = the default).
Lowering it below what the room already uses deletes nothing; it only blocks new uploads. Deleting a file (its uploader or a room admin) removes every version and gives
its bytes back to the room quota. Files are served for download only: ``application/octet-stream``,
``Content-Disposition: attachment`` and ``nosniff``, so nothing (an executable, an archive, an HTML
page) is ever opened inline.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import unicodedata
from typing import Any

from . import ids
from .rustfs import IntegrityError

MiB = 1024 * 1024
DEFAULT_MAX_BYTES = 25 * MiB
DEFAULT_ROOM_BYTES = 500 * MiB
DEFAULT_ROOM_BYTES_MAX = 10 * 1024 * MiB   # the ceiling for a room's own limit (a typo cannot fill the disk)
ROOM_SETTINGS = ("files_room_bytes",)
HARD_MAX_BYTES = 1024 * MiB          # the ceiling any YAML value is held to (one request is read into memory)
MAX_NAME = 200
MAX_MESSAGE_FILES = 10
STABLE_DIR = ".collab/files"
FILE_EVENTS = frozenset({"file_version_added", "file_deleted"})
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
# Room files have their OWN keyspace: the artifact workspace writes `sha256/..` into the same bucket, and a room
# file's deletion must never be able to remove an artifact's object with equal bytes.
KEY_PREFIX = "collab-files/"
_KEY = re.compile(r"collab-files/sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\Z")
MEMORY_STORE_CAP = 256 * MiB        # all rooms together, when bytes live only in this process
_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                           *(f"lpt{i}" for i in range(1, 10))})
_EXECUTABLE = frozenset({"exe", "dll", "so", "dylib", "msi", "bat", "cmd", "com", "scr", "ps1", "sh", "bash", "zsh",
                         "vbs", "app", "bin", "run", "jar", "apk", "deb", "rpm", "elf", "pyz", "command", "appimage"})
_ARCHIVE = frozenset({"zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "7z", "rar", "zst", "lz", "lzma", "cab",
                      "iso", "dmg"})


def _error(status: int, message: str, code: str | None = None):
    from .collab import CollabError  # collab imports this module; resolve at call time
    return CollabError(status, message, code=code)


def content_key(digest: str) -> str:
    return f"{KEY_PREFIX}sha256/{digest[:2]}/{digest[2:4]}/{digest}"


def _room_key(key: str) -> str:
    if not _KEY.match(key or ""):
        raise ValueError("not a room-file content key")
    return key


class StoreFull(Exception):
    """The in-process file store reached its cap."""


def _human(n: int) -> str:
    if n % MiB == 0 and n >= MiB:
        return f"{n // MiB} MiB"
    return f"{n} bytes"


def normalize_files(value: Any) -> dict:
    """``control_plane.collab.files`` -> {max_bytes, room_bytes, room_bytes_max}. Absent = the defaults."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("control_plane.collab.files must be a mapping")
    unknown = sorted(str(k) for k in set(value) - {"max_bytes", "room_bytes", "room_bytes_max"})
    if unknown:
        raise ValueError("control_plane.collab.files: unknown keys: " + ", ".join(unknown))
    out = {"max_bytes": value.get("max_bytes", DEFAULT_MAX_BYTES), "room_bytes": value.get("room_bytes", DEFAULT_ROOM_BYTES),
           "room_bytes_max": value.get("room_bytes_max", DEFAULT_ROOM_BYTES_MAX)}
    for k, v in out.items():
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"control_plane.collab.files.{k} must be a positive number of bytes")
    if out["max_bytes"] > HARD_MAX_BYTES:
        raise ValueError(f"control_plane.collab.files.max_bytes may be at most {HARD_MAX_BYTES} bytes")
    if out["room_bytes"] < out["max_bytes"]:
        raise ValueError("control_plane.collab.files.room_bytes must be at least max_bytes")
    if out["room_bytes_max"] < out["room_bytes"]:
        raise ValueError("control_plane.collab.files.room_bytes_max must be at least room_bytes")
    return out


def clean_file_name(raw: Any) -> str:
    """The name a file is stored and fetched under: directories are stripped, a ``..`` segment, NUL and
    control characters are refused, so ``.collab/files/<name>`` can never point outside that folder."""
    if not isinstance(raw, str):
        raise _error(400, "file name must be a string")
    from .collab_external import _INVISIBLE
    if _BAD_CHARS.search(raw) or _INVISIBLE.search(raw):     # incl. bidi overrides: `evil\u202efdp.exe` reads as .pdf
        raise _error(400, "file name may not contain control or invisible characters")
    segments = re.split(r"[/\\]", raw)
    if any(s.strip() == ".." for s in segments):
        raise _error(400, "file name may not contain '..'")
    name = unicodedata.normalize("NFC", segments[-1].strip())
    if not name or name == ".":
        raise _error(400, "file name is empty")
    if name.split(".", 1)[0].rstrip(" ").lower() in _DEVICE_NAMES:
        raise _error(400, "file name is a reserved device name (CON, NUL, COM1, ...)")
    if len(name) > MAX_NAME:
        raise _error(400, f"file name must be at most {MAX_NAME} characters")
    return name


def file_kind(name: str) -> str:
    """'executable' and 'archive' files are stored but never opened: the shelf offers a download only."""
    exts = name.lower().split(".")[1:]
    if exts and exts[-1] in _EXECUTABLE:
        return "executable"
    if any(e in _ARCHIVE for e in exts[-2:]):
        return "archive"
    return "file"


def name_key(name: str) -> str:
    """Two names that are the same file on macOS or Windows (case, NFC/NFD) are the same name in a room."""
    return unicodedata.normalize("NFC", name).casefold()


def _numbered(name: str, n: int) -> str:
    dot = name.find(".", 1)
    return f"{name} ({n})" if dot < 0 else f"{name[:dot]} ({n}){name[dot:]}"


def stable_path(name: str) -> str:
    return f"{STABLE_DIR}/{name}"


# ---------------------------------------------------------------------------------------- blob stores
class MemoryBlobStore:
    """In-process stand-in with RustFS's interface. Not durable: a service with a durable event log refuses to
    upload into it, and its bytes are capped across all rooms."""

    durable = False

    def __init__(self, max_total: int = MEMORY_STORE_CAP):
        self._data: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self.max_total = max_total

    def put(self, data: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        key = content_key(digest)
        with self._lock:
            if key not in self._data:
                if sum(len(v) for v in self._data.values()) + len(data) > self.max_total:
                    raise StoreFull("in-memory file store is full")
                self._data[key] = bytes(data)
        return key, digest

    def get(self, key: str, expected_sha256: str) -> bytes:
        data = self._data[_room_key(key)]
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise IntegrityError(f"sha256 mismatch for {key}")
        return data

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(_room_key(key), None)


class RoomFileObjectStore:
    """Room files in the artifact object store (RustFS/S3), under their own ``collab-files/`` prefix. It never
    reads, writes or deletes a key outside that prefix."""

    durable = True

    def __init__(self, client):
        self.client = client

    def put(self, data: bytes) -> tuple[str, str]:
        key, digest = self.client.put(data, prefix=KEY_PREFIX)
        return _room_key(key), digest

    def get(self, key: str, expected_sha256: str) -> bytes:
        return self.client.get(_room_key(key), expected_sha256)

    def delete(self, key: str) -> None:
        self.client.delete(_room_key(key))


class DirectoryBlobStore:
    """Content-addressed files under one directory, with RustFS's ``put``/``get``/``delete`` interface.
    Used when no RustFS endpoint is configured (the collab-only container keeps them next to its log)."""

    durable = True

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, mode=0o700, exist_ok=True)

    def _path(self, key: str) -> str:
        m = _KEY.match(key or "")
        if not m:
            raise ValueError("not a room-file content key")
        return os.path.join(self.root, "sha256", m.group(1), m.group(2), m.group(3))

    def put(self, data: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        key = content_key(digest)
        path = self._path(key)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".up-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        return key, digest

    def get(self, key: str, expected_sha256: str) -> bytes:
        with open(self._path(key), "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise IntegrityError(f"sha256 mismatch for {key}")
        return data

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------------------- the shelf
def _by(p: dict) -> dict:
    return {"type": p.get("actor_kind") or ("agent" if p.get("actor_agent_id") else "user"),
            "id": p.get("actor_agent_id") or p["actor_user_id"], "owner_user_id": p["actor_user_id"]}


def _principal_ref(pr) -> dict:
    return {"type": "external" if pr.external else pr.kind, "id": pr.id, "owner_user_id": pr.owner}


class FilesMixin:
    """Room files for CollabService. State is folded from ``file_*`` events; bytes live in ``file_blobs``."""

    def _init_files(self) -> None:
        self.files_limits = normalize_files(None)
        self.files_source = None               # optional callable -> raw YAML mapping, read per request
        self.file_blobs: Any = MemoryBlobStore()
        self._files: dict[str, dict] = {}                 # file id -> {room_id, name, uploader, versions, deleted}
        self._room_file_names: dict[str, dict] = {}       # room id -> live name -> file id
        self._room_file_bytes: dict[str, int] = {}        # room id -> bytes of every live version
        self._blob_refs: dict[str, int] = {}              # storage key -> live versions that use it (any room)
        self._blob_pending: dict[str, int] = {}           # storage key -> uploads in flight (memory only)
        self._blob_deleting: set[str] = set()             # storage keys being deleted outside the lock

    def _files_limits(self) -> dict:
        if self.files_source is not None:
            try:
                return normalize_files(self.files_source())
            except Exception:                    # a bad edit keeps the last good limits
                return self.files_limits
        return self.files_limits

    # ------------------------------------------------------------------ fold
    def _apply_files(self, ev: dict) -> bool:
        t, p = ev["event_type"], ev["payload"]
        if t not in FILE_EVENTS:
            return False
        rid = p["room_id"]
        if t == "file_version_added":
            rec = self._files.get(p["file_id"])
            if rec is None:
                rec = self._files[p["file_id"]] = {"id": p["file_id"], "room_id": rid, "name": p["name"],
                                                   "uploader": _by(p), "versions": [], "deleted": False}
                self._room_file_names.setdefault(rid, {})[name_key(p["name"])] = p["file_id"]
            rec["versions"].append({"version": p["version"], "size": p["size"], "sha256": p["sha256"],
                                    "storage_key": p["storage_key"], "by": _by(p), "created_at": p["created_at"]})
            self._room_file_bytes[rid] = self._room_file_bytes.get(rid, 0) + p["size"]
            self._blob_refs[p["storage_key"]] = self._blob_refs.get(p["storage_key"], 0) + 1
        else:
            rec = self._files.get(p["file_id"])
            if rec is None or rec["deleted"]:
                return True
            rec["deleted"] = True
            names = self._room_file_names.get(rid, {})
            if names.get(name_key(rec["name"])) == rec["id"]:
                del names[name_key(rec["name"])]
            for v in rec["versions"]:
                self._room_file_bytes[rid] = self._room_file_bytes.get(rid, 0) - v["size"]
                self._blob_refs[v["storage_key"]] = self._blob_refs.get(v["storage_key"], 1) - 1
        return True

    # ------------------------------------------------------------------ helpers
    def _live_file(self, room_id: str, file_id: Any) -> dict:
        rec = self._files.get(file_id) if isinstance(file_id, str) else None
        if rec is None or rec["deleted"] or rec["room_id"] != room_id:
            raise _error(404, "file not found")
        return rec

    def _files_reader(self, pr, room_id: str, audit: str) -> dict:
        room, _ = self._authorize(pr, room_id, "read", audit)
        self._no_external(pr)
        self._no_guest(pr, room)
        return room

    def _free_name(self, room_id: str, name: str) -> str:
        names = self._room_file_names.get(room_id, {})
        n, out = 1, name
        while name_key(out) in names:
            n += 1
            out = _numbered(name, n)
        return out

    def _room_limit(self, room_id: str, limits: dict) -> int:
        """The room's own limit (its properties) or the YAML default, never above the YAML ceiling. Read at call
        time, so the commit-time re-check of an upload sees a limit changed while its bytes were stored."""
        own = ((self._rooms.get(room_id) or {}).get("settings") or {}).get("files_room_bytes")
        return min(own if isinstance(own, int) else limits["room_bytes"], limits["room_bytes_max"])

    def _check_size(self, room_id: str, size: int, limits: dict) -> None:
        if size > limits["max_bytes"]:
            raise _error(413, f"the file is {size} bytes; the limit is {_human(limits['max_bytes'])} per file",
                         code="file_too_large")
        used, cap = self._room_file_bytes.get(room_id, 0), self._room_limit(room_id, limits)
        if used + size > cap:
            over = (f"this room's files already use {used} bytes, more than its limit of {_human(cap)}"
                    if used >= cap else f"this room's files would use {used + size} bytes; the room limit is {_human(cap)}")
            raise _error(413, f"{over}. Delete files to free space, or ask a room owner to raise the limit in the "
                              "room's properties.", code="room_quota_exceeded")

    def _claim_unreferenced(self, keys) -> list[str]:
        """Under the lock: the keys no live version and no in-flight upload uses. They are marked as being deleted
        (an upload of the same bytes waits) and are deleted by _delete_blobs, outside the lock."""
        out = []
        for key in set(keys):
            if self._blob_refs.get(key, 0) <= 0 and not self._blob_pending.get(key) and key not in self._blob_deleting:
                self._blob_refs.pop(key, None)
                self._blob_deleting.add(key)
                out.append(key)
        return out

    def _delete_blobs(self, keys: list[str]) -> None:
        """Outside the lock: the store may be across the network."""
        for key in keys:
            try:
                self.file_blobs.delete(key)
            except Exception:
                self.stats["file_blob_delete_failures"] += 1
        if keys:
            with self._cv:
                self._blob_deleting.difference_update(keys)
                self._cv.notify_all()

    def _upload_check(self, room_id: str, author, name: Any, size: int, parent: Any, limits: dict) -> dict:
        """Everything an upload is refused for, decided before a byte of it is read. Call under the lock."""
        room, _ = self._authorize(author, room_id, "post", "file.put")
        self._no_external(author)
        if self._sink is not None and not getattr(self.file_blobs, "durable", True):
            raise _error(503, "no durable file store: the operator must configure RustFS or the collab log directory",
                         code="no_durable_file_store")
        if parent is not None:
            self._live_file(room_id, parent)
        else:
            clean_file_name(name)
        self._check_size(room_id, size, limits)
        return room

    def upload_precheck(self, room_id: str, author, name: Any, size: int, parent: Any = None) -> None:
        with self._lock:
            self._upload_check(room_id, author, name, size, parent, self._files_limits())

    @staticmethod
    def _version_view(rec: dict, v: dict) -> dict:
        return {"id": rec["id"], "name": rec["name"], "version": v["version"], "size": v["size"], "sha256": v["sha256"]}

    # ------------------------------------------------------------------ operations
    def put_file(self, room_id: str, author, name: Any, data: Any, parent: Any = None) -> dict:
        """Store ``data`` as a new file (no parent) or as the next version of ``parent``.
        Returns {id, name, version, size, sha256}."""
        if not isinstance(data, (bytes, bytearray)):
            raise _error(400, "file content must be bytes")
        data = bytes(data)
        limits = self._files_limits()
        digest = hashlib.sha256(data).hexdigest()
        pending = content_key(digest)
        with self._cv:
            self._upload_check(room_id, author, name, len(data), parent, limits)
            while pending in self._blob_deleting:          # the same bytes are being deleted: wait for that
                self._cv.wait(1.0)
            # An in-flight upload holds its content key, so a concurrent delete of the same bytes keeps the blob.
            self._blob_pending[pending] = self._blob_pending.get(pending, 0) + 1
        committed = False
        try:
            try:
                key, digest = self.file_blobs.put(data)
            except StoreFull:
                raise _error(507, "the file store is full; ask the operator", code="file_store_full") from None
            with self._lock:
                room, _ = self._authorize(author, room_id, "post", "file.put")
                if parent is not None:
                    rec = self._live_file(room_id, parent)
                    file_id, final, version = rec["id"], rec["name"], len(rec["versions"]) + 1
                else:
                    file_id, final, version = ids.new_id("file"), self._free_name(room_id, clean_file_name(name)), 1
                self._check_size(room_id, len(data), limits)       # again: a racing upload may have used the room
                self._commit("file_version_added", self._actor(
                    author, project_id=room["project_id"], room_id=room_id, file_id=file_id, name=final,
                    version=version, size=len(data), sha256=digest, storage_key=key, parent=parent,
                    action="file.put", outcome="allowed", project_ids=[room["project_id"]], resource_id=file_id))
                committed = True
        finally:
            with self._lock:
                left = self._blob_pending.get(pending, 1) - 1
                if left > 0:
                    self._blob_pending[pending] = left
                else:
                    self._blob_pending.pop(pending, None)
                orphan = [] if committed else self._claim_unreferenced([pending])   # a refused upload's bytes
            self._delete_blobs(orphan)
        self.stats["files_put"] += 1
        return {"id": file_id, "name": final, "version": version, "size": len(data), "sha256": digest}

    def get_file(self, pr, room_id: str, file_id: Any, version: Any = None) -> tuple[dict, bytes]:
        with self._lock:
            self._files_reader(pr, room_id, "file.get")
            rec = self._live_file(room_id, file_id)
            if version is None:
                v = rec["versions"][-1]
            else:
                if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= len(rec["versions"]):
                    raise _error(404, "no such version")
                v = rec["versions"][version - 1]
            meta = {**self._version_view(rec, v), "kind": file_kind(rec["name"])}
            key = v["storage_key"]
        try:
            data = self.file_blobs.get(key, meta["sha256"])
        except Exception as exc:
            self.stats["file_blob_read_failures"] += 1
            raise _error(503, "file storage unavailable") from exc
        return meta, data

    def list_files(self, pr, room_id: str) -> dict:
        with self._lock:
            room = self._files_reader(pr, room_id, "file.list")
            limits = self._files_limits()
            return {"files": self._shelf(pr, room, room_id), "used_bytes": self._room_file_bytes.get(room_id, 0),
                    **limits, "room_bytes": self._room_limit(room_id, limits), "room_bytes_default": limits["room_bytes"]}

    # ------------------------------------------------------------------ room properties (the room's file limit)
    def _settings_view(self, pr, room: dict, room_id: str) -> dict:
        limits = self._files_limits()
        eff = self._effective(pr, room)
        return {"files_room_bytes": (room.get("settings") or {}).get("files_room_bytes"),
                "files_room_bytes_default": limits["room_bytes"],
                "files_room_bytes_effective": self._room_limit(room_id, limits),
                "files_room_bytes_max": limits["room_bytes_max"],
                "files_used_bytes": self._room_file_bytes.get(room_id, 0), "files_max_bytes": limits["max_bytes"],
                "can_edit": not pr.is_agent and bool(eff and "invite" in eff[0])}

    def get_room_settings(self, pr, room_id: str) -> dict:
        """The room's properties as the shelf's readers see them: its own file limit (null = the default), the
        effective limit, what the room uses, and the ceiling. Guests and external agents have no shelf."""
        with self._lock:
            room = self._files_reader(pr, room_id, "settings.get")
            return self._settings_view(pr, room, room_id)

    def set_room_settings(self, pr, room_id: str, body: Any) -> dict:
        """Room owners only (the people who hold `invite`, as for the helper profile and deleting others' files).
        ``files_room_bytes``: a whole number of bytes from 1 to the YAML ceiling, or null for the default."""
        from .collab import CollabError
        with self._lock:
            room = self._rooms.get(room_id)
            m = self._member(room, pr.id) if room and not pr.is_agent else None
            eff = self._effective(pr, room) if room else None
            if not eff:
                self.stats["policy_denied"] += 1
                raise CollabError(404, "not found")
            if pr.is_agent or pr.external or not m or "invite" not in eff[0]:
                self._audit_denied(pr, "settings.set", "not_owner", room_id=room_id, project_ids=[room["project_id"]])
                self.stats["policy_denied"] += 1
                raise CollabError(403, "only a room owner may change the room's properties")
            if not isinstance(body, dict) or not body or set(body) - set(ROOM_SETTINGS):
                raise CollabError(400, "room properties: files_room_bytes is the only setting")
            limits = self._files_limits()
            v = body["files_room_bytes"]
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= limits["room_bytes_max"]):
                raise CollabError(400, f"files_room_bytes must be a whole number of bytes from 1 to "
                                       f"{limits['room_bytes_max']} ({_human(limits['room_bytes_max'])}), or null for the default")
            self._throttle("settings:" + pr.id, 60, 600.0, "room properties")
            old = {k: (room.get("settings") or {}).get(k) for k in ROOM_SETTINGS}
            new = {**old, "files_room_bytes": v}
            self._commit("room_settings_set", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, settings=new, old=old,
                action="settings.set", outcome="allowed", project_ids=[room["project_id"]], resource_id=room_id))
            return self._settings_view(pr, room, room_id)

    def room_files_usage(self, room_id: str) -> dict:
        with self._lock:
            return {"used_bytes": self._room_file_bytes.get(room_id, 0),
                    "limit_bytes": self._room_limit(room_id, self._files_limits())}

    def _shelf(self, pr, room: dict, room_id: str) -> list[dict]:
        eff = self._effective(pr, room)
        admin = not pr.is_agent and bool(eff and "invite" in eff[0])
        me = _principal_ref(pr)
        out = []
        for fid in self._room_file_names.get(room_id, {}).values():
            rec = self._files[fid]
            last = rec["versions"][-1]
            out.append({**self._version_view(rec, last), "path": stable_path(rec["name"]), "kind": file_kind(rec["name"]),
                        "uploaded_by": rec["uploader"], "updated_at": last["created_at"],
                        "can_delete": admin or (rec["uploader"]["type"], rec["uploader"]["id"]) == (me["type"], me["id"]),
                        "versions": [{"version": v["version"], "size": v["size"], "sha256": v["sha256"], "by": v["by"],
                                      "created_at": v["created_at"]} for v in rec["versions"]]})
        out.sort(key=lambda r: r["id"])
        return out

    def delete_file(self, pr, room_id: str, file_id: Any) -> dict:
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "file.delete")
            self._no_external(pr)
            rec = self._live_file(room_id, file_id)
            eff = self._effective(pr, room)
            admin = not pr.is_agent and bool(eff and "invite" in eff[0])
            me = _principal_ref(pr)
            if not admin and (rec["uploader"]["type"], rec["uploader"]["id"]) != (me["type"], me["id"]):
                self._audit_denied(pr, "file.delete", "forbidden", room_id=room_id, resource_id=rec["id"],
                                   project_ids=[room["project_id"]])
                raise _error(403, "only the uploader or a room admin may delete a file")
            freed = sum(v["size"] for v in rec["versions"])
            self._commit("file_deleted", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, file_id=rec["id"], name=rec["name"],
                action="file.delete", outcome="allowed", project_ids=[room["project_id"]], resource_id=rec["id"]))
            drop = self._claim_unreferenced(v["storage_key"] for v in rec["versions"])
        self._delete_blobs(drop)                                   # no lock held across store I/O
        return {"deleted": rec["id"], "freed_bytes": freed}

    def _message_files(self, room_id: str, value: Any) -> list[dict]:
        """A message's ``files`` from ids (optionally with a version): the server fills in name, version,
        size and hash itself, so a client can never forge them."""
        if not isinstance(value, list) or len(value) > MAX_MESSAGE_FILES:
            raise _error(400, f"files must be a list of at most {MAX_MESSAGE_FILES} file ids")
        out = []
        for item in value:
            fid, version = (item.get("id"), item.get("version")) if isinstance(item, dict) else (item, None)
            rec = self._files.get(fid) if isinstance(fid, str) else None
            if rec is None or rec["deleted"] or rec["room_id"] != room_id:
                raise _error(400, "files must reference files of this room")
            if version is None:
                v = rec["versions"][-1]
            elif isinstance(version, int) and not isinstance(version, bool) and 1 <= version <= len(rec["versions"]):
                v = rec["versions"][version - 1]
            else:
                raise _error(400, "files: no such version")
            view = self._version_view(rec, v)
            if view not in out:
                out.append(view)
        return out

    def room_files(self, pr, room_id: str) -> list[dict] | None:
        """The shelf for the room long-poll: None for a guest or an external agent (no access)."""
        with self._lock:
            room = self._rooms[room_id]
            if pr.external or (not pr.is_agent and (self._member(room, pr.id) or {}).get("role") == "guest"):
                return None
            return self._shelf(pr, room, room_id)


# ---------------------------------------------------------------------------------------- HTTP helpers
def transfer_route(method: str, parts: list[str]) -> str | None:
    """'upload' / 'download' for the raw-bytes routes (handled outside the JSON dispatcher), else None.
    parts are the segments after /api/v1/collab."""
    if len(parts) >= 3 and parts[0] == "rooms" and parts[2] == "files":
        if method == "POST" and (len(parts) == 3 or (len(parts) == 5 and parts[4] == "versions")):
            return "upload"
        if method == "GET" and len(parts) == 4:
            return "download"
    return None


def content_disposition(name: str) -> str:
    from urllib.parse import quote
    ascii_name = re.sub(r'[^A-Za-z0-9._ ()+-]', "_", name) or "file"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
