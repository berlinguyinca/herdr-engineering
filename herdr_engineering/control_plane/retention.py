"""Retention policy (Spec §96, §99). Expire high-volume, low-value history.

Telemetry samples and raw events are high-volume and are pruned after a TTL.
Structured entity records (missions, sessions, services, hosts, artifacts
metadata) and their event *streams* are durable and are never auto-deleted —
only telemetry and old raw event frames are aged out. Artifact *bytes* (the
in-memory RustFS stand-in) are pruned after a longer TTL.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections import deque

log = logging.getLogger(__name__)


def _parse(iso: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(iso)


def _prune_usage_projection(repo, cutoff, entity_type):
    """Drop mission-scoped usage projection records whose occurred_at predates the
    retention cutoff, so usage_analytics() never reports swept totals."""
    if entity_type == "token_usage":
        projection = getattr(repo, "_token_usage", None)
        if projection is None:
            return
        stale = [rid for rid, rec in projection.items()
                 if _parse(rec.get("occurred_at") or "") < cutoff]
        for rid in stale:
            projection.pop(rid, None)


def apply_retention(repo, *, telemetry_ttl_hours=24, events_ttl_days=30,
                    artifact_ttl_days=365, now=None):
    """Prune telemetry / old raw events / artifact bytes past their TTL.

    Returns a dict of counts removed. Uses the repo's wall clock unless `now`
    (an ISO timestamp) is given (for deterministic tests).
    """
    now_dt = _parse(now) if now else _dt.datetime.now(_dt.UTC)
    removed = {"telemetry": 0, "events": 0, "artifacts": 0}

    cutoff_tel = now_dt - _dt.timedelta(hours=telemetry_ttl_hours)
    for host_id, samples in list(repo._telemetry.items()):
        kept = [s for s in samples if _parse(s["at"]) >= cutoff_tel]
        removed["telemetry"] += len(samples) - len(kept)
        if kept:
            repo._telemetry[host_id] = kept
        else:
            repo._telemetry.pop(host_id, None)

    cutoff_ev = now_dt - _dt.timedelta(days=events_ttl_days)
    with repo._event_lock:
        survivors = {}
        for key, stream in list(repo._events.items()):
            kept = []
            for event in stream:
                keep = _parse(event["source_timestamp"]) >= cutoff_ev
                # Match occurrences, not offsets: two distinct durable events
                # may share a sequence and only one may have expired.
                survivors.setdefault((*key, event["sequence"]), deque()).append(keep)
                if keep:
                    kept.append(event)
            removed["events"] += len(stream) - len(kept)
            if kept:
                repo._events[key] = kept
            else:
                repo._events.pop(key, None)
        order = []
        for reference in repo._event_order:
            occurrences = survivors.get(reference)
            if occurrences and occurrences.popleft():
                order.append(reference)
        repo._event_order = order

    cutoff_art = now_dt - _dt.timedelta(days=artifact_ttl_days)
    for aid in list(repo._blobs.keys()):
        a = repo._artifacts.get(aid)
        if a is None or _parse(a["created_at"]) < cutoff_art:
            repo._blobs.pop(aid, None)
            removed["artifacts"] += 1

    return removed


# ------------------------------------------------------------ token usage
# LLM page design (docs/superpowers/specs/2026-10-23-llm-page-design.md): raw usage
# records are kept ``raw_retention_days``; hourly rollups (slice 2) are kept
# ``rollup_retention_days``. Both live in ``control_plane.llm_proxy`` (YAML).

DEFAULT_RAW_RETENTION_DAYS = 14
DEFAULT_ROLLUP_RETENTION_DAYS = 400
USAGE_ENTITY_TYPE = "token_usage"


def usage_retention_config(mapping) -> dict:
    """Validated retention settings from ``control_plane.llm_proxy`` (None = defaults).

    Raises ValueError naming the bad key; days must be whole numbers >= 1.
    """
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        raise ValueError("control_plane.llm_proxy must be a mapping")
    out = {}
    for key, default in (("raw_retention_days", DEFAULT_RAW_RETENTION_DAYS),
                         ("rollup_retention_days", DEFAULT_ROLLUP_RETENTION_DAYS)):
        value = mapping.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"control_plane.llm_proxy.{key} must be a whole number of days >= 1")
        out[key] = value
    return out


def sweep_token_usage(repo, *, raw_retention_days, now=None) -> int:
    """Drop token_usage records older than ``raw_retention_days``, in memory and in
    the durable event log; no other entity type is touched. Returns records removed."""
    if isinstance(raw_retention_days, bool) or not isinstance(raw_retention_days, int) \
            or raw_retention_days < 1:
        raise ValueError("raw_retention_days must be a whole number of days >= 1")
    now_dt = _parse(now) if now else _dt.datetime.fromtimestamp(repo._now(), _dt.UTC)
    cutoff = now_dt - _dt.timedelta(days=raw_retention_days)
    removed = 0
    with repo._event_lock:
        survivors = {}
        for key in [k for k in repo._events if k[0] == USAGE_ENTITY_TYPE]:
            stream = repo._events[key]
            kept = []
            for event in stream:
                keep = _parse(event["source_timestamp"]) >= cutoff
                survivors.setdefault((*key, event["sequence"]), deque()).append(keep)
                if keep:
                    kept.append(event)
            removed += len(stream) - len(kept)
            if kept:
                repo._events[key] = kept
            else:
                repo._events.pop(key, None)
        if removed:
            order = []
            for reference in repo._event_order:
                if reference[0] != USAGE_ENTITY_TYPE:
                    order.append(reference)
                    continue
                occurrences = survivors.get(tuple(reference))
                if occurrences and occurrences.popleft():
                    order.append(reference)
            repo._event_order = order
    # The mission-scoped usage projection (issue #36) is keyed by request_id, so
    # its records are not the same rows just swept; prune them by occurred_at so
    # usage_analytics() never reports totals the retention window already dropped.
    _prune_usage_projection(repo, cutoff, USAGE_ENTITY_TYPE)
    # Durable I/O outside the event lock; without it a restart would re-hydrate them.
    prune = getattr(getattr(repo, "_sink", None), "prune_events_before", None)
    if callable(prune):
        try:
            prune(USAGE_ENTITY_TYPE, cutoff)
        except Exception as exc:  # retried on the next sweep; never breaks serving
            log.warning("durable token_usage prune failed: %s", type(exc).__name__)
    return removed
