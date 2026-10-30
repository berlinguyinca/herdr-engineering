"""Frozen share bundles: name rules, a verified file copy under one directory, and the per-token state projection.

Pure file logic (no HTTP, no database). A share is a directory ``<root>/<share_id>/files/<relative name>`` whose every file
was hash- and size-verified while copied, plus ``<root>/by-token/<sha256(token)>/state.json``, the projection a database-free
public server reads. Hostile input is refused rather than repaired: names that could leave the bundle (``..``, absolute,
backslash, NUL, control characters, empty or overlong segments), duplicates after Unicode/case folding, a name used both as
a file and a directory, bodies that do not match their pinned digest or size, and bundles over the caps. Files are created
with ``O_EXCL | O_NOFOLLOW`` under a destination that must not exist, and a destination that fails part-way is removed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

MAX_SEGMENT_BYTES = 255
MAX_NAME_BYTES = 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHARE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_RESERVED_IDS = {"by-token"}

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".parquet": "application/vnd.apache.parquet",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
}
MAX_SCAN_BYTES = 1024 * 1024
_SCANNED = {".html", ".css", ".js", ".mjs", ".svg"}


class ShareBundleError(ValueError):
    """The bundle, a name in it, or a state key is not acceptable."""


@dataclass
class BundleLimits:
    max_bytes: int
    max_files: int


@dataclass
class BundleFile:
    name: str
    sha256: str
    size: int
    content_type: str


@dataclass
class BundleIndex:
    entry: str
    files: list[BundleFile]
    total_bytes: int
    warnings: list[str] = field(default_factory=list)


def normalise_name(name: str) -> str:
    """The canonical relative name (NFC), or ``ShareBundleError``. Never decodes percent escapes."""
    if not isinstance(name, str) or not name:
        raise ShareBundleError("file name is empty")
    name = unicodedata.normalize("NFC", name)
    if any(ord(c) < 0x20 or ord(c) == 0x7F or 0xD800 <= ord(c) <= 0xDFFF for c in name):
        raise ShareBundleError("file name contains a control character or surrogate")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ShareBundleError("file name is too long")
    if name.startswith("/") or "\\" in name:
        raise ShareBundleError("file name must be a relative path with '/' separators")
    for segment in name.split("/"):
        if segment in ("", ".", ".."):
            raise ShareBundleError("file name has an empty, '.' or '..' segment")
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            raise ShareBundleError("file name segment is too long")
    return name


def content_type_for(name: str) -> str:
    return _CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


# Detection is a single-pass scan: one regex finds candidate URLs by their own shape (a host-or-absolute path right after a
# quote, "(", "=" or whitespace; no overlapping or nested quantifiers, so every failed start costs O(1) and matches never
# overlap), then a fixed-size look-back decides whether the context loads it. The number of candidates examined is capped,
# so the cost of one scan is bounded whatever the content looks like.
_CANDIDATE = re.compile(r"""(?<=[\s"'(=])((?:https?:)?//[^\s"'<>)]+|/[^/\s"'<>)][^\s"'<>)]*)""")
_LOOKBACK = 400
MAX_CANDIDATES = 20000  # distinct loading URLs recorded per scan
MAX_BUNDLE_SCAN_BYTES = 16 * 1024 * 1024
_ATTRS = {"src", "srcset", "poster", "data-src"}


def _trailing_word(prefix: str) -> str:
    end = len(prefix)
    i = end
    while i > 0 and (prefix[i - 1].isalnum() or prefix[i - 1] in "_-"):
        i -= 1
    return prefix[i:end].lower()


def _loads_url(prefix: str) -> bool:
    """Does the text right before a candidate URL make it a load (src=, href= on <link>, url(), import, @import, from)?"""
    if prefix and prefix[-1] in "\"'":
        prefix = prefix[:-1]
    prefix = prefix.rstrip()
    if prefix.endswith("="):
        prefix = prefix[:-1].rstrip()
        word = _trailing_word(prefix)
        if word in _ATTRS:
            return True
        if word == "href":
            tag = prefix.rfind("<")
            return tag >= 0 and ">" not in prefix[tag:] and prefix[tag + 1:tag + 5].lower() == "link"
        return False
    if prefix.endswith("("):
        return _trailing_word(prefix[:-1].rstrip()) in ("url", "import")
    return _trailing_word(prefix) in ("import", "from")


