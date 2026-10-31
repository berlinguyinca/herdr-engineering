"""Durable pull-request trails for the learning loop.

When a pull request ends (merged or closed, with a merge/close time on its record) one bounded ``PullRequestTrail`` event is written
on the ``pr_trail`` stream of that PR, assembled from what the control plane already holds (the PR trail of ``pr_trail`` and, for each
evidence-linked agent, the ``learning_record`` of ``agent_evolution``). It survives the 24 h live window, so a training pipeline can pull
it later. Rules, the same as the two builders it reads:

* Every figure is ``{"value": ..., "reason": ...}``; a source that could not be read, was truncated or is undated leaves ``null`` and
  the reason. Nothing is estimated here.
* **A frozen record never holds nulls that a cold start or a transient outage caused.** A record is assembled only when its critical
  sources (CI, handoffs, reviews, plan reviews, and the same for each embedded agent) are readable, and not before the process has been up
  for ``warmup_minutes``. Until then the PR is retried every ``retry_interval_minutes``; after ``ready_retry_minutes`` it is stored anyway
  with ``incomplete: true`` and ``missing_sources`` naming what was missing, so a permanent gap (no CI configured) is stated, not hidden.
* One record per end (``end_seq``, the PR record's ``end_count``): a reopened PR that ends again gets a new record, a corrected end time
  does not. A record is never rewritten. Later changes are ``PullRequestTrailLateEvidence`` events: a provider-reported end time, and
  signals that were null at assembly and became known (a bounded number of re-checks inside a window), each deduplicated by a digest.
* Only an allowlist is stored: ids, states, counts, timestamps, reasons, and a few short text fields (branch names), clipped with a visible
  marker. Goals, review text, reviewer and agent names, CI URLs are not stored. Every string and every key is redacted (secret patterns and
  key hints), e-mail addresses and home-directory paths are scrubbed, once, at write time, so the JSON and JSONL exports are identical.
* A record whose observed end time is old by the time it is assembled is ``end_time_confidence: "low"`` and the signals built on the end
  time are withheld (null with the reason), because the evidence has moved on since.
* Assembly is a sweep, never inside the PR upsert, and never holds the lock while it builds a trail. Export reads an in-memory index
  and copies only the page. A single control-plane process is assumed; the sequence counter refuses to write if the stored records are
  ahead of it.
"""
from __future__ import annotations

import bisect
import copy
import datetime as dt
import hashlib
import json
import logging
import re
import threading
import unicodedata
from urllib.parse import quote, unquote

from .project_links import canonical_repository
from .redact import redact_payload, redact_text

log = logging.getLogger(__name__)

EVENT = "PullRequestTrail"
LATE = "PullRequestTrailLateEvidence"
ENTITY = "pr_trail"
SCHEMA = 1
MAX_BYTES = 131072
SWEEP_EVERY = 300
_TEXT = 300
_BRANCH = 200
_CLIP = "…[truncated]"
_ENDED = frozenset(("merged", "closed"))
_LATE_KEYS = frozenset(("merged_at", "closed_at", "ended_source", "note", "signals", "digest"))
_CURSOR = re.compile(r"^s[0-9]{1,15}$")
_CRITICAL_TRAIL = ("ci", "handoffs", "reviews", "plan_reviews")
_CRITICAL_AGENT = ("ci", "handoffs", "reviews", "plan_reviews", "event_stream")
_END_TIME_SIGNALS = ("duration_s", "ci_green_first_try", "ci_reruns", "merged_without_rework")
_TRIM_ORDER = ("ci_runs", "handoffs", "reviews", "ambiguous", "sessions", "agents")
_AGENT_SCALARS = ("session_id", "harness", "model", "role", "started_at", "ended_at", "duration_s", "cost_usd_estimate", "outcome")
_UNBUILDABLE_CAP = 5000
# Every pattern here is linear on its input: the input is capped first (``_MAX_IN``), the local part and the labels are bounded, and a match
# attempt can only start where the lookbehind allows it.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])(?!git@)[A-Za-z0-9._%+\-]{1,64}@"
    r"(?:(?:[A-Za-z0-9\-]{1,63}\.){1,8}[A-Za-z]{2,24}|localhost)(?![A-Za-z0-9]|:[A-Za-z0-9_~./\-])", re.IGNORECASE)
_HOME = re.compile(r"(/home/|/Users/|[A-Za-z]:[\\/]Users[\\/])[/\\ ]{0,4}([^/\\\s\"'&?#]{1,128})", re.IGNORECASE)
_ROOT = re.compile(r"(?<![\w.-])/root(?=/|\s|$|[\"'])(?:/[^\s\"']{0,512})?")
_TILDE = re.compile(r"(?<![\w/~.-])~[A-Za-z_][\w.-]{0,64}")
_AT = re.compile(r"\s{0,3}[(\[{]\s{0,3}at\s{0,3}[)\]}]\s{0,3}", re.IGNORECASE)
_DOT = re.compile(r"\s{0,3}[(\[{]\s{0,3}dot\s{0,3}[)\]}]\s{0,3}", re.IGNORECASE)
_MAX_IN = 4096
_ROLES = ("operator", "viewer")

