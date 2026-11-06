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
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .artifact_members import is_member_principal
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
from .share_public import (
    MODE_SWITCH_MAX_BYTES,
    SHARED_BY_MAX,
    content_type_for,
    labelled_html,
    shared_by_bar,
)

DEFAULTS = {"max_share_bytes": 512 * 1024 * 1024, "max_files": 20000, "max_total_bytes": 2 * 1024 * 1024 * 1024,
            "default_public_days": 30}
PREVIEW_NAME = "preview.html"
LISTED_FILES = 200
MAX_DAYS = 3650
CREATE_FIELDS = {"version_id", "audience", "allow_emails", "allow_groups", "expires_in_days", "entry", "title", "room_id"}
# Serialises approvals in this process: the live-bytes budget is read, then spent, and a bundle directory is created.
_APPROVE_LOCK = threading.Lock()


class ShareHTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def share_settings_config(raw: dict | None, *, public_url: str | None, public_host: str | None = None) -> dict:
    """Validated share caps (YAML ``control_plane.share``) plus the public base URL links are issued under.

    With ``public_host`` (``control_plane.share.public_host``) links are issued under ``https://<public_host>``; a
    ``public_url`` that says something else is refused (one host, no silent winner), else ``public_url`` as before."""
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
    if public_host is not None:
        from .members_config import valid_public_host
        if not valid_public_host(public_host):
            raise ValueError("control_plane.share.public_host must be a lowercase DNS name (no scheme, port or path)")
        if public_url is not None and public_url.rstrip("/") != f"https://{public_host}":
            raise ValueError("control_plane.share.public_url and public_host disagree: set only public_host")
        public_url = f"https://{public_host}"
    if not isinstance(public_url, str) or not public_url.startswith("https://") or not urlsplit(public_url).netloc:
        raise ValueError("share links need an https:// control_plane.share.public_url")
    from .annotation_anchor import annotation_settings, clean_owner_emails
    return {"public_url": public_url, **settings, "annotations": annotation_settings(raw.get("annotations")),
            "owner_emails": clean_owner_emails(raw.get("owner_emails"))}


# ------------------------------------------------------------------------------------------------ helpers


UNBOUNDED = 2**63 - 1


def _caps(server, created_by) -> tuple[int, int | None]:
    """``(max_share_bytes, max_total_bytes)`` for a share by this creator; ``max_total_bytes`` None = no total check.

    Member-created shares use ``share_settings["member_share_limits"]`` (control_plane.collab.members.share_limits,
    D8; a null value means no check); everyone else keeps the global caps.
    """
    settings = server.share_settings
    if is_member_principal(created_by):
        limits = settings.get("member_share_limits") or {}
        per_share = limits.get("max_share_bytes")
        return (UNBOUNDED if per_share is None else per_share), limits.get("max_total_bytes")
    return settings["max_share_bytes"], settings["max_total_bytes"]


@contextmanager
def _spending(server):
    """The cross-process spend lock: another process (the worker, the API) cannot read the live total meanwhile."""
    _call(server.share_store, "acquire_spend")
    try:
        yield
    finally:
        _call(server.share_store, "release_spend")


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


def _report_preview(descriptor: dict, *, max_bytes: int | None = None) -> bytes:
    """The rendered report preview with asset and descriptor links reduced to plain text (assets are not shared)."""
    from .artifact_report_render import render_report_preview
    options = {} if max_bytes is None else {"max_bytes": max_bytes}
    page = render_report_preview(descriptor, asset_url=lambda asset: "#", **options)
    page = re.sub(r'<a class="btn" href="#" download>.*?</a>', "", page)  # a download button has no target here
    # keep chip styling; a chip whose target was omitted points at the descriptor ("report"), which is not shared
    page = re.sub(r'<a (class="[a-z ]+") href="(?:#|report)">(.*?)</a>', r"<span \1>\2</span>", page)
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


def _output_name(output: object) -> str:
    """The normalised name of one manifest entry (the single normalisation); ``ShareBundleError`` when it has none."""
    name = output.get("name") if isinstance(output, dict) else None
    if not isinstance(name, str):
        raise ShareBundleError("output has no name")
    return normalise_name(name)


def shared_file_names(version: dict) -> frozenset[str]:
    """The file names a share of this version exposes; depends only on the version row.

    A report version exposes its generated ``PREVIEW_NAME``; any other version its normalised ``output_manifest`` names
    (an unnamed or unnormalisable entry exposes nothing).
    """
    if version.get("report_descriptor"):
        return frozenset({PREVIEW_NAME})
    names: set[str] = set()
    for output in version.get("output_manifest") or []:
        try:
            names.add(_output_name(output))
        except ShareBundleError:
            continue
    return frozenset(names)