def _scan(data: bytes) -> tuple[list[str], bool]:
    """(urls, stopped): ``stopped`` when the cap of recorded URLs was hit, so the file may hold more."""
    text = data[:MAX_SCAN_BYTES].decode("utf-8", "replace")
    found: list[str] = []
    seen: set[str] = set()
    for match in _CANDIDATE.finditer(text):
        url = match.group(1)
        if url in seen or not _loads_url(text[max(0, match.start() - _LOOKBACK):match.start()]):
            continue
        if len(found) >= MAX_CANDIDATES:
            return found, True
        seen.add(url)
        found.append(url)
    return found, False


def scan_external_references(html: bytes) -> list[str]:
    """URLs a page loads from another host or by absolute path (neither works from a frozen bundle). Relative and ``data:`` are fine."""
    return _scan(html)[0]


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", name).casefold())


def _check_dest(dest: Path) -> None:
    if dest.is_symlink() or dest.exists():
        raise ShareBundleError("bundle destination already exists")


def _write_file(base: Path, name: str, expected_sha: str, expected_size: int, chunks: Iterable[bytes], budget: int) -> int:
    target = base.joinpath(*name.split("/"))
    current = base
    for part in name.split("/")[:-1]:
        current = current / part
        if not current.is_dir():
            current.mkdir()
            os.chmod(current, 0o755)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    os.fchmod(fd, 0o644)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "wb") as handle:
        for chunk in chunks:
            size += len(chunk)
            if size > expected_size or size > budget:
                raise ShareBundleError("%s is larger than its pinned size" % name)
            digest.update(chunk)
            handle.write(chunk)
    if size != expected_size:
        raise ShareBundleError("%s is smaller than its pinned size" % name)
    if digest.hexdigest() != expected_sha:
        raise ShareBundleError("%s does not match its pinned digest" % name)
    return size