_DEFAULTS = {"enabled": True, "max_agents": 12, "sweep_batch": 20, "page_default": 50, "page_max": 200, "max_records": 20000,
             "require_role": "operator", "warmup_minutes": 10, "retry_interval_minutes": 5, "ready_retry_minutes": 60,
             "low_confidence_after_minutes": 15, "late_checks": 3, "late_window_hours": 24, "late_check_interval_minutes": 30, "max_build_attempts": 12,
             "unbuildable_retry_hours": 24}
_RANGES = {"max_agents": (1, 50), "sweep_batch": (1, 100), "page_default": (1, 500), "page_max": (1, 500), "max_records": (100, 1_000_000),
           "warmup_minutes": (0, 1440), "retry_interval_minutes": (1, 1440), "ready_retry_minutes": (0, 1440),
           "low_confidence_after_minutes": (1, 10080), "late_checks": (0, 20), "late_window_hours": (1, 720),
           "late_check_interval_minutes": (1, 1440), "max_build_attempts": (1, 1000), "unbuildable_retry_hours": (1, 720)}


def learning_config(raw):
    """Validate ``control_plane.learning`` (YAML only); raises ValueError naming the key."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("control_plane.learning must be a mapping")
    extra = sorted(set(raw) - set(_DEFAULTS))
    if extra:
        raise ValueError(f"control_plane.learning has unknown keys: {', '.join(extra)}")
    out = {**_DEFAULTS, **raw}
    if not isinstance(out["enabled"], bool):
        raise ValueError("control_plane.learning.enabled must be true or false")
    if out["require_role"] not in _ROLES:
        raise ValueError(f"control_plane.learning.require_role must be one of: {', '.join(_ROLES)}")
    for key, (low, high) in _RANGES.items():
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"control_plane.learning.{key} must be a whole number from {low} to {high}")
    if out["page_default"] > out["page_max"]:
        raise ValueError("control_plane.learning.page_default must not exceed page_max")
    return out


def _parse(stamp):
    try:
        value = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=dt.UTC)).timestamp()


def _scrub_once(text):
    text = redact_text(text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _ROOT.sub("/root/[USER]", text)
    text = _TILDE.sub("~[USER]", text)
    return _HOME.sub(lambda m: m.group(1) + "[USER]", text)


def _strip(text):
    """NFKC, then without format and control characters (zero-width, bidi overrides, soft hyphens) that would hide a match."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c in "\t\n\r" or unicodedata.category(c) not in ("Cf", "Cc"))


def _deobfuscate(text):
    return _DOT.sub(".", _AT.sub("@", text))


def _decode(text):
    """Percent-decoding, repeated (case-insensitive hex) up to three rounds."""
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _variants(part):
    once = quote(part, safe="")
    twice = quote(once, safe="")
    lower = lambda v: re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), v)  # noqa: E731
    return sorted({part, once, twice, lower(once), lower(twice)}, key=len, reverse=True)


def _sensitive(view):
    """The parts of ``view`` that ``_scrub_once`` would replace, with their marker: usernames, addresses, root paths, ``~user``."""
    found = []
    for m in _EMAIL.finditer(view):
        found.append((m.group(0), "[EMAIL]"))
    for m in _HOME.finditer(view):
        found.append((m.group(2), "[USER]"))
    for m in _ROOT.finditer(view):
        found.append((m.group(0), "/root/[USER]"))
    for m in _TILDE.finditer(view):
        found.append((m.group(0), "~[USER]"))
    return found


def scrub(text):
    """Secret patterns, e-mail addresses (also ``a%40b.com``, fullwidth ``＠``, ``a@localhost``, ``a (at) b.com``) and home-directory paths
    (``/home/u``, ``/Users/u``, ``/root``, ``~u``, ``C:\\\\Users\\\\u``, also percent-encoded up to three rounds) out of one string.

    The input is cut to 4 KiB first (every caller clips far below that and marks it), then NFKC-normalised and stripped of format and control
    characters. Version strings (``lodash@4.17.21``), ``feat@v2`` and ssh remotes (``git@github.com:org/repo``) are not addresses. Text that
    needs nothing is returned verbatim. When only the percent-decoded form exposes something, the encoded text is scrubbed in place (the
    sensitive part's encoded forms are replaced), so a URL keeps its shape and joins on it keep working."""
    original = str(text)[:_MAX_IN]
    base = _strip(original)
    probe = _deobfuscate(base)
    cleaned = _scrub_once(probe)
    view = _decode(probe)
    if view != probe:
        exposed = _scrub_once(view)
        if exposed != view:
            if cleaned != probe:
                return cleaned
            replaced = probe
            for part, marker in sorted(_sensitive(view), key=lambda p: len(p[0]), reverse=True):
                for variant in _variants(part):
                    if variant:
                        replaced = replaced.replace(variant, marker)
            check = _decode(replaced)
            return replaced if _scrub_once(check) == check else exposed
    return original if cleaned == probe else cleaned[:_MAX_IN]          # text that needed nothing is kept verbatim


def _clean(value, trimmed, name="", depth=0):
    """Key-hint masking first (``{"password": ...}``), then every string and every key through ``scrub``, bounded in depth and length.
    A string or key that had to be cut is clipped with a visible marker and named in ``trimmed`` (``text:<field>``)."""
    if depth == 0:
        value = redact_payload(value)
    if depth > 8:
        trimmed.add("depth")
        return None

    def clip(text, limit, label):
        text = scrub(text)
        if len(text) > limit:
            trimmed.add(f"text:{label}" if label else "text")
            return text[:limit - len(_CLIP)] + _CLIP
        return text
    if isinstance(value, str):
        return clip(value, _TEXT, name)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = clip(str(k), 80, "key")
            out[key] = _clean(v, trimmed, key, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_clean(v, trimmed, name, depth + 1) for v in value]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return clip(str(value), _TEXT, name)