def _plan(server, artifact_id: str, version_id: str, entry: str | None,
          created_by: str | None = None) -> tuple[list[dict], str, list[str]]:
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
        (preview_name,) = shared_file_names(version)
        files = [{"name": preview_name, "sha256": hashlib.sha256(page).hexdigest(), "size_bytes": len(page),
                  "data": page}]
        return files, PREVIEW_NAME, ["report evidence files are not included; the preview lists them as text"]
    files = []
    for position, output in enumerate(version.get("output_manifest") or []):
        try:
            pinned = pinned_output(output, require_size=False)
        except PinnedOutputError as exc:
            raise ShareHTTPError(400, f"output {position}: {exc}; a share needs hash-verifiable outputs") from None
        try:
            name = _output_name(output)
        except ShareBundleError as exc:
            raise ShareHTTPError(400, f"output {position}: {exc}" if output.get("name") is not None
                                 else f"output {position} has no name") from None
        files.append({**pinned, "name": name})
    names = [f["name"] for f in files]
    try:
        check_names(names)
    except ShareBundleError as exc:
        raise ShareHTTPError(400, str(exc)) from None
    if not any(n.lower().endswith((".html", ".pdf")) for n in names):
        raise ShareHTTPError(400, "version has no HTML or PDF output to share")
    if is_member_principal(created_by):
        _member_plan_check(files, names)
    if len(files) > settings["max_files"]:
        raise ShareHTTPError(400, f"share has {len(files)} files, more than the {settings['max_files']} files allowed")
    declared = sum(f["size_bytes"] or 0 for f in files)
    max_share_bytes = _caps(server, created_by)[0]
    if declared > max_share_bytes:
        raise ShareHTTPError(400, f"share is {declared} bytes, over the {max_share_bytes} byte cap")
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


def _shared_by(creator: str, name: str | None) -> str:
    """The banner name of a member-created link: ``name`` when it is a non-blank string, else the user id."""
    text = name if isinstance(name, str) else ""
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return (text or creator[len("member:"):])[:SHARED_BY_MAX] or "a member"


UNLABELABLE_SUFFIXES = (".svg", ".xhtml", ".xht", ".xml", ".pdf")   # served as active documents the label cannot be shown on


def _member_plan_check(files: list[dict], names: list[str]) -> None:
    """A member-created link labels every page it serves, so it may hold no active document the label cannot be put on."""
    for name in names:
        if name.lower().endswith(UNLABELABLE_SUFFIXES):
            raise ShareHTTPError(400, f"unsupported_type: {name} cannot be shared by a member (SVG, XML and PDF files "
                                 "cannot carry the 'shared by' label); share the HTML or a raster image instead")
    if not any(n.lower().endswith(".html") for n in names):
        raise ShareHTTPError(400, "unsupported_type: a member share needs an HTML output")
    for item in files:
        if item["name"].lower().endswith(".html") and (item["size_bytes"] or 0) > MODE_SWITCH_MAX_BYTES:
            raise ShareHTTPError(400, f"html_too_large: {item['name']} is over {MODE_SWITCH_MAX_BYTES} bytes, the most a "
                                 "member-created link can label")


def _check_labelable(dest: Path, index, shared_by: str) -> None:
    """Dry run of what the share server will do: every HTML page of the built bundle must take the bar."""
    bar = shared_by_bar(shared_by)
    for entry in index.files:
        if not entry.name.lower().endswith(".html"):
            continue
        if entry.size > MODE_SWITCH_MAX_BYTES:
            raise ShareHTTPError(400, f"html_too_large: {entry.name} is over {MODE_SWITCH_MAX_BYTES} bytes, the most a "
                                 "member-created link can label")
        data = (dest / "files" / entry.name).read_bytes()
        if labelled_html(data, content_type_for(entry.name), bar) is None:
            raise ShareHTTPError(400, f"unsupported_html: {entry.name} cannot carry the 'shared by' label (unsupported "
                                 "encoding such as UTF-16, or ambiguous markup: CDATA, <?...>, odd comments, or an unclosed "
                                 "template/textarea/svg/script before <body>); save it as plain UTF-8 HTML")


