"""Attachment triage before anything is parsed or previewed (spec 02 / 05).

Decides ``accept`` / ``quarantine`` / ``reject`` from bytes alone with hard budgets. It never executes or renders
the content. Magic bytes outrank the declared MIME type; active content (script, HTML, macros) is never previewable.
"""
from __future__ import annotations

import hashlib
import io
import re
import struct
import zipfile
import zlib
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
_OOXML_EXTERNAL = ("externallinks/", "embeddings/", "oleobject")
_PDF_ACTIVE = (b"/javascript", b"/js ", b"/js\n", b"/js/", b"/openaction", b"/launch", b"/embeddedfile", b"/richmedia", b"/submitform")


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


VERIFY_ENTRY_MAX = 8 * 1024 * 1024


def _declared_sizes_lie(data: bytes, info: zipfile.ZipInfo) -> bool:
    """Inflate the raw entry with a hard output cap and compare to the header's ``file_size``.

    Other tools (unzip, office suites) may trust different fields than ``zipfile`` does, so a header that understates
    the real size is treated as hostile. Entries over ``VERIFY_ENTRY_MAX`` are judged by the ratio check alone.
    """
    if info.file_size > VERIFY_ENTRY_MAX or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        return False
    try:
        name_len, extra_len = struct.unpack("<HH", data[info.header_offset + 26:info.header_offset + 30])
        start = info.header_offset + 30 + name_len + extra_len
        raw = data[start:start + info.compress_size]
        if info.compress_type == zipfile.ZIP_STORED:
            return len(raw) != info.file_size
        out = zlib.decompressobj(-15).decompress(raw, info.file_size + 1)
    except (struct.error, zlib.error):
        return True
    return len(out) != info.file_size


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
            if any((i.compress_size == 0 and i.file_size > 0)
                   or (i.compress_size and i.file_size / i.compress_size > MAX_RATIO) for i in infos):
                reasons.append("suspicious compression ratio (possible zip bomb)")
            if len(infos) <= MAX_ARCHIVE_ENTRIES and any(_declared_sizes_lie(data, i) for i in infos):
                reasons.append("suspicious archive: declared sizes do not match the data")
            for i in infos:
                n = i.filename.lower().replace("\\", "/")     # some extractors treat a backslash as a separator
                if n.startswith("/") or ".." in n.split("/") or (len(n) > 1 and n[1] == ":"):
                    reasons.append("path traversal entry")
                if any(m in n for m in _MACRO_ENTRIES):
                    reasons.append("macro content")
                if any(m in n for m in _OOXML_EXTERNAL):
                    reasons.append("external link or embedded object")
                if n.endswith(".zip"):
                    reasons.append("nested archive")
                if n.endswith(".rels") and i.file_size <= VERIFY_ENTRY_MAX:
                    try:
                        if b'targetmode="external"' in zf.read(i).lower().replace(b"'", b'"').replace(b" =", b"=").replace(b"= ", b"="):
                            reasons.append("external link or embedded object")
                    except (zipfile.BadZipFile, RuntimeError, zlib.error):
                        reasons.append("suspicious archive: unreadable relationship part")
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
    low = data.lower()     # whole content (bounded by MAX_BYTES): markers after the first 64 KiB still count
    if mime == "application/pdf":
        # PDF names may be written with #xx hex escapes (/J#61vaScript); decode them before looking for keywords.
        plain = re.sub(rb"#([0-9a-f]{2})", lambda m: bytes([int(m.group(1), 16)]), low)
        if any(m in plain for m in _PDF_ACTIVE) or b"<script" in plain:
            reasons.append("active PDF content (script, launch action or embedded file)")
        if b"/objstm" in plain:
            # Objects inside compressed object streams are not scanned here, so absence of markers proves nothing.
            reasons.append("PDF with compressed object streams cannot be fully inspected")
    elif mime in ("image/svg+xml", "text/html") or any(m in low for m in _ACTIVE_MARKERS):
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


def safe_filename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')[:120].strip(". ")
    return base or "attachment"


async def ingest(store, data: bytes, *, declared_type: str | None, filename: str, created_by: str,
                 mission_id: str | None = None) -> dict:
    """Triage, then register ONLY accepted bytes with the existing ``ArtifactStore`` (so its ``ArtifactRegistered``
    event feeds the intelligence bridge unchanged). Quarantined and rejected bytes are never written anywhere; the
    caller records the returned metadata (sha256, verdict, reasons) as the evidence that they were seen and refused.
    Registered artifacts keep ``temporary`` retention: promoting one is a human decision."""
    result = triage(data, declared_type, filename)
    out = {"verdict": result.verdict, "sha256": result.sha256, "sniffed_type": result.sniffed_type,
           "previewable": result.previewable, "reasons": list(result.reasons), "artifact_id": None}
    if result.verdict == "accept":
        out["artifact_id"] = await store.register(
            original_filename=safe_filename(filename), mime_type=result.sniffed_type, data=data,
            artifact_type="email_attachment", created_by=created_by, mission_id=mission_id, retention_class="temporary")
    return out