def parse_repository(value):
    """``(host|None, owner, name)`` of a PR record's repository, or None when it is not ``owner/name`` or ``host/owner/name``."""
    if not isinstance(value, str):
        return None
    canonical = canonical_repository(value)
    parts = (canonical or value.strip()).strip("/").split("/")
    if canonical and len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if not canonical and len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", p) for p in parts):
        return None, parts[0], parts[1]
    return None


def _size(value):
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


class TrailRecords:
    """Assembles, stores and exports pull-request trails. ``trail_for(host, owner, name, number)`` returns a ``pr_trail`` body (or None),
    ``evolution_for(session_id)`` an ``agent_evolution`` body (or None) and ``ci_configured()`` whether a CI provider is configured at
    all; all three are supplied by the server.

    Locks: ``_write_lock`` serialises writers (and is held across a durable write that may wait for the sink); ``_lock`` guards the
    in-memory index and is only ever held briefly, so a reader never waits for a slow sink. Order: ``_write_lock`` then ``_lock``."""

    def __init__(self, repo, config, *, trail_for=None, evolution_for=None, ci_configured=None, now=None):
        self.repo, self.config = repo, config
        self.trail_for, self.evolution_for, self.ci_configured = trail_for, evolution_for, ci_configured
        self._now = now or repo._now
        self._started = self._now()
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._sweep_guard = threading.Lock()
        self._worker = None
        self._closed = False
        self._swept = 0.0
        self._index_ready = False
        self._seq = -1
        self._seqs: list[int] = []
        self._rows: list[tuple] = []          # (learning_seq, pr_id, event_type, payload, assembled_ts, ended_ts), ascending by seq
        self._records: dict = {}              # (pr_id, end_seq) -> payload; only the retained (exportable) records
        self._ever: set = set()               # (pr_id, end_seq) ever assembled; pruning never removes a key, restart rebuilds it
        self._late_keys: set = set()          # (pr_id, "reported:<time>" | "digest:<hex>")
        self._pruned_through = -1
        self._horizon = None                  # the latest end time among pruned records: an older end is not assembled again
        self._track: dict = {}                # key -> {"first", "retry_at", "last", "none"}; only keys of PRs still waiting
        self._unbuildable: dict = {}          # key -> {reason, since, retry_at}; trail not readable in max_build_attempts tries; retried slowly
        self._skipped_horizon = 0             # PRs the last scan left out because their end is older than the retention horizon
        self._next_due = float('inf')         # when the earliest postponed or unbuildable PR is due again
        self._idle_version, self._idle_until = None, 0.0   # the last scan found nothing due: idle while the PRs and the clock stand still
        self._late_state: dict = {}

    @property
    def enabled(self):
        return bool(self.config["enabled"]) and not self._closed

    def close(self):
        """Stop starting work and wait briefly for a background sweep that is running."""
        self._closed = True
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2)

    def _min(self, key):
        return float(self.config[key]) * 60

    # ------------------------------------------------------------------ index
    def _ensure_index(self):
        """Built once, from the stored events (no copies); afterwards updated on every write. The counter is the highest stored seq.
        The retention limit is applied at once, so ``truncated_before`` is the same after a restart."""
        if self._index_ready:
            return
        with self._lock:
            if self._index_ready:
                return
            rows = []
            with self.repo._event_lock:
                streams = [(key[1], list(stream)) for key, stream in self.repo._events.items() if key[0] == ENTITY]
            for pr_id, events in streams:
                for event in events:
                    payload = event.get("payload") or {}
                    seq = payload.get("learning_seq")
                    if event.get("event_type") in (EVENT, LATE) and isinstance(seq, int) and not isinstance(seq, bool):
                        rows.append((seq, pr_id, event["event_type"], payload))
            rows.sort(key=lambda r: r[0])
            self._rows, self._seqs, self._records, self._ever, self._late_keys = [], [], {}, set(), set()
            for seq, pr_id, event_type, payload in rows:
                self._add(seq, pr_id, event_type, payload)
            self._seq = self._seqs[-1] if self._seqs else -1
            self._index_ready = True
            self._prune_locked()

    def _add(self, seq, pr_id, event_type, payload):
        """Index one stored event. Caller holds ``_lock``."""
        if event_type == EVENT:
            ended = _parse(payload.get("ended_at"))
            self._records[(pr_id, payload.get("end_seq"))] = payload
            self._ever.add((pr_id, payload.get("end_seq")))
        else:
            parent = self._records.get((pr_id, payload.get("end_seq")))
            ended = _parse((parent or {}).get("ended_at"))
            evidence = payload.get("evidence") or {}
            if evidence.get("ended_at_reported"):
                self._late_keys.add((pr_id, f"reported:{evidence['ended_at_reported']}"))
            if evidence.get("digest"):
                self._late_keys.add((pr_id, f"digest:{evidence['digest']}"))
        self._seqs.append(seq)
        self._rows.append((seq, pr_id, event_type, payload, _parse(payload.get("assembled_at")) or 0.0, ended))

    def _write(self, pr_id, payload, event_type):
        """Allocate the next seq and record the event under the writer lock; the counter and the index move only if the event was recorded.
        The durable write (which may wait for the sink) holds the writer lock but not the index lock."""
        with self._write_lock:
            if self._closed:
                return None
            self._ensure_index()
            with self._lock:
                if self._seqs and self._seqs[-1] > self._seq:
                    log.warning("learning trail counter is behind the stored records; refusing to write")
                    return None
                seq = self._seq + 1
            payload = {**payload, "learning_seq": seq}
            sink = self.repo._sink
            durable = sink is not None and callable(getattr(sink, "record_required", None))
            try:
                event = self.repo.record_event(ENTITY, pr_id, event_type, payload, feed=False, require_durable=durable)
            except Exception as exc:  # noqa: BLE001 - a durable write that was refused is retried on the next pass
                log.warning("learning trail write failed: %s", type(exc).__name__)
                return None
            if not event:
                return None
            with self._lock:
                self._seq = seq
                self._add(seq, pr_id, event_type, payload)
            return payload

    def _has_record(self, pr_id, end_seq):
        """Whether this end was ever assembled (pruned records included)."""
        self._ensure_index()
        with self._lock:
            return (pr_id, end_seq) in self._ever

    def _record_for(self, pr_id, end_seq):
        self._ensure_index()
        with self._lock:
            return self._records.get((pr_id, end_seq))

    # ------------------------------------------------------------------ assembly
    @staticmethod
    def _signal(entry):
        """``{value, reason}`` of one agent figure: a scalar value or null; anything else is dropped."""
        if not isinstance(entry, dict):
            return {"value": None, "reason": "not recorded"}
        value = entry.get("value")
        if not (value is None or isinstance(value, bool | int | float | str)):
            return {"value": None, "reason": "a non-scalar value was not stored"}
        return {"value": value, "reason": entry.get("reason") if isinstance(entry.get("reason"), str) else None}

    def _agent_record(self, record):
        """The allowlisted agent figures: scalars and their reasons only. Anything else in the evolution record (including its ``prs``,
        which repeat the PR-wide signals) is not stored."""
        sig = self._signal
        calls, out_h = record.get("tool_calls"), record.get("handoffs_out")
        calls = calls if isinstance(calls, dict) else {}
        out_h = out_h if isinstance(out_h, dict) else {}
        return {**{k: sig(record.get(k)) for k in _AGENT_SCALARS},
                "tool_calls": {k: sig(calls.get(k)) for k in ("total", "failed", "retried")},
                "handoffs_in": sig(record.get("handoffs_in")),
                "handoffs_out": {k: sig(out_h.get(k)) for k in ("total", "rejected", "failed")}}

    def _agents(self, trail, ci_na):
        sessions = [s for s in (trail.get("sessions") or []) if isinstance(s, dict) and s.get("session_id")]
        limit = int(self.config["max_agents"])
        missing = set()
        if self.evolution_for is None:
            return None, {"state": "unavailable", "reason": "agent evolution is not wired"}, len(sessions), {"agent_evolution"}
        agents = []
        for session in sessions[:limit]:
            try:
                body = self.evolution_for(session["session_id"])
            except Exception:  # noqa: BLE001 - one unreadable agent must not hide the rest
                body = None
            record = (body or {}).get("learning_record")
            if not isinstance(record, dict):
                missing.add("agent_evolution")
                agents.append({"session_id": session["session_id"], "rule": session.get("rule"), "learning_record": None,
                               "reason": "the agent's evolution could not be read"})
                continue
            states = (body or {}).get("sources") or {}
            for name in _CRITICAL_AGENT:
                if name == "ci" and ci_na:
                    continue
                if states.get(name) not in ("ok", None):
                    missing.add(f"agent_evolution.{name}")
            if states.get("tools") == "unavailable":
                missing.add("agent_evolution.tools")
            agents.append({"session_id": session["session_id"], "rule": session.get("rule"), "learning_record": self._agent_record(record)})
        omitted = max(0, len(sessions) - limit)
        state = "ok" if not omitted and not missing else "partial"
        reason = None if state == "ok" else (f"{omitted} agents were not assembled (control_plane.learning.max_agents)" if omitted
                                             else "some agents could not be fully read")
        return agents, {"state": state, "reason": reason}, omitted, missing

    @staticmethod
    def _clip(value, limit, field, trimmed):
        if not isinstance(value, str):
            return value
        text = scrub(value)
        if len(text) > limit:
            trimmed.add(f"text:{field}")
            return text[:limit - len(_CLIP)] + _CLIP
        return text

    def _project(self, trail, trimmed):
        """The allowlisted shape of the builders' lists: ids, states, counts, timestamps, reasons. No goals, names, review text, URLs."""
        pick = lambda row, keys: {k: row.get(k) for k in keys}  # noqa: E731
        sessions = [{**pick(s, ("session_id", "role", "runtime", "model", "host_id", "status", "commit_sha", "started_at", "last_seen_at", "rule", "inferred")),
                     "branch": self._clip(s.get("branch"), _BRANCH, "branch", trimmed)} for s in trail.get("sessions") or [] if isinstance(s, dict)]
        handoffs = [{**pick(h, ("handoff_id", "rule", "to_session_on_trail", "state", "created_at", "history", "time_to_accept_s", "problem")),
                     "from": {k: (h.get("from") or {}).get(k) for k in ("harness", "session_id")},
                     "to": {k: (h.get("to") or {}).get(k) for k in ("harness", "session_id")}}
                    for h in trail.get("handoffs") or [] if isinstance(h, dict)]
        reviews = [pick(r, ("review_id", "source", "kind", "code", "status", "verdict", "at", "completed_at", "changes_requested", "rule"))
                   for r in trail.get("reviews") or [] if isinstance(r, dict)]
        runs = [{**pick(r, ("provider_repo_id", "sha", "run_number", "state", "event", "at", "match")),
                 "branch": self._clip(r.get("branch"), _BRANCH, "branch", trimmed)} for r in trail.get("ci_runs") or [] if isinstance(r, dict)]
        ambiguous = [{"session_id": a.get("session_id"), "reason": self._clip(a.get("reason"), _TEXT, "reason", trimmed)}
                     for a in trail.get("ambiguous") or [] if isinstance(a, dict)]
        return sessions, handoffs, reviews, runs, ambiguous

    @staticmethod
    def _listing(items, omitted=0, reason=None):
        """Every list field has one shape, ``{"items": [...] | null, "omitted": n, "reason": str | null}``, whether or not it was cut."""
        return {"items": items, "omitted": int(omitted), "reason": reason}

    def _fit(self, out):
        """Hard cap: empty whole lists in a defined order (``items`` becomes ``[]`` and ``omitted`` counts what was dropped), then shorten
        signal reasons, and last resort keep only the core. Aggregates and ``trimmed`` always remain."""
        limit = MAX_BYTES - 512
        for field in _TRIM_ORDER:
            if _size(out) <= limit:
                return out
            listing = out.get(field)
            if isinstance(listing, dict) and listing.get("items"):
                listing["omitted"] += len(listing["items"])
                listing["items"] = []
                listing["reason"] = f"dropped to keep the record under {MAX_BYTES} bytes"
                out["trimmed"].append(field)
        if _size(out) > limit:
            for signal in (out.get("signals") or {}).values():
                if isinstance(signal, dict) and isinstance(signal.get("reason"), str):
                    signal["reason"] = signal["reason"][:160]
            out["trimmed"].append("signals.reason")
        if _size(out) > limit:
            core = {k: out.get(k) for k in ("schema_version", "kind", "assembled_at", "pr_id", "end_seq", "ended_at", "ended_source",
                                            "end_time_confidence", "pull_request", "signals", "incomplete", "missing_sources")}
            core.update(trimmed=[*out["trimmed"], "everything-but-core"], sources=out.get("sources"), omitted=out.get("omitted"))
            out = core
        return out

    def _ci_not_applicable(self):
        """True only when a CI provider is positively known not to be configured (then CI is null with a reason, not 'missing')."""
        try:
            return self.ci_configured is not None and self.ci_configured() is False
        except Exception:  # noqa: BLE001
            return False

    def _build(self, pr, now):
        """``(payload, missing_sources)`` for one ended pull request, or None when its trail cannot be built now."""
        parsed = parse_repository(pr.get("repository"))
        number = pr.get("number")
        if parsed is None or not isinstance(number, int) or isinstance(number, bool) or self.trail_for is None:
            return None
        host, owner, name = parsed
        try:
            trail = self.trail_for(host, owner, name, number)
        except Exception:  # noqa: BLE001
            trail = None
        if not isinstance(trail, dict):
            return None
        sources = dict(trail.get("sources") or {})
        ci_na = sources.get("ci") != "ok" and self._ci_not_applicable()
        if ci_na:
            sources["ci"] = "not_applicable"
        missing = {k for k in _CRITICAL_TRAIL if sources.get(k) != "ok" and not (k == "ci" and ci_na)}
        agents, agents_source, agents_omitted, agent_missing = self._agents(trail, ci_na)
        missing |= agent_missing
        trimmed: set = set()
        sessions, handoffs, reviews, runs, ambiguous = self._project(trail, trimmed)
        cut = trail.get("omitted") or {}
        view = trail.get("pull_request") or {}
        ended_at = pr.get("merged_at") or pr.get("closed_at")
        ended_ts = _parse(ended_at)
        lag = None if ended_ts is None else max(0.0, now - ended_ts)
        confidence = "high" if pr.get("ended_source") == "reported" or (
            pr.get("ended_source") == "observed" and lag is not None and lag <= self._min("low_confidence_after_minutes")) else "low"
        signals = {k: dict(v) if isinstance(v, dict) else v for k, v in (trail.get("signals") or {}).items()}
        withheld = []
        if confidence == "low":
            why = ("Withheld: the end time is " + (f"an observation made {int(lag // 60)} minutes after the pull request ended"
                                                  if lag is not None else "unknown") + ", so windows built on it are unreliable.")
            for key in _END_TIME_SIGNALS:
                if key in signals:
                    signals[key] = {"value": None, "reason": why}
                    withheld.append(key)
        listing = self._listing
        out = {
            "schema_version": SCHEMA, "kind": "pull_request_trail", "assembled_at": self.repo._stamp(),
            "pr_id": pr.get("pr_id"), "end_seq": pr.get("end_count") or 1, "ended_at": ended_at, "ended_source": pr.get("ended_source"),
            "end_time_confidence": confidence, "withheld_signals": withheld,
            "pull_request": {"host": host, "repository": f"{owner}/{name}", "number": number, "status": pr.get("status"),
                             "url": self._clip(view.get("url") or pr.get("url"), _TEXT, "url", trimmed),
                             "branch": self._clip(view.get("branch") or pr.get("branch"), _BRANCH, "branch", trimmed),
                             "base": self._clip(pr.get("base"), _BRANCH, "base", trimmed), "opened_at": view.get("opened_at"),
                             "merged_at": pr.get("merged_at"), "closed_at": pr.get("closed_at")},
            "state": trail.get("state"), "reason": self._clip(trail.get("reason"), _TEXT, "reason", trimmed),
            "sources": {**sources, "agent_evolution": agents_source},
            "omitted": {**cut, "agents": agents_omitted},
            "signals": signals,
            "sessions": listing(sessions, cut.get("sessions") or 0), "ambiguous": listing(ambiguous),
            "inferred_sessions_count": len(trail.get("inferred_sessions") or []),
            "handoffs": listing(handoffs, cut.get("handoffs") or 0), "reviews": listing(reviews, cut.get("reviews") or 0),
            "ci_runs": listing(runs, cut.get("ci_runs") or 0),
            "agents": listing(agents, agents_omitted, agents_source.get("reason") if agents is None else None),
            "incomplete": False, "missing_sources": [], "trimmed": [],
        }
        out = _clean(out, trimmed)
        out["trimmed"] = sorted(trimmed)
        return self._fit(out), missing

    # ------------------------------------------------------------------ sweep
    def sweep(self, *, force=False):
        """Assemble the records that are due (at most ``sweep_batch`` builds), re-check recent records for late evidence, prune. ``force``
        skips the five-minute throttle (tests, maintenance); the warm-up still applies. A second concurrent sweep returns at once."""
        if not self.enabled:
            return 0
        now = self._now()
        if now - self._started < self._min("warmup_minutes"):
            return 0
        if not force and now - self._swept < SWEEP_EVERY:
            return 0
        if not self._sweep_guard.acquire(blocking=False):
            return 0
        try:
            self._swept = now
            written = self._assemble_due(now)
            self._late_checks(now)
            self._prune()
            return written
        finally:
            self._sweep_guard.release()

    def trigger_sweep(self):
        """A bounded, non-forced sweep in a background thread, started only when a record is actually due; never blocks the caller on a
        trail build. Returns whether one was started."""
        if not self.enabled or (self._worker is not None and self._worker.is_alive()):
            return False
        now = self._now()
        if now - self._swept < SWEEP_EVERY or now - self._started < self._min("warmup_minutes"):
            return False
        version = getattr(self.repo, "pull_request_version", None)
        if version is not None and version == self._idle_version and now < self._idle_until:
            return False                                          # nothing moved since a scan found nothing due: no scan, no copy
        if not self._candidates(now)[0]:
            self._idle_version, self._idle_until = version, self._next_due
            return False

        def run():
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001
                log.warning("learning trail sweep failed: %s", type(exc).__name__)
        self._worker = threading.Thread(target=run, daemon=True, name="learning-trails-sweep")
        self._worker.start()
        return True

    @staticmethod
    def _same_end(a, b):
        """Whether two reads of a PR record describe the same end (status, times and end count unchanged)."""
        a, b = a or {}, b or {}
        return (a.get("status") == b.get("status") and a.get("merged_at") == b.get("merged_at")
                and a.get("closed_at") == b.get("closed_at") and (a.get("end_count") or 1) == (b.get("end_count") or 1))

    def _candidates(self, now):
        """``(due, seen, recorded)``: the PRs whose end has no record yet and is not postponed, fairest first (never tried, then least
        recently tried, then oldest end); every ended key seen; the PRs that already have a record. No trail is built here."""
        self._ensure_index()
        due, seen, recorded = [], set(), []
        skipped, next_due = 0, float("inf")
        with self._lock:
            horizon = self._horizon
            ever = self._ever
        for pr in self.repo.list_pull_requests():
            if str(pr.get("status") or "").lower() not in _ENDED or not isinstance(pr.get("pr_id"), str):
                continue
            if not (pr.get("merged_at") or pr.get("closed_at")):
                continue
            key = (pr["pr_id"], pr.get("end_count") or 1)
            seen.add(key)
            if key in ever:
                recorded.append(pr)
                continue
            ended = _parse(pr.get("merged_at") or pr.get("closed_at"))
            if horizon is not None and ended is not None and ended < horizon:
                skipped += 1                                    # older than what retention already dropped: it would be pruned at once
                continue
            gave_up = self._unbuildable.get(key)
            if gave_up and gave_up["retry_at"] > now:
                next_due = min(next_due, gave_up["retry_at"])
                continue
            track = self._track.get(key)
            if track and track["retry_at"] > now:
                next_due = min(next_due, track["retry_at"])
                continue
            due.append(((track["last"] if track else -1.0), ended or 0.0, pr, key))
        due.sort(key=lambda d: (d[0], d[1]))
        self._skipped_horizon, self._next_due = skipped, next_due
        return due, seen, recorded

    def _assemble_due(self, now):
        due, seen, recorded = self._candidates(now)
        for pr in recorded:
            self._late_from_record(pr)
        self._track = {k: v for k, v in self._track.items() if k in seen}          # the maps never outgrow the PRs that still wait
        self._unbuildable = {k: v for k, v in self._unbuildable.items() if k in seen}
        written = 0
        attempts = int(self.config["max_build_attempts"])
        for _, _, pr, key in due[:int(self.config["sweep_batch"])]:
            if self._closed:
                break                                           # a closed instance stops writing, even mid-pass
            retry = key in self._unbuildable                    # a long-retry attempt: one failure puts it back to waiting
            track = self._track.setdefault(key, {"first": now, "retry_at": 0.0, "last": -1.0, "none": attempts - 1 if retry else 0})
            track["last"] = now
            built = self._build(pr, now)                      # outside any lock: it walks every session and handoff
            if built is None:
                track["none"] += 1
                if track["none"] >= attempts:
                    since = (self._unbuildable.get(key) or {}).get("since", now)
                    self._unbuildable[key] = {"reason": f"the trail could not be built in {attempts} attempts", "since": since,
                                              "retry_at": now + float(self.config["unbuildable_retry_hours"]) * 3600}
                    self._track.pop(key, None)
                    log.warning("learning trail for %s is unbuildable; retrying in %s h", key[0], self.config["unbuildable_retry_hours"])
                    while len(self._unbuildable) > _UNBUILDABLE_CAP:
                        self._unbuildable.pop(next(iter(self._unbuildable)))
                else:
                    track["retry_at"] = now + self._min("retry_interval_minutes")
                continue
            payload, missing = built
            if missing and now - track["first"] < self._min("ready_retry_minutes"):
                track["retry_at"] = now + self._min("retry_interval_minutes")
                continue
            payload["incomplete"], payload["missing_sources"] = bool(missing), sorted(missing)
            with self._write_lock:
                if self._has_record(*key) or not self._same_end(self.repo.get_pull_request(pr["pr_id"]), pr):
                    continue                                    # it moved while the trail was built: decide again next pass
                if self._write(pr["pr_id"], payload, EVENT) is not None:
                    written += 1
                    self._track.pop(key, None)
                    self._unbuildable.pop(key, None)            # a build that works clears the attempt count and the give-up state
        return written

    def unbuildable(self):
        """``{(pr_id, end_seq): reason}`` of the ends given up on (the trail was never readable)."""
        return {k: v["reason"] for k, v in self._unbuildable.items()}

    # ------------------------------------------------------------------ late evidence
    def _late_from_record(self, pr):
        """A provider-reported end time that differs from the one the record was assembled with becomes one late-evidence event."""
        if pr.get("ended_source") != "reported":
            return
        record = self._record_for(pr["pr_id"], pr.get("end_count") or 1)
        if record is None or record.get("ended_source") != "observed":
            return
        reported = pr.get("merged_at") or pr.get("closed_at")
        with self._lock:
            known = (pr["pr_id"], f"reported:{reported}") in self._late_keys
        if reported == record.get("ended_at") or known:
            return
        self.late_evidence(pr["pr_id"], record["end_seq"], {"merged_at": pr.get("merged_at"), "closed_at": pr.get("closed_at"),
                                                           "ended_source": "reported"}, _reported=reported)

    def late_evidence(self, pr_id, end_seq, evidence, *, _reported=None):
        """Append evidence that arrived after a record was written. Only the whitelisted keys are kept; the record itself is untouched.
        Evidence for a record that retention has pruned is refused (KeyError)."""
        with self._write_lock:
            if self._record_for(pr_id, end_seq) is None:
                raise KeyError("no trail record for that end")
            kept = _clean({k: v for k, v in (evidence or {}).items() if k in _LATE_KEYS}, set())
            if not kept:
                raise ValueError("no recognised evidence")
            if _reported:
                kept["ended_at_reported"] = scrub(str(_reported))[:64]
            payload = {"schema_version": SCHEMA, "kind": "pull_request_trail_late_evidence", "pr_id": pr_id, "end_seq": end_seq,
                       "assembled_at": self.repo._stamp(), "evidence": kept}
            return self._write(pr_id, payload, LATE)

    def _late_checks(self, now):
        """Rebuild the trail of a few recent records and note signals that were null at assembly and are known now (bounded: at most
        ``late_checks`` times per record, ``late_check_interval_minutes`` apart, inside ``late_window_hours``)."""
        if int(self.config["late_checks"]) <= 0:
            return
        self._ensure_index()
        with self._lock:
            candidates = [(key, rec) for key, rec in self._records.items()
                          if now - (_parse(rec.get("assembled_at")) or 0) <= float(self.config["late_window_hours"]) * 3600]
        done = 0
        for key, record in candidates:
            count, last = self._late_state.get(key, (0, _parse(record.get("assembled_at")) or 0.0))
            if count >= int(self.config["late_checks"]) or now - last < self._min("late_check_interval_minutes"):
                continue
            if done >= max(1, int(self.config["sweep_batch"]) // 2):
                break
            done += 1
            self._late_state[key] = (count + 1, now)
            pr = self.repo.get_pull_request(key[0])
            if pr is None or (pr.get("end_count") or 1) != record.get("end_seq") or str(pr.get("status") or "").lower() not in _ENDED:
                continue                                         # the PR is not on the end this record describes any more
            built = self._build(pr, _parse(record.get("assembled_at")) or now)
            if built is None or built[1]:
                continue                                         # not fully readable now: a later check will decide
            fresh = built[0]["signals"]
            changed = {name: sig for name, sig in fresh.items()
                       if name not in (record.get("withheld_signals") or []) and isinstance(sig, dict) and sig.get("value") is not None
                       and (record.get("signals") or {}).get(name, {}).get("value") is None}
            if not changed:
                continue
            digest = hashlib.sha256(json.dumps(changed, sort_keys=True, default=str).encode()).hexdigest()[:32]
            with self._lock:
                if (key[0], f"digest:{digest}") in self._late_keys:
                    continue
            try:
                self.late_evidence(key[0], key[1], {"signals": changed, "digest": digest})
            except (KeyError, ValueError):
                continue                                         # pruned meanwhile

    # ------------------------------------------------------------------ retention
    def _prune(self):
        self._ensure_index()
        with self._lock:
            return self._prune_locked()

    def _prune_locked(self):
        """Keep at most ``max_records`` trail records in memory and in the export, dropping the oldest PRs whole. The durable log keeps the
        events and a restart reloads and prunes them again, to the same point. The set of ends ever assembled is not touched, so a pruned
        PR is never assembled again; ``_horizon`` stops an end older than what was dropped from being assembled at all."""
        limit = int(self.config["max_records"])
        if len(self._records) <= limit:
            return 0
        per_pr: dict = {}
        for pr_id, _ in self._records:
            per_pr[pr_id] = per_pr.get(pr_id, 0) + 1
        order, seen = [], set()
        for row in self._rows:
            if row[1] not in seen:
                seen.add(row[1])
                order.append(row[1])
        drop, count = set(), len(self._records)
        for pr_id in order:
            if count <= limit:
                break
            drop.add(pr_id)
            count -= per_pr.get(pr_id, 0)
        if not drop:
            return 0
        gone = [r for r in self._rows if r[1] in drop]
        self._pruned_through = max(self._pruned_through, max(r[0] for r in gone))
        ends = [r[5] for r in gone if r[2] == EVENT and r[5] is not None]
        if ends:
            self._horizon = max([self._horizon or 0.0, *ends])
        self._rows = [r for r in self._rows if r[1] not in drop]
        self._seqs = [r[0] for r in self._rows]
        self._records = {k: v for k, v in self._records.items() if k[0] not in drop}
        self._late_keys = {k for k in self._late_keys if k[0] not in drop}
        return len(drop)

    # ------------------------------------------------------------------ export
    def export(self, *, since=None, cursor=None, limit=None, ended_after=None):
        """One page, oldest first, from the index (only the page is copied). ``since`` filters on ``assembled_at`` (when the line was
        written), ``ended_after`` on the pull request's ``ended_at``. Raises ValueError for bad parameters."""
        page_max = int(self.config["page_max"])
        if limit is None:
            limit = int(self.config["page_default"])
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= page_max:
            raise ValueError(f"limit must be a whole number from 1 to {page_max}")
        since_ts = ended_ts = None
        if since is not None:
            since_ts = _parse(since)
            if since_ts is None:
                raise ValueError("since must be an ISO 8601 time")
        if ended_after is not None:
            ended_ts = _parse(ended_after)
            if ended_ts is None:
                raise ValueError("ended_after must be an ISO 8601 time")
        after = -1
        if cursor is not None:
            if not isinstance(cursor, str) or not _CURSOR.fullmatch(cursor):
                raise ValueError("cursor is not valid")
            after = int(cursor[1:])
        self._ensure_index()
        with self._lock:
            start = bisect.bisect_right(self._seqs, after)
            picked, has_more = [], False
            for index in range(start, len(self._rows)):
                row = self._rows[index]
                if since_ts is not None and row[4] < since_ts:
                    continue
                if ended_ts is not None and (row[5] is None or row[5] < ended_ts):
                    continue
                if len(picked) == limit:
                    has_more = True
                    break
                picked.append(row)
            page = [{**copy.deepcopy(row[3]), "cursor": f"s{row[0]}"} for row in picked]
            pruned = self._pruned_through
        waiting = list(self._unbuildable.items())
        stamp = lambda ts: dt.datetime.fromtimestamp(ts, dt.UTC).isoformat()  # noqa: E731
        return {"schema_version": SCHEMA, "records": page, "count": len(page), "has_more": has_more,
                "unbuildable": {"count": len(waiting), "items": [
                    {"pr_id": k[0], "end_seq": k[1], "reason": v["reason"], "retry_at": stamp(v["retry_at"])}
                    for k, v in sorted(waiting, key=lambda kv: kv[1]["since"])[:20]]},
                "skipped_by_horizon": self._skipped_horizon,
                "next_cursor": page[-1]["cursor"] if page else (cursor if cursor else None),
                "truncated_before": f"s{pruned}" if pruned >= 0 else None,
                "cursor_truncated": bool(pruned >= 0 and cursor is not None and after < pruned)}


def encode_cursor(seq):
    """A cursor for ``learning_seq`` ``seq`` (exposed for tests and clients that want to resume from a known point)."""
    return f"s{int(seq)}"


__all__ = ["EVENT", "LATE", "ENTITY", "TrailRecords", "learning_config", "parse_repository", "encode_cursor", "scrub"]
