"""Authenticated, immutable report routes and bounded asset transport."""
from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import PurePosixPath
from urllib.parse import parse_qs, quote, urlsplit

MAX_ASSET_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


class LimitedInput:
    def __init__(self, stream, length):
        self.stream, self.remaining = stream, length

    def read(self, size=CHUNK_BYTES):
        size = min(self.remaining, CHUNK_BYTES, size if size >= 0 else CHUNK_BYTES)
        if size == 0:
            return b""
        value = self.stream.read(size)
        self.remaining -= len(value)
        return value


def _body(handler, allowed):
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("report mutations require application/json")
    value = handler._read_json()
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("invalid report request fields; identity is derived from authentication")
    return value


def _access(handler, artifact_id, permission):
    return handler._artifact_call("report_permission", artifact_id, handler._resource_principal(), permission)


def _text(handler, data, *, mime="text/html; charset=utf-8", status=200):
    payload = data.encode() if isinstance(data, str) else data
    handler._response_status = status
    handler.send_response(status)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                        "script-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'; "
                        "frame-ancestors 'self'; sandbox")
    handler.end_headers()
    handler.wfile.write(payload)


def _download(handler, server, asset):
    if server.rustfs is None or not hasattr(server.rustfs, "iter_get"):
        handler._json({"error": "verified report downloads are unavailable"}, 503)
        return
    # Verify the full object before headers, using bounded RAM and temporary disk.
    # Never deliver corrupt bytes and only learn that at the end of the response.
    with tempfile.TemporaryFile(mode="w+b") as spool:
        digest, size = hashlib.sha256(), 0
        try:
            for chunk in server.rustfs.iter_get(asset["storage_key"], asset["sha256"], chunk_size=CHUNK_BYTES):
                if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                    raise ValueError("unbounded object chunk")
                size += len(chunk)
                if size > asset["size_bytes"]:
                    raise ValueError("object larger than pinned size")
                digest.update(chunk)
                spool.write(chunk)
            if size != asset["size_bytes"] or digest.hexdigest() != asset["sha256"]:
                raise ValueError("pinned report asset integrity mismatch")
        except Exception:
            handler._json({"error": "report asset integrity verification failed"}, 502)
            return
        spool.seek(0)
        handler._response_status = 200
        handler.send_response(200)
        handler.send_header("Content-Type", asset["media_type"])
        handler.send_header("Content-Length", str(size))
        handler.send_header("Content-Disposition", "attachment; filename*=UTF-8''" +
                            quote(PurePosixPath(asset["name"]).name, safe=""))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        handler.send_header("ETag", '"' + asset["sha256"] + '"')
        handler.end_headers()
        try:
            while chunk := spool.read(CHUNK_BYTES):
                handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True


