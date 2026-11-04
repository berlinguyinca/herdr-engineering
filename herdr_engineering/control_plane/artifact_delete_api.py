"""Owner-only artifact delete API: tombstone (restorable), purge now, restore, and the owner's tombstone listing.

Routes (all under ``/api/v1/artifacts/``; none takes an artifact id in the path, so they cannot shadow ``{id}/...``):

* ``POST delete``  ``{"ids": [...], "mode": "tombstone"|"purge_now", "reason": str, "hold_days"?: number,
  "request_key"?: str, "confirm"?: "<count>", "override_legal_hold"?: bool}``. ``purge_now`` also needs ``confirm`` to
  be the decimal count of the distinct ids, so one stray request cannot purge. Answers one result per id.
* ``POST restore`` ``{"ids": [...], "request_key"?: str}`` brings tombstoned (not yet purged) artifacts back.
* ``GET tombstoned`` lists tombstoned artifacts; ``GET delete-access`` tells the caller whether it is an owner.

Who may call: only an *owner*. With API keys configured that is an ``operator`` key whose short digest (the one in the
principal ``api-key:<digest>:operator``) is listed in YAML ``control_plane.artifact_delete.owner_key_digests``; on an
open (keyless) listener the operator is the owner. Every other caller (other operators, creators, members, viewers) is
refused with a fixed 404 that does not depend on any id, so nothing is revealed. Creators and members keep ``retract``.

Any store failure is one fixed 503 body; the driver text stays in the server log.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from urllib.parse import urlsplit

from .artifact_lifecycle import DEFAULT_HOLD_DAYS, MAX_BULK, MAX_REASON
from .artifacts import ArtifactError

_LOG = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_DIGEST = re.compile(r"[0-9a-f]{16}")
_DELETE_FIELDS = frozenset({"ids", "mode", "reason", "hold_days", "request_key", "confirm", "override_legal_hold"})
_RESTORE_FIELDS = frozenset({"ids", "request_key"})
_MAX_HOLD_DAYS = 3650
_BAD = {"error": "invalid request"}
_UNAVAILABLE = {"error": "artifact store unavailable"}


def owner_digests_config(raw: object) -> frozenset[str]:
    """``control_plane.artifact_delete`` as a set of owner key digests; ``ValueError`` names the bad YAML key."""
    if raw is None:
        return frozenset()
    if not isinstance(raw, dict) or set(raw) - {"owner_key_digests"}:
        raise ValueError("control_plane.artifact_delete must be a mapping with only owner_key_digests")
    digests = raw.get("owner_key_digests") or []
    if not isinstance(digests, list) or not all(isinstance(d, str) and _DIGEST.fullmatch(d) for d in digests):
        raise ValueError("control_plane.artifact_delete.owner_key_digests must be a list of 16-hex-digit key digests")
    return frozenset(digests)


def is_owner(handler, server) -> bool:
    """True for an operator on an open listener, or an operator key whose digest the YAML lists as an owner."""
    if handler._role() != "operator":
        return False
    if not server._keys:
        return True
    supplied = handler.headers.get("X-API-Key", "")
    digest = hashlib.sha256(supplied.encode()).hexdigest()[:16]
    return any(secrets.compare_digest(digest, owner) for owner in getattr(server, "artifact_delete_owners", ()))


class _Bad(ValueError):
    pass


def _body(handler, allowed: frozenset[str]) -> dict:
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise _Bad("content type")
    try:
        value = handler._read_json(limit=256 * 1024)
    except ValueError:
        raise _Bad("body") from None
    if not isinstance(value, dict) or set(value) - allowed:
        raise _Bad("fields")
    return value


def _ids(value: object) -> list[str]:
    if (not isinstance(value, list) or not value or len(value) > MAX_BULK
            or not all(isinstance(i, str) and _ID.fullmatch(i) for i in value)):
        raise _Bad("ids")
    return list(dict.fromkeys(value))


def _request_key(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise _Bad("request_key")
    return value


def _parse_delete(body: dict) -> dict:
    ids = _ids(body.get("ids"))
    mode, reason = body.get("mode"), body.get("reason")
    if mode not in ("tombstone", "purge_now"):
        raise _Bad("mode")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise _Bad("reason")
    hold = body.get("hold_days", DEFAULT_HOLD_DAYS)
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0 <= hold <= _MAX_HOLD_DAYS:
        raise _Bad("hold_days")
    override = body.get("override_legal_hold", False)
    if not isinstance(override, bool) or (override and mode != "purge_now"):
        raise _Bad("override_legal_hold")
    if mode == "purge_now" and body.get("confirm") != str(len(ids)):
        raise _Bad("confirm")
    return {"ids": ids, "mode": mode, "reason": reason, "hold_days": float(hold), "override": override,
            "request_key": _request_key(body.get("request_key"))}


def _public(result: dict) -> dict:
    """A result row without storage keys: counts only."""
    row = {"artifact_id": result["artifact_id"], "result": result["result"], "reason": result.get("reason"),
           "replayed": bool(result.get("replayed"))}
    if "deleted" in result:
        row["objects_deleted"] = len(result["deleted"])
        row["objects_kept_shared"] = len(result["retained_shared"])
    if result.get("unchanged"):
        row["unchanged"] = True
    return row


def _summary(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    return counts


def route_delete(handler, server, parts, resource) -> bool:
    """Handle the four fixed routes; false for any other path."""
    if resource != "artifacts" or len(parts) != 5:
        return False
    name, method = parts[4], handler.command
    routes = {("delete", "POST"), ("restore", "POST"), ("tombstoned", "GET"), ("delete-access", "GET")}
    if (name, method) not in routes:
        return False
    owner = is_owner(handler, server)
    if name == "delete-access":
        handler._json({"owner": owner})
        return True
    if not owner:
        handler._deny(404, "not found")
        return True
    origin = handler.headers.get("Origin")
    if method == "POST" and origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                         or urlsplit(origin).scheme not in {"http", "https"}):
        handler._deny(403, "cross-origin artifact mutation refused")
        return True
    if method == "POST":
        try:
            body = _body(handler, _DELETE_FIELDS if name == "delete" else _RESTORE_FIELDS)
            args = _parse_delete(body) if name == "delete" else {
                "ids": _ids(body.get("ids")), "request_key": _request_key(body.get("request_key"))}
        except _Bad as exc:
            if str(exc) == "content type":
                handler._deny(415, "artifact requests require application/json")
            else:
                handler._json(_BAD, 400)
            return True
    if server.artifact_store is None:
        handler._json(_UNAVAILABLE, 503)
        return True
    actor = handler._resource_principal()
    try:
        if name == "tombstoned":
            handler._json({"artifacts": handler._artifact_call("list_tombstoned")})
        elif name == "restore":
            rows = [_public(r) for r in handler._artifact_call(
                "restore_many", args["ids"], actor=actor, request_key=args["request_key"])]
            _LOG.info("artifact restore by %s: %s", actor, _summary(rows))
            handler._json({"results": rows, "summary": _summary(rows)})
        else:
            rows = [_public(r) for r in handler._artifact_call(
                "delete_many", args["ids"], mode=args["mode"], actor=actor, reason=args["reason"],
                request_key=args["request_key"], hold_days=args["hold_days"],
                override_legal_hold=args["override"])]
            _LOG.info("artifact %s by %s: %s", args["mode"], actor, _summary(rows))
            handler._json({"mode": args["mode"], "results": rows, "summary": _summary(rows)})
    except ArtifactError:
        handler._json(_BAD, 400)
    except Exception:
        _LOG.exception("artifact delete store call failed")
        handler._json(_UNAVAILABLE, 503)
    return True
