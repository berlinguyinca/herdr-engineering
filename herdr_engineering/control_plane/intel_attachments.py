"""Attachment triage before anything is parsed or previewed (spec 02 / 05).

Decides ``accept`` / ``quarantine`` / ``reject`` from bytes alone with hard budgets. It never executes or renders
the content. Magic bytes outrank the declared MIME type; active content (script, HTML, macros) is never previewable.
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass

MAX_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_RATIO = 100

_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"%PDF-", "application/pdf"),
          (b"GIF8", "image/gif"), (b"PK\x03\x04", "application/zip"), (b"\xd0\xcf\x11\xe0", "application/x-ole"))
_ALLOWED = frozenset({"image/png", "image/jpeg", "image/gif", "application/pdf", "text/csv", "text/plain",
                      "application/zip-ooxml", "application/xml-scientific"})
_ACTIVE_MARKERS = (b"<script", b"javascript:", b"onload=", b"onerror=", b"<iframe", b"<foreignobject", b"<!doctype html", b"<html")
_MACRO_ENTRIES = ("vbaproject.bin", "macros/", "xl/macrosheets")


@dataclass(frozen=True)
class Triage:
    verdict: str            # accept | quarantine | reject
    sniffed_type: str
    sha256: str
    previewable: bool
    reasons: tuple[str, ...]


def sniff(data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    head = data[:2048].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in head:
        return "image/svg+xml"
    if any(head.startswith(m) for m in (b"<html", b"<!doctype html")):
        return "text/html"
    if data[:2048].startswith(b"<?xml") or data[:2048].lstrip().startswith(b"<mz"):
        return "application/xml-scientific"
    try:
        data[:4096].decode("utf-8")
        return "text/csv" if b"," in data[:4096] and b"\n" in data[:4096] else "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _zip_findings(data: bytes) -> tuple[list[str], str]:
    reasons: list[str] = []
    kind = "application/zip"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                reasons.append("too many archive entries")
            total = sum(i.file_size for i in infos)
            if total > MAX_UNCOMPRESSED:
                reasons.append("uncompressed size over budget")
            if any(i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_RATIO for i in infos):
                reasons.append("suspicious compression ratio (possible zip bomb)")
            for i in infos:
                n = i.filename.lower()
                if n.startswith("/") or ".." in n.split("/"):
                    reasons.append("path traversal entry")
                if any(m in n for m in _MACRO_ENTRIES):
                    reasons.append("macro content")
                if n.endswith(".zip"):
                    reasons.append("nested archive")
            if any(i.filename.startswith(("word/", "xl/", "ppt/")) for i in infos):
                kind = "application/zip-ooxml"
    except zipfile.BadZipFile:
        reasons.append("malformed archive")
    return reasons, kind


def triage(data: bytes, declared_type: str | None = None, filename: str = "") -> Triage:
    sha = hashlib.sha256(data).hexdigest()
    if len(data) > MAX_BYTES:
        return Triage("reject", "unknown", sha, False, ("over size limit",))
    mime = sniff(data)
    reasons: list[str] = []
    if mime == "application/zip":
        zr, mime = _zip_findings(data)
        reasons += zr
    low = data[:65536].lower()
    if mime in ("image/svg+xml", "text/html") or any(m in low for m in _ACTIVE_MARKERS) and mime != "application/pdf":
        reasons.append("active content")
    if mime == "application/x-ole":
        reasons.append("legacy OLE document (macros cannot be excluded)")
    if mime == "application/octet-stream":
        reasons.append("unrecognised content")
    base = (declared_type or "").split(";")[0].strip().lower()
    if base and base != mime and not (mime in ("text/csv", "text/plain") and base.startswith("text/")) \
            and not (mime == "application/zip-ooxml" and base.endswith(("document", "sheet", "presentation"))):
        reasons.append(f"declared type {base} does not match content {mime}")
    if filename.lower().endswith((".exe", ".js", ".vbs", ".bat", ".scr", ".html", ".htm", ".svg")):
        reasons.append("executable/active file extension")
    if reasons:
        severe = any(r.startswith(("malformed", "suspicious", "path traversal", "too many", "uncompressed")) for r in reasons)
        return Triage("reject" if severe else "quarantine", mime, sha, False, tuple(dict.fromkeys(reasons)))
    ok = mime in _ALLOWED
    return Triage("accept" if ok else "quarantine", mime, sha, mime.startswith("image/") or mime == "application/pdf",
                  () if ok else (f"type {mime} not on the allow-list",))