def route_report(handler, server, parts, resource):
    """Return true only after handling a report route, otherwise leave legacy routes alone."""
    creating = resource == "artifact-reports" and len(parts) == 4
    building = resource == "artifact-reports" and len(parts) == 6 and parts[5] == "jobs"
    build_status = resource == "artifact-reports" and len(parts) == 7 and parts[5] == "jobs"
    uploading = resource == "artifacts" and len(parts) == 6 and parts[5] == "blobs"
    versioned = (resource == "artifacts" and len(parts) >= 8 and parts[5] == "versions"
                 and parts[7] in {"report", "diff", "feedback", "checks", "preview", "assets", "outputs"})
    if not any((creating, building, build_status, uploading, versioned)):
        return False
    if server.artifact_store is None:
        handler._json({"error": "versioned report registry is unavailable"}, 503)
        return True
    method = handler.command
    origin = handler.headers.get("Origin")
    if method == "POST" and origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                         or urlsplit(origin).scheme not in {"http", "https"}):
        handler._deny(403, "cross-origin report mutation refused")
        return True
    if creating:
        if method != "POST":
            handler._json({"error": "not found"}, 404)
            return True
        body = _body(handler, {"title"})
        title = body.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 240:
            raise ValueError("report title is required (up to 240 characters)")
        artifact = handler._artifact_call("register_logical", original_filename=title,
            artifact_type="report", mime_type="text/html", created_by=handler._resource_principal())
        if server.artifact_acl is not None:
            server.artifact_acl.grant(artifact["artifact_id"], handler._resource_principal(),
                                      {"read", "edit", "review", "share", "admin"})
        handler._json(artifact, 201)
        return True
    artifact_id = parts[4]
    permission = "read" if method == "GET" else "edit"
    if versioned and method == "POST" and parts[7] in {"checks", "feedback"}:
        permission = "review"
    if not _access(handler, artifact_id, permission):
        handler._deny(404, "not found")
        return True
    if build_status:
        if method != "GET":
            handler._json({"error": "not found"}, 404)
            return True
        job = handler._artifact_call("get_report_job", artifact_id, parts[6])
        if job is None or job.get("artifact_id") != artifact_id:
            handler._json({"error": "not found"}, 404)
        else:
            handler._json({key: value for key, value in job.items() if key != "request"})
        return True
    query = parse_qs(urlsplit(handler.path).query)
    if uploading:
        if method != "POST" or server.rustfs is None or not hasattr(server.rustfs, "put_stream"):
            handler._json({"error": "streaming report upload is unavailable"}, 503)
            return True
        lengths = handler.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]) or handler.headers.get("Transfer-Encoding"):
            handler._deny(400, "unambiguous Content-Length is required")
            return True
        length = int(lengths[0])
        if length > MAX_ASSET_BYTES:
            handler._deny(413, "report asset exceeds upload limit")
            return True
        name = query.get("filename", ["asset.bin"])[0]
        path = PurePosixPath(name)
        if not name or path.is_absolute() or any(part in {"..", "."} for part in name.split("/")) or "\\" in name:
            raise ValueError("unsafe report asset name")
        mime = handler.headers.get("Content-Type", "application/octet-stream")
        # Persist the same bare media-type contract used by report descriptors.
        # Reject parameters before storing bytes so an immutable digest cannot
        # become bound to metadata that no valid descriptor can reference.
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime):
            raise ValueError("invalid report asset media type")
        uploaded = server.rustfs.put_stream(LimitedInput(handler.rfile, length), expected_size=length,
                                            media_type=mime, chunk_size=CHUNK_BYTES)
        registered = handler._artifact_call("register_report_blob", artifact_id,
            storage_key=uploaded["key"], sha256=uploaded["sha256"], size_bytes=uploaded["size"], media_type=mime)
        handler._json({"name": name, **registered}, 201)
        return True
    if building:
        if method != "POST" or server.artifact_job_manager is None:
            handler._json({"error": "report build worker is unavailable"}, 503)
            return True
        body = _body(handler, {"base_version_id", "request"})
        if "base_version_id" not in body or not isinstance(body.get("request"), dict):
            raise ValueError("base_version_id and request are required")
        from .artifact_report_descriptor import validate_report_descriptor
        request = body["request"]
        if set(request) - {"report_descriptor", "assets", "source"}:
            raise ValueError("report requests accept descriptor, source and asset references; execution identity is worker-owned")
        validate_report_descriptor(request.get("report_descriptor"), request.get("assets") or [])
        job = server.artifact_job_manager.submit(profile="report", artifact_id=artifact_id,
            base_version_id=body["base_version_id"], request=request, requested_by=handler._resource_principal())
        if server.artifact_job_manager.on_change is None:
            handler._artifact_call("record_job_event", "created", job)
        handler._json(job, 201)
        return True
    version_id, action = parts[6], parts[7]
    if action == "outputs" and len(parts) == 9 and method == "GET":
        version = handler._artifact_call("get_version", version_id, artifact_id)
        if version is None or not re.fullmatch(r"[0-9]+", parts[8]):
            handler._json({"error": "not found"}, 404)
            return True
        outputs = version.get("output_manifest") or []
        index = int(parts[8])
        if index >= len(outputs):
            handler._json({"error": "not found"}, 404)
            return True
        output = outputs[index]
        # Use the reference the registry authorized, never a second digest field
        # that could point at different bytes in the global content-addressed store.
        reference = output.get("storage_key") or output.get("blob_sha256")
        digest = reference.rsplit("/", 1)[-1] if isinstance(reference, str) else None
        size = output.get("size_bytes")
        media_type = output.get("media_type", "application/octet-stream")
        if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_ASSET_BYTES
                or not isinstance(media_type, str)
                or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:;[^\r\n]*)?", media_type)):
            handler._json({"error": "pinned output lacks verifiable download metadata"}, 409)
            return True
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        if (reference not in {key, digest}
                or any(field in output and output[field] != digest
                       for field in ("sha256", "blob_sha256"))):
            handler._json({"error": "pinned output references disagree"}, 409)
            return True
        _download(handler, server, {"storage_key": key, "sha256": digest, "size_bytes": size,
                                   "media_type": media_type, "name": output.get("name", "output.bin")})
        return True
    if action == "assets" and len(parts) == 9 and method == "GET":
        asset = handler._artifact_call("get_report_asset", artifact_id, version_id, parts[8])
        _download(handler, server, asset)
        return True
    if len(parts) != 8:
        handler._json({"error": "not found"}, 404)
        return True
    if method == "GET":
        if action == "diff":
            base = query.get("base_version_id", [None])[0]
            if not base:
                raise ValueError("base_version_id is required")
            handler._json(handler._artifact_call("get_report_diff", artifact_id, version_id, base))
        elif action == "feedback":
            limit = int(query.get("limit", [10])[0])
            offset = int(query.get("offset", [0])[0])
            if not 1 <= limit <= 20 or offset < 0:
                raise ValueError("feedback limit must be 1..20 and offset nonnegative")
            values = handler._artifact_call("list_report_feedback", artifact_id, version_id,
                                            limit=limit, offset=offset)
            total = handler._artifact_call("count_report_feedback", artifact_id, version_id)
            handler._json({"artifact_id": artifact_id, "version_id": version_id, "feedback": values,
                           "total": total, "has_more": offset + len(values) < total})
        elif action == "preview":
            from .artifact_report_render import render_report_preview
            descriptor = handler._artifact_call("get_report_descriptor", artifact_id, version_id)
            prefix = f"/api/v1/artifacts/{quote(artifact_id, safe='')}/versions/{quote(version_id, safe='')}/assets/"
            _text(handler, render_report_preview(descriptor,
                asset_url=lambda asset: prefix + quote(asset["id"], safe="")))
        elif action in {"report", "checks"}:
            report = handler._artifact_call("get_report", artifact_id, version_id)
            report["preview_url"] = f"/api/v1/artifacts/{quote(artifact_id, safe='')}/versions/{quote(version_id, safe='')}/preview"
            handler._json(report)
        else:
            handler._json({"error": "not found"}, 404)
        return True
    if method == "POST" and action in {"feedback", "checks"}:
        allowed = {"target_kind", "target_id", "body"} if action == "feedback" else {
            "stage", "status", "notes", "evidence_refs"}
        body = _body(handler, allowed)
        function = "add_report_feedback" if action == "feedback" else "record_report_check"
        handler._json(handler._artifact_call(function, artifact_id, version_id,
                                             **body, actor=handler._resource_principal()), 201)
        return True
    handler._json({"error": "not found"}, 404)
    return True