def choose_entry(entry: str | None, names: list[str]) -> str:
    """The bundle's entry: ``entry`` if given (must be in ``names``), else index.html, the single HTML, the single PDF."""
    if entry is not None:
        wanted = normalise_name(entry)
        if wanted not in names:
            raise ShareBundleError("entry %s is not in the bundle" % wanted)
        return wanted
    if "index.html" in names:
        return "index.html"
    for ext in (".html", ".pdf"):
        found = [n for n in names if n.lower().endswith(ext)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ShareBundleError("several %s files, pass an entry: %s" % (ext, ", ".join(sorted(found))))
    raise ShareBundleError("no HTML or PDF entry in the bundle; files: %s" % ", ".join(sorted(names)[:10]))


def build_bundle(dest: Path, files: Iterable[tuple[str, str, int, Iterable[bytes]]], *, entry: str | None,
                 limits: BundleLimits) -> BundleIndex:
    """Verify and write ``files`` (name, sha256, size, chunks) under ``dest/files``. ``dest`` must not exist; on any error it is removed."""
    dest = Path(dest)
    _check_dest(dest)
    created = False
    try:
        entries = []
        seen: dict[str, str] = {}
        folded_dirs: set[str] = set()
        total_declared = 0
        for name, sha, size, chunks in files:
            name = normalise_name(name)
            if not isinstance(sha, str) or not _SHA256.match(sha):
                raise ShareBundleError("%s has no valid sha256" % name)
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ShareBundleError("%s has no valid size" % name)
            key = _fold(name)
            if key in seen:
                raise ShareBundleError("duplicate name (case/Unicode-folded): %s and %s" % (seen[key], name))
            seen[key] = name
            parts = key.split("/")
            for i in range(1, len(parts)):
                folded_dirs.add("/".join(parts[:i]))
            if len(seen) > limits.max_files:
                raise ShareBundleError("more than %d files" % limits.max_files)
            total_declared += size
            if total_declared > limits.max_bytes:
                raise ShareBundleError("bundle is over %d bytes" % limits.max_bytes)
            entries.append((name, sha, size, chunks))
        if not entries:
            raise ShareBundleError("bundle has no files")
        if folded_dirs & set(seen):
            clash = sorted(seen[k] for k in folded_dirs & set(seen))
            raise ShareBundleError("name is both a file and a directory: %s" % ", ".join(clash))
        dest.mkdir(parents=True)
        created = True
        os.chmod(dest, 0o755)
        base = dest / "files"
        base.mkdir()
        os.chmod(base, 0o755)
        index_files = []
        total = 0
        for name, sha, size, chunks in entries:
            total += _write_file(base, name, sha, size, chunks, limits.max_bytes - total)
            index_files.append(BundleFile(name, sha, size, content_type_for(name)))
        chosen = choose_entry(entry, [f.name for f in index_files])
        warnings = []
        budget = MAX_BUNDLE_SCAN_BYTES
        skipped = False
        for f in index_files:
            if os.path.splitext(f.name)[1].lower() not in _SCANNED:
                continue
            take = min(f.size, MAX_SCAN_BYTES)
            if take > budget:
                skipped = True
                continue
            budget -= take
            if f.size > MAX_SCAN_BYTES:
                warnings.append("%s is over %d bytes; only the first %d were checked for external references"
                                % (f.name, MAX_SCAN_BYTES, MAX_SCAN_BYTES))
            with open(base.joinpath(*f.name.split("/")), "rb") as handle:
                refs, stopped = _scan(handle.read(MAX_SCAN_BYTES))
            if stopped:
                warnings.append("reference scan of %s stopped after %d candidates; file may contain more" % (f.name, MAX_CANDIDATES))
            warnings.extend("%s references %s" % (f.name, url) for url in refs[:20])
        if skipped:
            warnings.append("the scan budget (%d bytes) was used up; the remaining files were not checked for external references"
                            % MAX_BUNDLE_SCAN_BYTES)
        return BundleIndex(chosen, index_files, total, warnings)
    except BaseException:
        if created:
            shutil.rmtree(dest, ignore_errors=True)
        raise


def _token_dir(root: Path, token_hash: str) -> Path:
    if not isinstance(token_hash, str) or not _SHA256.match(token_hash):
        raise ShareBundleError("token hash must be 64 lowercase hex digits")
    return Path(root) / "by-token" / token_hash


def _mkdirs(path: Path, root: Path) -> None:
    for d in (path.parent, path):
        d.mkdir(exist_ok=True)
        os.chmod(d, 0o755)


def write_state(root: Path, token_hash: str, state: dict) -> None:
    """Atomically publish ``state`` as ``<root>/by-token/<token_hash>/state.json`` (a reader never sees a partial file)."""
    directory = _token_dir(root, token_hash)
    Path(root).mkdir(parents=True, exist_ok=True)
    _mkdirs(directory, Path(root))
    fd, tmp_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o644)
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, directory / "state.json")
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def remove_state(root: Path, token_hash: str) -> None:
    """Revoke: the very next lookup finds nothing. Idempotent."""
    directory = _token_dir(root, token_hash)
    state = directory / "state.json"
    try:
        state.unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(directory, ignore_errors=True)


def remove_bundle(root: Path, share_id: str) -> None:
    """Delete ``<root>/<share_id>`` (the files). Idempotent; ``share_id`` must be a plain id, never a path."""
    if not isinstance(share_id, str) or not _SHARE_ID.match(share_id) or share_id in _RESERVED_IDS:
        raise ShareBundleError("invalid share id")
    target = Path(root) / share_id
    if target.is_symlink():
        target.unlink()
    else:
        shutil.rmtree(target, ignore_errors=True)
