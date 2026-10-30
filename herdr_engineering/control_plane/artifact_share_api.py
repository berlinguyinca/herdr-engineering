"""Owner API for artifact share links (spec 2026-10-30-artifact-share-links-design.md, "Owner API").

Routes (all need the ``share`` permission on the artifact; a denial is a non-disclosing 404):

* ``POST /api/v1/artifacts/{id}/shares`` creates a ``pending`` share and dry-runs the bundle (names, sizes, entry,
  warnings) without copying bytes. No token exists yet.
* ``POST .../shares/{sid}/approve`` ``{"confirm": true}`` copies the pinned version's hash-verified outputs into a fresh
  ``<shares_root>/<share_id>/files/`` bundle, writes ``by-token/<sha256(token)>/state.json`` LAST, marks the share
  live and returns the link ONCE. Any failure removes what this call wrote and leaves the share ``pending``.
* ``GET .../shares``, ``GET .../shares/{sid}/events``: never carry a token or its hash.
* ``GET .../shares/{sid}``: the share and, while pending, the same dry-run summary create returned (for the CLI to
  print what approve will expose before the separate confirmed call).
* ``POST .../shares/{sid}/revoke`` removes ``state.json`` first (the next request already 404s), then the bundle.

The token is never stored, logged or returned twice: only its sha256 reaches the store and the volume.
"""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .artifact_report_api import CHUNK_BYTES, PinnedOutputError, _access, pinned_output
from .artifact_shares import ShareError, new_token
from .share_bundle import (
    BundleLimits,
    ShareBundleError,
    build_bundle,
    check_names,
    choose_entry,
    normalise_name,
    remove_bundle,
    remove_state,
    write_state,
)

DEFAULTS = {"max_share_bytes": 512 * 1024 * 1024, "max_files": 20000, "max_total_bytes": 2 * 1024 * 1024 * 1024,
            "default_public_days": 30}
PREVIEW_NAME = "preview.html"
LISTED_FILES = 200
MAX_DAYS = 3650
CREATE_FIELDS = {"version_id", "audience", "allow_emails", "allow_groups", "expires_in_days", "entry", "title"}
# Serialises approvals in this process: the live-bytes budget is read, then spent, and a bundle directory is created.
_APPROVE_LOCK = threading.Lock()


class ShareHTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def share_settings_config(raw: dict | None, *, public_url: str | None) -> dict:
    """Validated share caps (YAML ``control_plane.share``) plus the public base URL links are issued under."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("control_plane.share must be a mapping")
    settings = {}
    for name, default in DEFAULTS.items():
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"control_plane.share.{name} must be a positive integer")
        settings[name] = value
    if settings["default_public_days"] > 90:
        raise ValueError("control_plane.share.default_public_days must be 1-90")
    if not isinstance(public_url, str) or not public_url.startswith("https://") or not urlsplit(public_url).netloc:
        raise ValueError("share links need an https:// control_plane.collab.public_url")
    return {"public_url": public_url, **settings}


# ------------------------------------------------------------------------------------------------ helpers


def _call(store, method, *args, **kwargs):
    runner = getattr(store, "sync_call", None) or (store if callable(store) else None)
    if runner is None:
        raise RuntimeError("store adapter is unavailable")
    return runner(method, *args, **kwargs)


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items() if k != "token_hash"}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _share_body(handler, allowed):
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("share requests require application/json")
    value = handler._read_json()
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("invalid share request fields; allowed: " + ", ".join(sorted(allowed)))
    return value


def _report_preview(descriptor: dict) -> bytes:
    """The rendered report preview with asset and descriptor links reduced to plain text (assets are not shared)."""
    from .artifact_report_render import render_report_preview
    page = render_report_preview(descriptor, asset_url=lambda asset: "#")
    return re.sub(r'<a href="(?:#|report)">(.*?)</a>', r"\1", page).encode("utf-8")


def _chunks(server, item):
    """Lazily stream one pinned object (opened only when ``build_bundle`` reaches it)."""
    for chunk in server.rustfs.iter_get(item["storage_key"], item["sha256"], chunk_size=CHUNK_BYTES):
        if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
            raise ShareBundleError(f"{item['name']}: unbounded object chunk")
        yield chunk


def _measure(server, item, budget: int) -> int:
    """Size of an output whose manifest entry lacks ``size_bytes``, from a hash-verified download."""
    digest, size = hashlib.sha256(), 0
    for chunk in _chunks(server, item):
        size += len(chunk)
        if size > budget:
            raise ShareBundleError(f"{item['name']} does not fit the share cap")
        digest.update(chunk)
    if digest.hexdigest() != item["sha256"]:
        raise ShareBundleError(f"{item['name']} does not match its pinned digest")
    return size


def _plan(server, artifact_id: str, version_id: str, entry: str | None) -> tuple[list[dict], str, list[str]]:
    """Dry run: the files a bundle of this version holds, its entry and warnings. No object bytes are read."""
    registry = server.artifact_store
    settings = server.share_settings
    version = _call(registry, "get_version", version_id, artifact_id)
    if version is None:
        raise ShareHTTPError(404, "not found")
    if version.get("state") != "ready":
        raise ShareHTTPError(400, f"version is {version.get('state')}, not ready; only a ready version can be shared")
    warnings: list[str] = []
    if version.get("report_descriptor"):
        report = _call(registry, "get_report", artifact_id, version_id)
        rendered = ((report.get("checks") or {}).get("rendered") or {}).get("status")
        if rendered != "passed":
            raise ShareHTTPError(400, f"report version is not rendered (rendered check: {rendered})")
        if entry not in (None, PREVIEW_NAME):
            raise ShareHTTPError(400, f"a report share's entry is its generated {PREVIEW_NAME}")
        page = _report_preview(_call(registry, "get_report_descriptor", artifact_id, version_id))
        files = [{"name": PREVIEW_NAME, "sha256": hashlib.sha256(page).hexdigest(), "size_bytes": len(page),
                  "data": page}]
        return files, PREVIEW_NAME, ["report evidence files are not included; the preview lists them as text"]
    files = []
    for position, output in enumerate(version.get("output_manifest") or []):
        try:
            pinned = pinned_output(output, require_size=False)
        except PinnedOutputError as exc:
            raise ShareHTTPError(400, f"output {position}: {exc}; a share needs hash-verifiable outputs") from None
        name = output.get("name")
        if not isinstance(name, str):
            raise ShareHTTPError(400, f"output {position} has no name")
        try:
            name = normalise_name(name)
        except ShareBundleError as exc:
            raise ShareHTTPError(400, f"output {position}: {exc}") from None
        files.append({**pinned, "name": name})
    names = [f["name"] for f in files]
    try:
        check_names(names)
    except ShareBundleError as exc:
        raise ShareHTTPError(400, str(exc)) from None
    if not any(n.lower().endswith((".html", ".pdf")) for n in names):
        raise ShareHTTPError(400, "version has no HTML or PDF output to share")
    if len(files) > settings["max_files"]:
        raise ShareHTTPError(400, f"share has {len(files)} files, more than the {settings['max_files']} files allowed")
    declared = sum(f["size_bytes"] or 0 for f in files)
    if declared > settings["max_share_bytes"]:
        raise ShareHTTPError(400, f"share is {declared} bytes, over the {settings['max_share_bytes']} byte cap")
    try:
        chosen = choose_entry(entry, names)
    except ShareBundleError as exc:
        raise ShareHTTPError(400, str(exc)) from None
    unsized = sum(f["size_bytes"] is None for f in files)
    if unsized:
        warnings.append(f"{unsized} file(s) have no recorded size; they will be measured from the verified download at approve")
    return files, chosen, warnings


def _summary(files, entry, warnings, audience, expires_at, total=None) -> dict:
    known = [f["size_bytes"] for f in files if f["size_bytes"] is not None]
    return {"entry": entry, "file_count": len(files),
            "total_bytes": total if total is not None else sum(known),
            "files": [{"name": f["name"], "size_bytes": f["size_bytes"]} for f in files[:LISTED_FILES]],
            "audience": audience, "expires_at": _jsonable(expires_at), "warnings": list(warnings)}


def _get_share(server, artifact_id: str, share_id: str) -> dict:
    share = _call(server.share_store, "get", share_id)
    if share is None or share.get("artifact_id") != artifact_id:
        raise ShareHTTPError(404, "not found")
    return share


# ------------------------------------------------------------------------------------------------ approve


def approve_share(server, share: dict, *, approved_by: str | None = None) -> tuple[str, dict]:
    """Build the frozen bundle for a pending ``share``, publish its state and mark it live; returns ``(url, summary)``.

    Order: validate -> scope check -> measure -> build bundle -> total cap -> write state.json -> mark live. On any
    failure the state file and bundle this call wrote are removed and the share stays pending. Between write_state and
    mark_live the projection exists while the row is pending, which is harmless: the token exists only in this call.
    """
    settings = server.share_settings
    root = Path(server.shares_root)
    share_id = share["share_id"]
    if share.get("state") != "pending":
        raise ShareHTTPError(409, f"share is {share.get('state')}; a link is issued only once")
    expires_at = share.get("expires_at")
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ShareHTTPError(410, "share expired before approval; create a new share")
    with _APPROVE_LOCK:
        files, entry, warnings = _plan(server, share["artifact_id"], share["version_id"], share.get("entry"))
        _call(server.artifact_store, "verify_share_outputs", share["artifact_id"], share["version_id"])
        live = int(_call(server.share_store, "live_total_bytes"))
        cap, total_cap = settings["max_share_bytes"], settings["max_total_bytes"]

        def over_total(size):
            if live + size > total_cap:
                raise ShareHTTPError(400, f"approving would put {live + size} bytes live, over the {total_cap} byte total cap "
                                     f"({live} already live)")

        over_total(sum(f["size_bytes"] or 0 for f in files))
        budget = cap - sum(f["size_bytes"] or 0 for f in files)
        for item in files:
            if item["size_bytes"] is None:
                item["size_bytes"] = _measure(server, item, budget)
                budget -= item["size_bytes"]
        over_total(sum(f["size_bytes"] for f in files))        # measured sizes, before anything is written
        dest = root / share_id
        if dest.exists() or dest.is_symlink():
            raise ShareHTTPError(409, "a bundle directory already exists for this share; revoke it and create a new share")
        index = build_bundle(dest, ((f["name"], f["sha256"], f["size_bytes"],
                                     [f["data"]] if "data" in f else _chunks(server, f)) for f in files),
                             entry=entry, limits=BundleLimits(cap, settings["max_files"]))
        token_hash = None
        try:
            over_total(index.total_bytes)
            token, token_hash = new_token()
            write_state(root, token_hash, {
                "share_id": share_id, "audience": share["audience"], "state": "live",
                "expires_at": _jsonable(expires_at), "allow_emails": list(share.get("allow_emails") or []),
                "allow_groups": list(share.get("allow_groups") or []), "entry": index.entry, "title": share.get("title"),
                "files": [{"name": f.name, "sha256": f.sha256, "size": f.size, "content_type": f.content_type}
                          for f in index.files]})
            try:
                _call(server.share_store, "mark_live", share_id, token_hash=token_hash, index=index,
                      approved_by=approved_by or "unknown")
            except ShareError as exc:
                raise ShareHTTPError(410 if exc.code == "expired" else 409, str(exc)) from None
        except BaseException:
            if token_hash is not None:
                remove_state(root, token_hash)
            remove_bundle(root, share_id)
            raise
    url = settings["public_url"].rstrip("/") + "/s/" + token + "/"
    summary = {"entry": index.entry, "file_count": len(index.files), "total_bytes": index.total_bytes,
               "expires_at": _jsonable(expires_at), "audience": share["audience"],
               "warnings": warnings + index.warnings}
    return url, summary


# ------------------------------------------------------------------------------------------------ routes


def _create(handler, server, artifact_id):
    body = _share_body(handler, CREATE_FIELDS)
    version_id, audience = body.get("version_id"), body.get("audience")
    if not isinstance(version_id, str) or not version_id:
        raise ValueError("version_id is required")
    days = body.get("expires_in_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS):
        raise ValueError(f"expires_in_days must be an integer from 1 to {MAX_DAYS}")
    if days is None and audience == "public":
        days = server.share_settings["default_public_days"]
    expires_at = datetime.now(UTC) + timedelta(days=days) if days is not None else None
    entry, title = body.get("entry"), body.get("title")
    if entry is not None and not isinstance(entry, str):
        raise ValueError("entry must be a string")
    if title is not None and (not isinstance(title, str) or len(title) > 240):
        raise ValueError("title must be a string of up to 240 characters")
    files, chosen, warnings = _plan(server, artifact_id, version_id, entry)
    row = _call(server.share_store, "create", artifact_id=artifact_id, version_id=version_id, audience=audience,
                allow_emails=body.get("allow_emails"), allow_groups=body.get("allow_groups"),
                expires_at=expires_at, entry=entry, title=title, created_by=handler._resource_principal())
    handler._json({"share": _jsonable(row),
                   "summary": _summary(files, chosen, warnings, audience, row.get("expires_at"))}, 201)


def _show(handler, server, share):
    """The share (never its token hash) and, while pending, the exposure summary approve would publish."""
    out = {"share": _jsonable(share)}
    if share.get("state") == "pending":
        try:
            files, entry, warnings = _plan(server, share["artifact_id"], share["version_id"], share.get("entry"))
            out["summary"] = _summary(files, entry, warnings, share["audience"], share.get("expires_at"))
        except ShareHTTPError as exc:      # the version changed since create: say what approve would refuse
            out["summary_error"] = exc.message
    handler._json(out)


def _revoke(handler, server, share):
    root = Path(server.shares_root)
    if share.get("token_hash"):
        remove_state(root, share["token_hash"])
    row = _call(server.share_store, "mark_revoked", share["share_id"], actor=handler._resource_principal())
    # An approve that raced this revoke (pending when read above, live just before mark_revoked) set a token hash
    # this call has not seen: read it back so its state.json cannot outlive the revoked row.
    latest = _call(server.share_store, "get", share["share_id"]) or {}
    if latest.get("token_hash") and latest["token_hash"] != share.get("token_hash"):
        remove_state(root, latest["token_hash"])
    remove_bundle(root, share["share_id"])
    handler._json({"share": _jsonable(row)})


def route_share(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/artifacts/{id}/shares[/{sid}/{approve|revoke|events}]``; false for any other path."""
    if resource != "artifacts" or len(parts) < 6 or parts[5] != "shares":
        return False
    method = handler.command
    listing = len(parts) == 6
    single = len(parts) == 7 and method == "GET"
    action = parts[7] if len(parts) == 8 else None
    allowed = {"approve": "POST", "revoke": "POST", "events": "GET"}
    # Early answers leave a POST body unread: close the connection so its bytes are never parsed as a next request.
    if not (listing and method in {"GET", "POST"}) and not single and allowed.get(action) != method:
        handler._deny(404, "not found")
        return True
    if (getattr(server, "share_store", None) is None or getattr(server, "shares_root", None) is None
            or getattr(server, "share_settings", None) is None or server.artifact_store is None):
        handler._deny(503, "sharing is not enabled")
        return True
    origin = handler.headers.get("Origin")
    if method == "POST" and origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                         or urlsplit(origin).scheme not in {"http", "https"}):
        handler._deny(403, "cross-origin share mutation refused")
        return True
    artifact_id = parts[4]
    if not _access(handler, artifact_id, "share"):
        handler._deny(404, "not found")
        return True
    try:
        if listing and method == "GET":
            rows = _call(server.share_store, "list_for_artifact", artifact_id)
            handler._json({"artifact_id": artifact_id, "shares": _jsonable(rows)})
        elif listing:
            _create(handler, server, artifact_id)
        elif single:
            _show(handler, server, _get_share(server, artifact_id, parts[6]))
        else:
            # Read (and validate) a POST body before any lookup can answer early.
            body = _share_body(handler, {"confirm"} if action == "approve" else set()) if method == "POST" else None
            share = _get_share(server, artifact_id, parts[6])
            if action == "events":
                events = _call(server.share_store, "events", share["share_id"])
                handler._json({"share_id": share["share_id"], "events": _jsonable(events)})
            elif action == "revoke":
                _revoke(handler, server, share)
            else:
                if body.get("confirm") is not True:
                    raise ValueError('approve requires {"confirm": true}')
                url, summary = approve_share(server, share, approved_by=handler._resource_principal())
                try:
                    row = _call(server.share_store, "get", share["share_id"])
                except Exception:  # the link is live and shown only now: never lose it to a read-back failure
                    row = {"share_id": share["share_id"], "state": "live"}
                handler._json({"share": _jsonable(row), "url": url, "summary": summary})
    except ShareHTTPError as exc:
        handler._json({"error": exc.message}, exc.status)
    return True
