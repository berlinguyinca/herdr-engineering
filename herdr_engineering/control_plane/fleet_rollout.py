"""``fleet_rollout`` event model (spec docs/specs/herdr-fleet-rollout-watcher-20261104, section 8).

One append-only stream per rollout (``entity_id`` = ``rollout_id``). The model contains NO free text: every value is
a closed enum, an inventory host, a lowercase-hex id or digest, an integer, or a UTC timestamp, and unknown fields
are rejected. Secret-free is therefore by construction, not by redaction (decision 2026-11-05: regex redaction of log
text cannot be made safe). Logs stay on the host and are referenced by SHA-256 (``log``). The read model is derived
in memory from the streams, like ``repo.list_ai_sync``; no SQL table exists.
"""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import UTC, datetime

from .envelope import EnvelopeConflict

WATCHED_REPOS = ("autospec", "pi-engineering", "herdr-engineering", "ai-sync", "claude-config")
STATUSES = ("pending", "precheck", "applying", "checking", "ok", "blocked", "failed", "unreachable",
            "skipped", "rolled_back")
REASONS = ("dirty_checkout", "not_on_main", "unpublished_commits", "busy", "timeout", "health_failed",
           "apply_failed", "ssh_failed", "halted", "optional_unreachable", "not_published", "rate_limited",
           "ahead_of_target",
           "operator", "downgrade", "lock_invalid")
HOST_ORDER = ("beast", "bender", "macbook-pro")
#: Health checks the host-apply step runs (spec section 5): the only names a check may carry.
CHECK_NAMES = ("version_matches", "service_active", "gateway_registered", "parity_ok", "release_complete",
               "current_symlink", "version_smoke", "generation_receipt", "verify_ok", "feed_in_sync")
CHECK_STATUSES = ("ok", "failed", "skipped")
#: Which host-side log a reference points at; the content itself never leaves the host.
LOG_KINDS = ("updater", "installer", "healthcheck")
MAX_CHECKS = len(CHECK_NAMES)
COUNT_KEYS = ("dirty_paths", "unpublished_commits", "behind")

_HEX40 = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"[0-9a-f]{16}")
_LINK = re.compile(r"[0-9a-f]{16,32}")

#: event type -> (required fields, optional fields), beyond the common rollout_id / ts / seq.
EVENT_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "rollout.observed": (frozenset({"trigger", "targets"}), frozenset()),
    "rollout.planned": (frozenset({"trigger", "targets"}), frozenset()),
    "rollout.started": (frozenset({"targets"}), frozenset()),
    "rollout.resumed": (frozenset(), frozenset({"reason"})),
    "host.started": (frozenset({"host", "repo"}), frozenset({"status"})),
    "host.step": (frozenset({"host", "repo", "status"}), frozenset({"checks", "log"})),
    "host.finished": (frozenset({"host", "repo", "status"}),
                      frozenset({"reason", "before_sha", "after_sha", "started_at", "finished_at",
                                 "duration_ms", "checks", "log", "counts", "links"})),
    "rollout.succeeded": (frozenset(), frozenset()),
    "rollout.halted": (frozenset({"halted_at_host"}), frozenset({"reason"})),
    "rollout.superseded": (frozenset(), frozenset({"reason"})),
    "rollout.abandoned": (frozenset(), frozenset({"reason"})),
    "lock.pinned": (frozenset({"targets"}), frozenset()),
    "lock.refused": (frozenset({"targets"}), frozenset({"reason"})),
    "source.refused": (frozenset({"repo", "reason"}), frozenset({"trigger"})),
    "source.degraded": (frozenset({"repo", "reason"}), frozenset()),
    "source.recovered": (frozenset({"repo"}), frozenset()),
}
EVENT_TYPES = frozenset(EVENT_FIELDS)
_ROLLOUT_STATUS = {"rollout.observed": "observed", "rollout.planned": "planned", "rollout.started": "running",
                   "rollout.resumed": "running", "rollout.succeeded": "succeeded", "rollout.halted": "halted",
                   "rollout.superseded": "superseded", "rollout.abandoned": "abandoned"}


def event_id(rollout_id: str, seq: int) -> str:
    """Deterministic envelope identity, so an outbox replay can never double-count an event."""
    return f"{rollout_id}:{seq}"


