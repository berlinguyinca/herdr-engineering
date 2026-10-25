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

Limits are YAML (``control_plane.collab.files``): ``max_bytes`` per file (25 MiB) and ``room_bytes``
per room (500 MiB). Deleting a file (its uploader or a room admin) removes every version and gives
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
from typing import Any

from . import ids
from .rustfs import IntegrityError

MiB = 1024 * 1024
DEFAULT_MAX_BYTES = 25 * MiB
DEFAULT_ROOM_BYTES = 500 * MiB
HARD_MAX_BYTES = 1024 * MiB          # the ceiling any YAML value is held to (one request is read into memory)
MAX_NAME = 200
MAX_MESSAGE_FILES = 10
STABLE_DIR = ".collab/files"
FILE_EVENTS = frozenset({"file_version_added", "file_deleted"})
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_KEY = re.compile(r"sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\Z")
_EXECUTABLE = frozenset({"exe", "dll", "so", "dylib", "msi", "bat", "cmd", "com", "scr", "ps1", "sh", "bash", "zsh",
                         "vbs", "app", "bin", "run", "jar", "apk", "deb", "rpm", "elf", "pyz", "command", "appimage"})
_ARCHIVE = frozenset({"zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "7z", "rar", "zst", "lz", "lzma", "cab",
                      "iso", "dmg"})


def _error(status: int, message: str, code: str | None = None):
    from .collab import CollabError  # collab imports this module; resolve at call time
    return CollabError(status, message, code=code)


def _human(n: int) -> str:
    if n % MiB == 0 and n >= MiB:
        return f"{n // MiB} MiB"
    return f"{n} bytes"


def normalize_files(value: Any) -> dict:
    """``control_plane.collab.files`` -> {max_bytes, room_bytes}. Absent = the defaults."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("control_plane.collab.files must be a mapping")
    unknown = sorted(str(k) for k in set(value) - {"max_bytes", "room_bytes"})
    if unknown:
        raise ValueError("control_plane.collab.files: unknown keys: " + ", ".join(unknown))
    out = {"max_bytes": value.get("max_bytes", DEFAULT_MAX_BYTES), "room_bytes": value.get("room_bytes", DEFAULT_ROOM_BYTES)}
    for k, v in out.items():
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f"control_plane.collab.files.{k} must be a positive number of bytes")
    if out["max_bytes"] > HARD_MAX_BYTES:
        raise ValueError(f"control_plane.collab.files.max_bytes may be at most {HARD_MAX_BYTES} bytes")
    if out["room_bytes"] < out["max_bytes"]:
        raise ValueError("control_plane.collab.files.room_bytes must be at least max_bytes")
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
    name = segments[-1].strip()
    if not name or name == ".":
        raise _error(400, "file name is empty")
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


def _numbered(name: str, n: int) -> str:
    dot = name.find(".", 1)
    return f"{name} ({n})" if dot < 0 else f"{name[:dot]} ({n}){name[dot:]}"


def stable_path(name: str) -> str:
    return f"{STABLE_DIR}/{name}"


# ---------------------------------------------------------------------------------------- blob stores
class MemoryBlobStore:
    """In-process stand-in with RustFS's interface (tests and an ephemeral service)."""

    def __init__(self):
        self._data: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        with self._lock:
            self._data[key] = bytes(data)
        return key, digest

    def get(self, key: str, expected_sha256: str) -> bytes:
        data = self._data[key]
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise IntegrityError(f"sha256 mismatch for {key}")
        return data

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class DirectoryBlobStore:
    """Content-addressed files under one directory, with RustFS's ``put``/``get``/``delete`` interface.
    Used when no RustFS endpoint is configured (the collab-only container keeps them next to its log)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, mode=0o700, exist_ok=True)

    def _path(self, key: str) -> str:
        m = _KEY.match(key or "")
        if not m:
            raise ValueError("not a content key")
        return os.path.join(self.root, "sha256", m.group(1), m.group(2), m.group(3))

    def put(self, data: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
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
                self._room_file_names.setdefault(rid, {})[p["name"]] = p["file_id"]
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
            if names.get(rec["name"]) == rec["id"]:
                del names[rec["name"]]
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
        while out in names:
            n += 1
            out = _numbered(name, n)
        return out

    def _check_size(self, room_id: str, size: int, limits: dict) -> None:
        if size > limits["max_bytes"]:
            raise _error(413, f"the file is {size} bytes; the limit is {_human(limits['max_bytes'])} per file",
                         code="file_too_large")
        used = self._room_file_bytes.get(room_id, 0)
        if used + size > limits["room_bytes"]:
            raise _error(413, f"this room's files would use {used + size} bytes; the room limit is "
                              f"{_human(limits['room_bytes'])}. Delete files to free space.", code="room_quota_exceeded")

    def _drop_unreferenced(self, keys) -> None:
        for key in set(keys):
            if self._blob_refs.get(key, 0) <= 0 and not self._blob_pending.get(key):
                self._blob_refs.pop(key, None)
                try:
                    self.file_blobs.delete(key)
                except Exception:
                    self.stats["file_blob_delete_failures"] += 1

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
        with self._lock:
            self._authorize(author, room_id, "post", "file.put")
            self._no_external(author)
            if parent is not None:
                self._live_file(room_id, parent)
            else:
                clean_file_name(name)
            self._check_size(room_id, len(data), limits)
            # An in-flight upload holds its content key, so a concurrent delete of the same bytes keeps the blob.
            pending = hashlib.sha256(data).hexdigest()
            pending = f"sha256/{pending[:2]}/{pending[2:4]}/{pending}"
            self._blob_pending[pending] = self._blob_pending.get(pending, 0) + 1
        try:
            key, digest = self.file_blobs.put(data)
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
        finally:       # a refused upload leaves its bytes as an unreferenced, content-addressed blob
            with self._lock:
                left = self._blob_pending.get(pending, 1) - 1
                if left > 0:
                    self._blob_pending[pending] = left
                else:
                    self._blob_pending.pop(pending, None)
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
            return {"files": self._shelf(pr, room, room_id), "used_bytes": self._room_file_bytes.get(room_id, 0),
                    **self._files_limits()}

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
            self._drop_unreferenced(v["storage_key"] for v in rec["versions"])
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