def approve_share(server, share: dict, *, approved_by: str | None = None,
                  shared_by_name: str | None = None) -> tuple[str, dict]:
    """Build the frozen bundle for a pending ``share``, publish its state and mark it live; returns ``(url, summary)``.

    A share created by a member (``member:<id>``) gets ``shared_by`` in its state: ``shared_by_name`` (the directory's
    display name) else the user id, control characters dropped and cut to ``SHARED_BY_MAX``. Operator links get no field.

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
    token_hash, built = None, False
    try:
        with _APPROVE_LOCK, _spending(server):
            creator = share.get("created_by")
            files, entry, warnings = _plan(server, share["artifact_id"], share["version_id"], share.get("entry"), creator)
            _call(server.artifact_store, "verify_share_outputs", share["artifact_id"], share["version_id"])
            member = is_member_principal(creator)
            live = int(_call(server.share_store, "live_total_bytes", member=member))    # member / operator totals are separate
            cap, total_cap = _caps(server, creator)

            def over_total(size):
                if total_cap is not None and live + size > total_cap:
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
            built = True
            over_total(index.total_bytes)
            extra = {"shared_by": _shared_by(creator, shared_by_name)} if member else {}
            if member:
                _check_labelable(dest, index, extra["shared_by"])
            token, token_hash = new_token()
            write_state(root, token_hash, {
                **extra, "share_id": share_id, "audience": share["audience"], "state": "live",
                "expires_at": _jsonable(expires_at), "allow_emails": list(share.get("allow_emails") or []),
                "allow_groups": list(share.get("allow_groups") or []), "entry": index.entry, "title": share.get("title"),
                "files": [{"name": f.name, "sha256": f.sha256, "size": f.size, "content_type": f.content_type}
                          for f in index.files]})
            try:
                _call(server.share_store, "mark_live", share_id, token_hash=token_hash, index=index,
                      approved_by=approved_by or "unknown")
            except ShareError as exc:
                raise ShareHTTPError({"expired": 410, "not_found": 404}.get(exc.code, 409), str(exc)) from None
    except BaseException:       # includes a release (COMMIT) that failed: no live link without a committed row
        if token_hash is not None:
            remove_state(root, token_hash)
        if built:
            remove_bundle(root, share_id)
        raise
    url = settings["public_url"].rstrip("/") + "/s/" + token + "/"
    summary = {"entry": index.entry, "file_count": len(index.files), "total_bytes": index.total_bytes,
               "expires_at": _jsonable(expires_at), "audience": share["audience"],
               "warnings": warnings + index.warnings}
    return url, summary


# ------------------------------------------------------------------------------------------------ routes


def _room_emails(server, audience, room_id, extra):
    """``room_id`` shares with the people in that room: their sign-in emails, as they are NOW, join the lab allow-list.
    Nobody who joins later is added (create a new share); the link is still the lab link, behind Cognito sign-in."""
    if audience != "lab":
        raise ValueError("room_id only applies to a lab share")
    if not isinstance(room_id, str) or not room_id:
        raise ValueError("room_id must be a room id")
    collab = getattr(server, "collab", None)
    emails = collab.room_member_emails(room_id) if collab is not None else []
    if not emails:
        raise ValueError("that room has no members with a known email, or collaboration is off")
    if extra is not None and not (isinstance(extra, list) and all(isinstance(e, str) for e in extra)):
        raise ValueError("allow_emails must be a list of emails")
    return sorted({*emails, *(extra or [])})


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
    allow_emails = body.get("allow_emails")
    if body.get("room_id") is not None:
        allow_emails = _room_emails(server, audience, body["room_id"], allow_emails)
    owners = server.share_settings.get("owner_emails") or []
    principal = handler._resource_principal()
    # A member's lab share never inherits the operator owner merge: its audience is the caller's own, checked by the store.
    if audience == "lab" and owners and not is_member_principal(principal) and (allow_emails is None or (
            isinstance(allow_emails, list) and all(isinstance(e, str) for e in allow_emails))):
        allow_emails = sorted({e.lower() for e in (*(allow_emails or []), *owners)})   # the owners may always sign in
    files, chosen, warnings = _plan(server, artifact_id, version_id, entry, principal)
    row = _call(server.share_store, "create", artifact_id=artifact_id, version_id=version_id, audience=audience,
                allow_emails=allow_emails, allow_groups=body.get("allow_groups"),
                expires_at=expires_at, entry=entry, title=title, created_by=principal)
    handler._json({"share": _jsonable(row),
                   "summary": _summary(files, chosen, warnings, audience, row.get("expires_at"))}, 201)


def _show(handler, server, share):
    """The share (never its token hash) and, while pending, the exposure summary approve would publish."""
    out = {"share": _jsonable(share)}
    if share.get("state") == "pending":
        try:
            files, entry, warnings = _plan(server, share["artifact_id"], share["version_id"], share.get("entry"),
                                           share.get("created_by"))
            out["summary"] = _summary(files, entry, warnings, share["audience"], share.get("expires_at"))
        except ShareHTTPError as exc:      # the version changed since create: say what approve would refuse
            out["summary_error"] = exc.message
    handler._json(out)


def revoke_share(server, share, actor):
    """Revoke one share: its served state first, then the row, then the bundle; returns the revoked row.

    Shared by the owner route, the operator member controls and purge, so no path leaves a bundle behind."""
    root = Path(server.shares_root)
    if share.get("token_hash"):
        remove_state(root, share["token_hash"])
    row = _call(server.share_store, "mark_revoked", share["share_id"], actor=actor)
    # An approve that raced this revoke (pending when read above, live just before mark_revoked) set a token hash
    # this call has not seen: read it back so its state.json cannot outlive the revoked row.
    latest = _call(server.share_store, "get", share["share_id"]) or {}
    if latest.get("token_hash") and latest["token_hash"] != share.get("token_hash"):
        remove_state(root, latest["token_hash"])
    remove_bundle(root, share["share_id"])
    return row


def _revoke(handler, server, share):
    handler._json({"share": _jsonable(revoke_share(server, share, handler._resource_principal()))})


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
    if (listing and method == "POST") or action == "approve":
        # a deleted, archived or retracted artifact is not shared out: only an active one gets new links
        if handler._artifact_call("lifecycle_of", artifact_id) != "active":
            handler._json({"error": "artifact is not active"}, 409)
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
    except ShareError as exc:
        if exc.code not in {"not_found", "member_audience_refused"}:
            raise
        handler._json({"error": str(exc)}, 404 if exc.code == "not_found" else 403)
    return True