def source_stream_id(repo: str) -> str:
    """Stream id for ``source.*`` events (not tied to one rollout): stable per watched repo."""
    return hashlib.sha256(f"source:{repo}".encode()).hexdigest()[:16]


def _bad(message: str) -> ValueError:
    return ValueError(f"fleet_rollout {message}")


def _enum(name: str, value, allowed) -> str:
    if value not in allowed:
        raise _bad(f"{name} must be one of {', '.join(allowed)}")
    return value


def _count(name: str, value) -> int:
    if type(value) is not int or not 0 <= value < 2**31:
        raise _bad(f"{name} must be a non-negative integer")
    return value


def _utc(name: str, value) -> str:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 40 else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise _bad(f"{name} must be an ISO-8601 UTC timestamp")
    return value


def _sha(name: str, value, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not _HEX40.fullmatch(value):
        raise _bad(f"{name} must be 40 lowercase hex characters")
    return value


def _host(name: str, value, nullable=False):
    if value is None and nullable:
        return None
    if value not in HOST_ORDER:
        raise _bad(f"{name} must be one of {', '.join(HOST_ORDER)}")
    return value


def _keys(name: str, value: dict, required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(value, dict):
        raise _bad(f"{name} must be an object")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise _bad(f"unknown {name} field: {unknown[0]}")
    missing = sorted(required - set(value))
    if missing:
        raise _bad(f"{name} requires {missing[0]}")


def _trigger(value) -> dict:
    _keys("trigger", value, {"repo", "sha"}, {"pr", "merged_at"})
    clean = {"repo": _enum("trigger.repo", value["repo"], WATCHED_REPOS), "sha": _sha("trigger.sha", value["sha"])}
    if "pr" in value:
        clean["pr"] = None if value["pr"] is None else _count("trigger.pr", value["pr"])
    if "merged_at" in value:
        clean["merged_at"] = None if value["merged_at"] is None else _utc("trigger.merged_at", value["merged_at"])
    return clean


def _targets(value) -> list:
    if not isinstance(value, list) or not 0 < len(value) <= len(WATCHED_REPOS):
        raise _bad(f"targets must be a list of 1 to {len(WATCHED_REPOS)} objects")
    out = []
    for item in value:
        _keys("target", item, {"repo", "sha"}, {"before"})
        clean = {"repo": _enum("target.repo", item["repo"], WATCHED_REPOS), "sha": _sha("target.sha", item["sha"])}
        if "before" in item:
            clean["before"] = _sha("target.before", item["before"], nullable=True)
        out.append(clean)
    return out


def _checks(value) -> list:
    if not isinstance(value, list) or len(value) > MAX_CHECKS:
        raise _bad(f"checks must be a list of at most {MAX_CHECKS} objects")
    out = []
    for item in value:
        _keys("check", item, {"name", "status"}, {"duration_ms"})
        clean = {"name": _enum("check name", item["name"], CHECK_NAMES),
                 "status": _enum("check status", item["status"], CHECK_STATUSES)}
        if "duration_ms" in item:
            clean["duration_ms"] = _count("check duration_ms", item["duration_ms"])
        out.append(clean)
    return out


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _log_ref(value) -> dict:
    """A pointer to a log kept on the host: which host and kind, a SHA-256 of the content, its size. No text."""
    _keys("log", value, {"host", "path_id", "sha256", "bytes", "lines"})
    if not isinstance(value["sha256"], str) or not _SHA256.fullmatch(value["sha256"]):
        raise _bad("log.sha256 must be 64 lowercase hex characters")
    return {"host": _host("log.host", value["host"]), "path_id": _enum("log.path_id", value["path_id"], LOG_KINDS),
            "sha256": value["sha256"], "bytes": _count("log.bytes", value["bytes"]),
            "lines": _count("log.lines", value["lines"])}


def validate_fleet_rollout(entity_id, event_type, payload) -> dict:
    """Strict allow-list for one rollout event; returns a clean copy of only the keys that were present."""
    if event_type not in EVENT_FIELDS:
        raise _bad(f"event_type must be one of {', '.join(sorted(EVENT_TYPES))}")
    if not isinstance(payload, dict):
        raise _bad("payload must be an object")
    required, optional = EVENT_FIELDS[event_type]
    _keys("payload", payload, {"rollout_id", "ts", "seq"} | required, optional)
    rid = payload["rollout_id"]
    if not isinstance(rid, str) or not _ID.fullmatch(rid):
        raise _bad("rollout_id must be 16 lowercase hex characters")
    if rid != entity_id:
        raise _bad("rollout_id must equal entity_id")
    clean = {"rollout_id": rid, "ts": _utc("ts", payload["ts"]), "seq": _count("seq", payload["seq"])}
    for key, value in payload.items():
        if key in clean:
            continue
        if key == "trigger":
            clean[key] = _trigger(value)
        elif key == "targets":
            clean[key] = _targets(value)
        elif key in ("repo", "host"):
            clean[key] = _enum(key, value, WATCHED_REPOS) if key == "repo" else _host(key, value)
        elif key == "status":
            clean[key] = _enum(key, value, STATUSES)
        elif key == "reason":
            clean[key] = _enum(key, value, REASONS)
        elif key in ("before_sha", "after_sha"):
            clean[key] = _sha(key, value, nullable=True)
        elif key in ("started_at", "finished_at"):
            clean[key] = _utc(key, value)
        elif key == "duration_ms":
            clean[key] = _count(key, value)
        elif key == "checks":
            clean[key] = _checks(value)
        elif key == "log":
            clean[key] = _log_ref(value)
        elif key == "counts":
            _keys("counts", value, set(), set(COUNT_KEYS))
            clean[key] = {k: _count(f"counts.{k}", v) for k, v in value.items()}
        elif key == "links":
            _keys("links", value, set(), {"pi_session_rollout"})
            link = value.get("pi_session_rollout")
            if link is not None and (not isinstance(link, str) or not _LINK.fullmatch(link)):
                raise _bad("links.pi_session_rollout must be a rollout id (16 to 32 lowercase hex characters)")
            clean[key] = dict(value)
        elif key == "halted_at_host":
            clean[key] = _host(key, value, nullable=True)
    if "log" in clean and clean["log"]["host"] != clean.get("host"):
        raise _bad("log.host must equal host")
    if event_type.startswith("source.") and rid != source_stream_id(clean["repo"]):
        raise _bad("source events must use the stream id of their repo")
    return clean


# --------------------------------------------------------------------------- idempotent write

_record_lock = threading.Lock()


def record_event(repo, entity_id: str, event_type: str, clean: dict) -> bool:
    """Append ``clean`` unless an event with the same ``seq`` is already stored.

    The same content again is a no-op (returns False); different content for a stored ``seq`` is a conflict,
    so a corrupt outbox cannot rewrite history. Returns True when the event was appended.
    """
    with _record_lock:
        for stored in repo.stream("fleet_rollout", entity_id):
            payload = {k: v for k, v in stored["payload"].items() if not k.startswith("_")}
            if payload.get("seq") == clean["seq"]:
                if payload == clean and stored["event_type"] == event_type:
                    return False
                raise EnvelopeConflict("seq is already used by a different fleet_rollout event",
                                       reason="sequence_conflict")
        repo.record_event("fleet_rollout", entity_id, event_type, clean)
        return True


# --------------------------------------------------------------------------- read model

def _events(repo, rid: str) -> list[dict]:
    """The stream as ``{"event_type", **payload}`` ordered by the emitter's seq."""
    rows = [{"event_type": e["event_type"], **{k: v for k, v in e["payload"].items() if not k.startswith("_")}}
            for e in repo.stream("fleet_rollout", rid)]
    return sorted(rows, key=lambda e: e["seq"])


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _host_rows(events: list[dict]) -> list[dict]:
    rows: dict[tuple[str, str], dict] = {}
    for e in events:
        if not e["event_type"].startswith("host."):
            continue
        row = rows.setdefault((e["host"], e["repo"]), {})
        row.pop("reason", None)
        row.update({k: v for k, v in e.items() if k not in ("event_type", "rollout_id", "seq", "ts")})
        row["updated_at"] = e["ts"]
    order = {h: i for i, h in enumerate(HOST_ORDER)}
    return sorted(rows.values(), key=lambda r: (order.get(r["host"], len(order)), r["host"], r["repo"]))


def _summary(rid: str, events: list[dict]) -> dict | None:
    levels = [e for e in events if e["event_type"] in _ROLLOUT_STATUS]
    if not levels:
        return None
    last = levels[-1]
    described = [e for e in levels if "trigger" in e]
    sized = [e for e in events if "targets" in e and e["event_type"].startswith("rollout.")]
    halted = last if last["event_type"] == "rollout.halted" else None
    counts: dict[str, int] = {}
    for row in _host_rows(events):
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "rollout_id": rid,
        "status": _ROLLOUT_STATUS[last["event_type"]],
        "trigger": described[-1]["trigger"] if described else None,
        "targets": sized[-1]["targets"] if sized else [],
        "created_at": events[0]["ts"],
        "updated_at": max((e["ts"] for e in events), key=_when),
        "halted_at_host": halted["halted_at_host"] if halted else None,
        "reason": last.get("reason") if last["event_type"] in ("rollout.halted", "rollout.abandoned") else None,
        "host_counts": counts,
        "event_count": len(events),
    }


def _summaries(repo) -> list[dict]:
    out = []
    for rid in repo.entity_ids("fleet_rollout"):
        summary = _summary(rid, _events(repo, rid))
        if summary:
            out.append(summary)
    return sorted(out, key=lambda s: (_when(s["updated_at"]), s["rollout_id"]), reverse=True)


def list_rollouts(repo, limit: int = 50, repo_name: str | None = None) -> list[dict]:
    rows = _summaries(repo)
    if repo_name is not None:
        rows = [s for s in rows if any(t["repo"] == repo_name for t in s["targets"])
                or (s["trigger"] or {}).get("repo") == repo_name]
    return rows[:max(1, min(limit, 200))]


def rollout_detail(repo, rid: str) -> dict | None:
    if not _ID.fullmatch(rid):
        return None
    events = _events(repo, rid)
    summary = _summary(rid, events)
    if summary is None:
        return None
    return {**summary, "hosts": _host_rows(events), "events": events}


def versions(repo) -> dict:
    """What each host runs (latest ok/rolled_back host.finished per host x repo), the newest observed heads,
    the last lock pin and the state of each watched source."""
    hosts: dict[str, dict[str, tuple]] = {}
    heads: dict[str, tuple] = {}
    lock: tuple | None = None
    sources: dict[str, tuple] = {}
    for rid in repo.entity_ids("fleet_rollout"):
        for e in _events(repo, rid):
            kind, stamp = e["event_type"], (_when(e["ts"]), e["seq"])
            if kind == "host.finished" and e["status"] in ("ok", "rolled_back") and e.get("after_sha"):
                since = e.get("finished_at") or e["ts"]
                key = (_when(since), e["seq"])
                slot = hosts.setdefault(e["host"], {})
                if e["repo"] not in slot or key > slot[e["repo"]][0]:
                    slot[e["repo"]] = (key, {"sha": e["after_sha"], "since": since, "rollout_id": rid,
                                             "status": e["status"]})
            elif kind == "rollout.observed":
                for target in e["targets"]:
                    if target["repo"] not in heads or stamp > heads[target["repo"]][0]:
                        heads[target["repo"]] = (stamp, target["sha"])
            elif kind == "lock.pinned" and (lock is None or stamp > lock[0]):
                lock = (stamp, {"repos": {t["repo"]: t["sha"] for t in e["targets"]}, "pinned_at": e["ts"],
                                "rollout_id": rid})
            elif kind.startswith("source.") and (e["repo"] not in sources or stamp > sources[e["repo"]][0]):
                state = {"source.degraded": "degraded", "source.refused": "refused", "source.recovered": "ok"}[kind]
                entry = {"state": state, "since": e["ts"]}
                if "reason" in e:
                    entry = {"state": state, "reason": e["reason"], "since": e["ts"]}
                sources[e["repo"]] = (stamp, entry)
    order = {h: i for i, h in enumerate(HOST_ORDER)}
    return {
        "hosts": [{"host": h, "repos": {r: v[1] for r, v in sorted(hosts[h].items())}}
                  for h in sorted(hosts, key=lambda h: (order.get(h, len(order)), h))],
        "main_heads": {r: v[1] for r, v in sorted(heads.items())},
        "lock": lock[1] if lock else None,
        "sources": {r: v[1] for r, v in sorted(sources.items())},
    }
