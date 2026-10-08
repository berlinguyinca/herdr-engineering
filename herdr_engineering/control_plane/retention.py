"""Retention policy (Spec §96, §99). Expire high-volume, low-value history.

Telemetry samples and raw events are high-volume and are pruned after a TTL.
Structured entity records (missions, sessions, services, hosts, artifacts
metadata) and their event *streams* are durable and are never auto-deleted —
only telemetry and old raw event frames are aged out. Artifact *bytes* (the
in-memory RustFS stand-in) are pruned after a longer TTL.
"""
from __future__ import annotations

import datetime as _dt


def _parse(iso: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(iso)


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
    for key, stream in list(repo._events.items()):
        kept = [e for e in stream if _parse(e["source_timestamp"]) >= cutoff_ev]
        removed["events"] += len(stream) - len(kept)
        if kept:
            repo._events[key] = kept
        else:
            repo._events.pop(key, None)
    # drop chronological index entries pointing at pruned events
    repo._event_order = [(t, i, s) for (t, i, s) in repo._event_order
                         if (t, i) in repo._events and
                         s < len(repo._events[(t, i)])]

    cutoff_art = now_dt - _dt.timedelta(days=artifact_ttl_days)
    for aid in list(repo._blobs.keys()):
        a = repo._artifacts.get(aid)
        if a is None or _parse(a["created_at"]) < cutoff_art:
            repo._blobs.pop(aid, None)
            removed["artifacts"] += 1

    return removed
