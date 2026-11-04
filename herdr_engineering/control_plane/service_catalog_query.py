"""Opt-in, bounded service catalog snapshots. Legacy service reads stay unchanged.

The factory supplies authorized, projected records. Cursors freeze their classification
and order for 30 seconds; they never cause another projection or truncate inventory.
"""

from __future__ import annotations

import base64
import hmac
import json
import math
import re
import secrets
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from datetime import UTC, datetime
from hashlib import sha256

SORT = "group-name-id-nfc-codepoint-v1"
STATES = ("current", "unavailable", "unknown", "archived")


class CatalogSnapshotExpired(ValueError):
    """HTTP 409: restart this query without its cursor."""

    code = "catalog_snapshot_expired"


def _stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (
            value if value >= 1e11 else value * 1000 if math.isfinite(value) and value > 0 else None
        )
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value, re.I
    ):
        return None
    try:
        return datetime.fromisoformat(value.upper().replace("Z", "+00:00")).timestamp() * 1000
    except (ValueError, OverflowError):
        return None


def _fresh(value, now, seconds=900):
    stamp = _stamp(value)
    return (
        stamp is not None
        and math.isfinite(stamp)
        and stamp <= now * 1000 + 5000
        and now * 1000 - stamp <= seconds * 1000
    )


def _scope(service):
    scope = (service.get("catalog") or {}).get("scope")
    return scope if scope in ("herdr", "system", "unassigned") else "unassigned"


