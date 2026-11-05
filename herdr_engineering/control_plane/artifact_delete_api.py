"""Owner-only artifact delete API: tombstone (restorable), purge now, restore, and the owner's tombstone listing.

Routes (all under ``/api/v1/artifacts/``; none takes an artifact id in the path, so they cannot shadow ``{id}/...``):

* ``POST delete``  ``{"ids": [...], "mode": "tombstone"|"purge_now", "reason": str, "hold_days"?: number,
  "request_key"?: str, "confirm"?: "<count>", "override_legal_hold"?: bool}``. ``purge_now`` also needs ``confirm`` to
  be the decimal count of the distinct ids, so one stray request cannot purge. Answers one result per id.
* ``POST restore`` ``{"ids": [...], "request_key"?: str}`` brings tombstoned (not yet purged) artifacts back.
* ``GET tombstoned`` lists tombstoned artifacts; ``GET delete-access`` tells the caller whether it is an owner.

Who may call: only an *owner*. With API keys configured that is an ``operator`` key whose short digest (the one in the
principal ``api-key:<digest>:operator``) is listed in YAML ``control_plane.artifact_delete.owner_key_digests``. A keyless
listener has no keys to list: its operator is an owner only when YAML ``control_plane.artifact_delete.allow_keyless_owner``
is true (default false), because a keyless listener is reachable by DNS rebinding. Every other caller (other operators, creators, members, viewers) is
refused with a fixed 404 that does not depend on any id, so nothing is revealed. Creators and members keep ``retract``.

Any store failure is one fixed 503 body; the driver text stays in the server log.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from .artifact_lifecycle import DEFAULT_HOLD_DAYS, MAX_BULK, MAX_REASON
from .artifacts import ArtifactError

_LOG = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_DIGEST = re.compile(r"[0-9a-f]{16}")
_DELETE_FIELDS = frozenset({"ids", "mode", "reason", "hold_days", "request_key", "confirm", "override_legal_hold"})
_RESTORE_FIELDS = frozenset({"ids", "request_key", "override_legal_hold"})
_MAX_HOLD_DAYS = 3650
_KEYLESS_ACTOR = "keyless:operator"
_BAD = {"error": "invalid request"}
_UNAVAILABLE = {"error": "artifact store unavailable"}


class TombstoneCache:
    """The registry's tombstoned artifact ids, for request paths that must not query the store per row.

    Refreshed at most every ``ttl`` seconds and immediately on every delete/restore this process performs (``add`` /
    ``invalidate``); another process's delete is visible within ``ttl``. If the store cannot be asked, the last known
    set stays in force, and before the first successful load the failure propagates (the caller answers the fixed 503,
    so nothing is served unchecked)."""

    def __init__(self, ttl: float = 5.0, clock=time.monotonic) -> None:
        self._ttl, self._clock = ttl, clock
        self._lock = threading.Lock()
        self._ids: frozenset[str] | None = None
        self._loaded = 0.0
        self._dirty = True

    def contains(self, artifact_id: str, load) -> bool:
        with self._lock:
            now = self._clock()
            if self._ids is None or self._dirty or now - self._loaded > self._ttl:
                try:
                    self._ids, self._loaded, self._dirty = frozenset(load()), now, False
                except Exception:                       # noqa: BLE001 - keep the last answer, try again next call
                    _LOG.exception("tombstone cache refresh failed")
                    if self._ids is None:
                        raise
            return artifact_id in self._ids

    def add(self, ids) -> None:
        with self._lock:
            if self._ids is not None:
                self._ids = self._ids | frozenset(ids)

    def invalidate(self) -> None:
        with self._lock:
            self._dirty = True


class DeleteOwners(NamedTuple):
    digests: frozenset[str]
    allow_keyless: bool


def owner_digests_config(raw: object) -> DeleteOwners:
    """``control_plane.artifact_delete`` as owner key digests plus the keyless opt-in; ``ValueError`` names the bad key."""
    if raw is None:
        return DeleteOwners(frozenset(), False)
    if not isinstance(raw, dict) or set(raw) - {"owner_key_digests", "allow_keyless_owner"}:
        raise ValueError("control_plane.artifact_delete must be a mapping with only owner_key_digests and "
                         "allow_keyless_owner")
    digests = raw.get("owner_key_digests") or []
    if not isinstance(digests, list) or not all(isinstance(d, str) and _DIGEST.fullmatch(d) for d in digests):
        raise ValueError("control_plane.artifact_delete.owner_key_digests must be a list of 16-hex-digit key digests")
    keyless = raw.get("allow_keyless_owner", False)
    if not isinstance(keyless, bool):
        raise ValueError("control_plane.artifact_delete.allow_keyless_owner must be true or false")
    return DeleteOwners(frozenset(digests), keyless)


def is_owner(handler, server) -> bool:
    """An operator key whose digest the YAML lists, or the operator of a keyless listener that opted in."""
    if handler._role() != "operator":
        return False
    if not server._keys:
        return bool(getattr(server, "artifact_delete_allow_keyless_owner", False))
    supplied = handler.headers.get("X-API-Key", "")
    digest = hashlib.sha256(supplied.encode()).hexdigest()[:16]
    return any(secrets.compare_digest(digest, owner) for owner in getattr(server, "artifact_delete_owners", ()))


def _actor(handler, server) -> str:
    """The audit actor: the key principal, or ``keyless:operator`` so a keyless owner is not mistaken for a key."""
    return handler._resource_principal() if server._keys else _KEYLESS_ACTOR


def revoke_shares(handler, server, artifact_id: str, actor: str) -> int | None:
    """Revoke every pending or live share of the artifact and remove its bundle. Returns how many were revoked, or
    ``None`` when sharing could not be fully revoked (the delete stands; repeat it to retry)."""
    store, root = getattr(server, "share_store", None), getattr(server, "shares_root", None)
    if store is None or root is None:
        return 0
    from .artifact_share_api import _call
    from .share_bundle import remove_bundle, remove_state
    revoked = 0
    try:
        for share in _call(store, "list_for_artifact", artifact_id):
            if share.get("state") == "revoked":
                continue
            full = _call(store, "get", share["share_id"]) or share
            if full.get("token_hash"):
                remove_state(Path(root), full["token_hash"])        # first: the link stops working at once
            _call(store, "mark_revoked", share["share_id"], actor=actor)
            latest = _call(store, "get", share["share_id"]) or {}
            if latest.get("token_hash") and latest["token_hash"] != full.get("token_hash"):
                remove_state(Path(root), latest["token_hash"])
            remove_bundle(Path(root), share["share_id"])
            revoked += 1
    except Exception:                                   # noqa: BLE001 - reported as None, detail stays in the log
        _LOG.exception("revoking the shares of %s failed", artifact_id)
        return None
    return revoked


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
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 1 <= hold <= _MAX_HOLD_DAYS:
        raise _Bad("hold_days")
    override = body.get("override_legal_hold", False)
    if not isinstance(override, bool):
        raise _Bad("override_legal_hold")
    if mode == "purge_now" and body.get("confirm") != str(len(ids)):
        raise _Bad("confirm")
    return {"ids": ids, "mode": mode, "reason": reason, "hold_days": float(hold), "override": override,
            "request_key": _request_key(body.get("request_key"))}


def _public(result: dict) -> dict:
    """A result row without storage keys: counts only."""
    row = {"artifact_id": result["artifact_id"], "result": result["result"], "reason": result.get("reason"),
           "replayed": bool(result.get("replayed"))}
    if result.get("shares") is not None:
        row["shares_revoked"] = result["shares"]
    if "deleted" in result:
        row["objects_deleted"] = len(result["deleted"])
        row["objects_kept_shared"] = len(result["retained_shared"])
    if result.get("unchanged"):
        row["unchanged"] = True
    if result.get("warning"):
        row["warning"] = result["warning"]
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
        _LOG.warning("artifact delete route %s refused for a non-owner (%s)", name, _who(handler))
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
                "ids": _ids(body.get("ids")), "request_key": _request_key(body.get("request_key")),
                "override": body.get("override_legal_hold", False) is True}
            if name == "restore" and body.get("override_legal_hold", False) not in (True, False):
                raise _Bad("override_legal_hold")
        except _Bad as exc:
            if str(exc) == "content type":
                handler._deny(415, "artifact requests require application/json")
            else:
                handler._json(_BAD, 400)
            return True
    if server.artifact_store is None:
        handler._json(_UNAVAILABLE, 503)
        return True
    actor = _actor(handler, server)
    try:
        if name == "tombstoned":
            handler._json({"artifacts": handler._artifact_call("list_tombstoned")})
            return True
        if name == "restore":
            raw = handler._artifact_call("restore_many", args["ids"], actor=actor, request_key=args["request_key"],
                                         override_legal_hold=args["override"])
        else:
            raw = handler._artifact_call(
                "delete_many", args["ids"], mode=args["mode"], actor=actor, reason=args["reason"],
                request_key=args["request_key"], hold_days=args["hold_days"], override_legal_hold=args["override"])
    except ArtifactError:
        handler._json(_BAD, 400)
        return True
    except Exception:
        _LOG.exception("artifact delete store call failed")
        handler._json(_UNAVAILABLE, 503)
        return True
    if all(r["result"] == "error" for r in raw):            # nothing could be done: the store is down
        _LOG.error("artifact delete: every id failed in the store")
        handler._json(_UNAVAILABLE, 503)
        return True
    cache = getattr(server, "tombstone_cache", None)
    if cache is not None:                                   # the legacy read path sees the change at once
        cache.add([r["artifact_id"] for r in raw if r["result"] in ("tombstoned", "deleted", "already_purged")])
        cache.invalidate()
    if name == "delete":
        for row in raw:
            if row["result"] in ("tombstoned", "deleted", "already_purged"):
                row["shares"] = revoke_shares(handler, server, row["artifact_id"], actor)
    rows = [_public(r) for r in raw]
    _LOG.info("artifact %s by %s: %s", args.get("mode", "restore"), actor, _summary(rows))
    handler._json({**({"mode": args["mode"]} if name == "delete" else {}), "results": rows, "summary": _summary(rows)})
    return True


def _who(handler) -> str:
    supplied = handler.headers.get("X-API-Key", "")
    return "api-key:" + hashlib.sha256(supplied.encode()).hexdigest()[:16] if supplied else "no key"