def service_state(service, now):
    """Copy services-view.js serviceState precedence; ``now`` is epoch seconds."""
    lifecycle = service.get("lifecycle") or {}
    if lifecycle.get("archived") or lifecycle.get("state") == "archived":
        return "archived"
    status = service.get("status")
    status = status.lower() if isinstance(status, str) else ""
    if (
        status in ("unavailable", "stopped", "dead", "failed")
        or lifecycle.get("state") == "unavailable"
    ):
        return "unavailable"
    if lifecycle.get("state") == "unknown":
        return "unknown"
    observation = (service.get("catalog") or {}).get("metadata_observation")
    if observation is None:
        observation = service.get("metadata_observation")
    if (
        observation is not None
        and observation is not False
        and observation != 0
        and observation != ""
    ):
        observation = observation if isinstance(observation, dict) else {}
        seconds = observation.get("stale_after_seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            seconds = 90
        if observation.get("state") in ("missing", "unconfirmed") or (
            _scope(service) == "herdr"
            and not (
                observation.get("state") == "observed"
                and _fresh(observation.get("observed_at"), now, seconds)
            )
        ):
            return "unknown"
    if lifecycle.get("state") == "current" or status in (
        "healthy",
        "running",
        "active",
        "available",
    ):
        last_seen = lifecycle.get("last_seen_at")
        return (
            "current"
            if _fresh(service.get("updated_at") if last_seen is None else last_seen, now)
            else "unknown"
        )
    return "unknown"


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _identity(service, key):
    return _text((service.get("catalog") or {}).get(key)) or _text(service.get(key))


def _host(service):
    return _text(service.get("host_id")) or "Unknown host"


def _group(service, by):
    label = _text((service.get("catalog") or {}).get("label")) or "Unclassified service"
    if by == "host":
        return "host:" + _host(service), _host(service)
    if by == "type":
        return "type:" + label, label
    project, repository = _identity(service, "project_id"), _identity(service, "repository_id")
    label = project or repository or "No verified project · " + label
    return (
        "project:" if project else "repository:" if repository else "unassigned:"
    ) + label, label


def _search_text(service):
    catalog = service.get("catalog") or {}
    values = [
        service.get(key)
        for key in ("service_name", "description", "owner", "host_id", "port", "service_id")
    ]
    values += [catalog.get(key) for key in ("source_port", "project", "label", "purpose")]
    values += [_identity(service, key) for key in ("project_id", "repository_id", "commit_sha")]
    return " ".join(str(value) for value in values if value is not None).lower()


def _nfc(value):
    return unicodedata.normalize("NFC", str(value or ""))


class ServiceCatalogPager:
    def __init__(
        self, *, now=None, max_snapshots=16, ttl=30, max_bytes=64 * 1024 * 1024, max_rows=100_000
    ):
        if (
            any(
                type(value) is not int or value <= 0
                for value in (max_snapshots, max_bytes, max_rows)
            )
            or not math.isfinite(ttl)
            or ttl <= 0
        ):
            raise ValueError("Catalog snapshot budgets and TTL must be positive")
        self._now = now or time.time
        self._max_snapshots, self._ttl = max_snapshots, ttl
        self._max_bytes, self._max_rows = max_bytes, max_rows
        self._snapshots = OrderedDict()
        self._bytes = 0
        self._rows = 0
        self._key = secrets.token_bytes(32)
        self._lock = threading.RLock()

    def _cursor(self, snapshot_id, offset, query):
        query_hash = sha256(json.dumps(query, ensure_ascii=True).encode()).hexdigest()
        payload = json.dumps([snapshot_id, offset, query_hash], separators=(",", ":")).encode()
        return (
            base64.urlsafe_b64encode(payload + hmac.digest(self._key, payload, "sha256"))
            .decode()
            .rstrip("=")
        )

    def _read_cursor(self, cursor, query):
        try:
            if (
                not isinstance(cursor, str)
                or len(cursor) > 16000
                or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
            ):
                raise ValueError()
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._key, payload, "sha256")):
                raise ValueError()
            snapshot_id, offset, saved_query = json.loads(payload)
            if (
                saved_query != sha256(json.dumps(query, ensure_ascii=True).encode()).hexdigest()
                or type(offset) is not int
                or offset <= 0
            ):
                raise ValueError()
            return snapshot_id, offset
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError(
                "Invalid catalog cursor or changed query; restart without a cursor"
            ) from exc

    def _drop(self, snapshot_id):
        _, body, count = self._snapshots.pop(snapshot_id)
        self._bytes -= len(body)
        self._rows -= count

    def page(
        self,
        rows_factory,
        *,
        scope,
        status,
        include_archived,
        search,
        host_id,
        group_by,
        limit=100,
        cursor=None,
    ):
        if (
            scope not in ("herdr", "system", "unassigned")
            or status not in (*STATES, "all")
            or group_by not in ("project", "host", "type")
        ):
            raise ValueError("Invalid catalog scope, status or group_by")
        if type(limit) is not int or not 1 <= limit <= 500 or type(include_archived) is not bool:
            raise ValueError("Catalog limit must be 1..500 and include_archived must be boolean")
        if (
            not isinstance(search, str)
            or len(search) > 2000
            or (host_id is not None and (not isinstance(host_id, str) or len(host_id) > 300))
        ):
            raise ValueError("Catalog search/host must be bounded text (2000/300 characters)")
        search = " ".join(search.lower().split())
        query = (scope, status, include_archived, search, host_id or "", group_by, limit, SORT)
        with self._lock:
            now = self._now()
            for key, (created, _, _) in list(self._snapshots.items()):
                if now - created >= self._ttl:
                    self._drop(key)
            if cursor is not None:
                snapshot_id, offset = self._read_cursor(cursor, query)
                if snapshot_id not in self._snapshots:
                    raise CatalogSnapshotExpired(
                        "Catalog snapshot expired or was evicted; restart without a cursor"
                    )
                result = json.loads(self._snapshots[snapshot_id][1])
                if offset >= len(result["services"]) or offset % limit:
                    raise ValueError("Invalid catalog cursor position")
            else:
                rows = rows_factory()
                if len(rows) > self._max_rows:
                    raise ValueError(
                        "Catalog snapshot exceeds row budget; reduce inventory or increase the configured snapshot budget"
                    )
                # Round-trip freezes nested projections without retaining caller-owned dictionaries.
                scoped = [r for r in rows if _scope(r) == scope]
                counts = {state: 0 for state in STATES}
                hosts = Counter()
                selected, groups = [], {}
                terms = search.split()
                for service in scoped:
                    state = service_state(service, now)
                    counts[state] += 1
                    hosts[_host(service)] += 1
                    if (
                        (state == "archived" and not include_archived)
                        or (status != "all" and state != status)
                        or (host_id and _host(service) != host_id)
                    ):
                        continue
                    if terms and not all(term in _search_text(service) for term in terms):
                        continue
                    key, label = _group(service, group_by)
                    group = groups.setdefault(
                        key, dict(key=key, label=label, total=0, states={s: 0 for s in STATES})
                    )
                    group["total"] += 1
                    group["states"][state] += 1
                    selected.append(service)
                selected.sort(
                    key=lambda s: (
                        _nfc(_group(s, group_by)[1]),
                        _nfc(_group(s, group_by)[0]),
                        _group(s, group_by)[0],
                        _nfc(s.get("service_name")),
                        _nfc(s.get("service_id")),
                        str(s.get("service_id") or ""),
                    )
                )
                counts.update(
                    scope_total=len(scoped),
                    history_total=len(scoped) - counts["current"],
                    matching_total=len(selected),
                    matching_groups=len(groups),
                )
                snapshot_id = secrets.token_urlsafe(18)
                result = dict(
                    services=selected,
                    as_of=datetime.fromtimestamp(now, UTC).isoformat(),
                    snapshot_id=snapshot_id,
                    sort=SORT,
                    counts=counts,
                    facets={
                        "hosts": [
                            {"value": host, "count": count}
                            for host, count in sorted(
                                hosts.items(), key=lambda p: (_nfc(p[0]), p[0])
                            )
                        ]
                    },
                    groups=list(groups.values()),
                )
                encoded = json.dumps(
                    result, ensure_ascii=True, separators=(",", ":"), allow_nan=False
                ).encode()
                if len(encoded) > self._max_bytes:
                    raise ValueError(
                        "Catalog snapshot exceeds byte budget; reduce inventory or increase the configured snapshot budget"
                    )
                while self._snapshots and (
                    len(self._snapshots) >= self._max_snapshots
                    or self._bytes + len(encoded) > self._max_bytes
                    or self._rows + len(selected) > self._max_rows
                ):
                    self._drop(next(iter(self._snapshots)))
                self._snapshots[snapshot_id] = (now, encoded, len(selected))
                self._bytes += len(encoded)
                self._rows += len(selected)
                result = json.loads(encoded)
                offset = 0
            all_services = result["services"]
            result["services"] = all_services[offset : offset + limit]
            keys = {_group(s, group_by)[0] for s in result["services"]}
            result["groups"] = sorted(
                (g for g in result["groups"] if g["key"] in keys),
                key=lambda g: (_nfc(g["label"]), _nfc(g["key"]), g["key"]),
            )
            next_offset = offset + len(result["services"])
            more = next_offset < len(all_services)
            result["page"] = dict(
                limit=limit,
                returned=len(result["services"]),
                has_more=more,
                next_cursor=self._cursor(snapshot_id, next_offset, query) if more else None,
            )
            return result
