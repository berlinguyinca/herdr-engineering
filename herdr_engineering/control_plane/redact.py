"""Secret redaction (Spec §97, §101). Never log or expose credentials.

Applies to event payloads, session messages, and any text surfaced in the UI
or logs. Redaction happens at the boundary (before persistence/display) so raw
secrets are not stored or rendered.
"""
from __future__ import annotations

import re
from typing import Any

# Ordered so longer/more specific patterns win over generic ones.
_PATTERNS = [
    # OpenAI-style keys: sk-... (not inside a longer word: "task-s6lease", "disk-image-1" are ids, not keys; found
    # by the T16 acceptance run, which saw a task id stored as "ta[REDACTED]")
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9\-_]{3,}"),
    # ... but a key-length run is a key wherever it sits: right after an ANSI colour code ("\x1b[32msk-...", the "m" is a word
    # character) or a URL-encoded "=" ("api_key%3Dsk-..."), which CI logs routinely contain. Ids such as "task-sk-ab" are
    # far shorter than 20 characters after "sk-" and stay untouched.
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    # AWS access key id (and the temporary ASIA form), wherever it sits
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    # AWS secret access key: 40 base64 characters after an aws secret label
    re.compile(r"(?i)aws[_\- ]?secret[_\- ]?(?:access[_\- ]?)?key[\"']?\s*[:=]?\s*[\"']?[A-Za-z0-9/+=]{30,}"),
    # GitHub tokens (classic, fine-grained, OAuth, app, refresh) and Slack tokens
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])xox[abprse]-[A-Za-z0-9-]{10,}"),
    # generic bearer tokens / JWTs (three base64url dot-separated parts)
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"),
    # password / secret / token / apikey / api_key / client_secret / auth = <value>
    # (value may be quoted or unquoted)
    re.compile(
        r"(?i)(password|passwd|secret|token|apikey|api_key|access_token|"
        r"refresh_token|client_secret|client_secret|authorization|x-api-key|"
        r"auth_token)\s*[:=]\s*[\"']?[^\s,;\"']+"),
    # key=value / "key": "value" with an obviously-secret key name
    re.compile(r"(?i)\b(secret|password|passwd|apikey|api[_-]?key|private[_-]?key|\
        pwd|pass)\b[\"']?\s*[:=]\s*[\"']?[^\s,;\"']{8,}"),
    # space-separated secrets: "token abc.def.ghi", "secret abc123"
    re.compile(r"(?i)\b(token|secret|api[_-]?key|access[_-]?token)")
    if False else re.compile(
        r"(?i)\b(token|secret|api[_-]?key|access[_-]?token)\s+[A-Za-z0-9._\-]{8,}"),
    # private keys: handled by the linear scan in _redact_private_keys (a lazy
    # ".*?" over many unterminated BEGIN markers is quadratic).
    # generic connection strings with embedded credentials
    re.compile(r"(?i)(postgres|postgresql|mysql|redis|rediss|mongodb|mongodb\+srv|"
               r"amqp|amqps|s3|minio)://[^:\s]+:[^@\s]+@"),
]

_REDACTED = "[REDACTED]"

#: Keys whose values are redacted wholesale (lowercased substring match).
_SECRET_KEY_HINTS = (
    "password", "passwd", "pass", "pwd", "secret", "token", "apikey",
    "api_key", "client_secret", "authorization", "x-api-key", "auth",
    "bearer", "private_key", "private-key", "credential", "dsn",
    "connection_string",
)


#: Keys that merely CONTAIN a hint ("author" contains "auth") but carry no secret. The head-bound review quorum
#: excludes the PR author's own approval by comparing this login (T06), so redacting it to "[REDACTED]" silently let
#: an author approve their own PR (found by the T16 acceptance run).
_PLAIN_KEYS = frozenset({"author", "authors", "author_login", "author_name"})

_BEGIN = re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")
_END = re.compile(r"-----END[ A-Z]*PRIVATE KEY-----")


def _redact_private_keys(text: str) -> str:
    """Linear-time PEM block redaction. An unterminated block is redacted to the end."""
    if "PRIVATE KEY-----" not in text:
        return text
    out, pos = [], 0
    while True:
        b = _BEGIN.search(text, pos)
        if not b:
            out.append(text[pos:])
            break
        out.append(text[pos:b.start()])
        e = _END.search(text, b.end())
        out.append(_REDACTED)
        if not e:
            break
        pos = e.end()
    return "".join(out)


def redact_text(text: str) -> str:
    """Replace secret-looking substrings in a string with [REDACTED]."""
    if not text:
        return text
    out = _redact_private_keys(text)
    for pattern in _PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def redact_payload(payload: Any, *, _seen=None) -> Any:
    """Recursively redact secret values/keys inside an event payload / JSON."""
    _seen = _seen if _seen is not None else set()
    if id(payload) in _seen:
        return payload
    _seen.add(id(payload))
    if isinstance(payload, str):
        return redact_text(payload)
    if isinstance(payload, dict):
        result = {}
        for key, value in payload.items():
            lk = str(key).lower()
            if lk not in _PLAIN_KEYS and any(s in lk for s in _SECRET_KEY_HINTS):
                result[key] = _REDACTED
            else:
                result[key] = redact_payload(value, _seen=_seen)
        return result
    if isinstance(payload, list):
        return [redact_payload(item, _seen=_seen) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_payload(item, _seen=_seen) for item in payload)
    return payload


def redact_values(payload: Any) -> Any:
    """Like ``redact_payload`` but never by key name: only string values are scanned."""
    if isinstance(payload, str):
        return redact_text(payload)
    if isinstance(payload, dict):
        return {key: redact_values(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return type(payload)(redact_values(item) for item in payload)
    return payload


#: Delivery-record keys whose VALUES are scanned for secrets but that never
#: themselves carry a secret, so key-name redaction must not blank them. This
#: is delivery-specific (unlike the global ``_PLAIN_KEYS``): the count fields
#: ``passed``/``failed``/``skipped``/``total`` and ``url`` stay intact, while
#: any string value (including a ``passed`` value that happens to be secret
#: text) is still scanned by ``redact_text``.
_DELIVERY_VALUE_KEYS = frozenset({
    "passed", "failed", "skipped", "total", "url", "status", "verdict",
    "reviewer", "repository", "branch", "base", "number", "severity",
    "summary", "detail", "label", "kind", "test_run_id", "review_id",
    "pr_id", "finding_id", "mission_id", "session_id", "started_at",
    "ended_at", "created_at", "updated_at", "completed_at",
})


def redact_delivery(payload: Any, *, _memo=None, _active=None) -> Any:
    """Redact a delivery record (tests/builds/reviews/pulls) for display.

    Numeric counts and structural fields survive; string values are scanned for
    leaked secrets via ``redact_text``, so a secret in a ``passed`` value or a
    finding ``summary`` is redacted while the count/url/status text is kept.
    Keys whose names hint at a secret (e.g. a ``token`` or ``api_key`` field)
    are redacted wholesale. Unlike ``redact_payload`` this never blanks the
    ``passed`` count key merely because it contains the substring ``pass``.

    A memoized result is returned for a repeated container object (cycle
    safety), so a shared dict/list never leaks a secret on a later occurrence:
    the sanitized copy is reused, never the original.
    """
    _memo = _memo if _memo is not None else {}
    _active = _active if _active is not None else set()
    if isinstance(payload, str):
        # Redact every occurrence: the same string object may appear more than
        # once, and a shared string must never bypass scanning because a sibling
        # occurrence was already visited. Cycle safety is handled per-container.
        return redact_text(payload)
    if isinstance(payload, dict):
        ident = id(payload)
        if ident in _memo:
            return _memo[ident]
        if ident in _active:
            return payload  # direct cycle; nothing to redact that isn't already
        _active.add(ident)
        result = {}
        for key, value in payload.items():
            lk = str(key).lower()
            if any(s in lk for s in _SECRET_KEY_HINTS) and lk not in _DELIVERY_VALUE_KEYS:
                result[key] = _REDACTED
            else:
                result[key] = redact_delivery(value, _memo=_memo, _active=_active)
        _active.discard(ident)
        _memo[ident] = result
        return result
    if isinstance(payload, (list, tuple)):
        ident = id(payload)
        if ident in _memo:
            return _memo[ident]
        if ident in _active:
            return payload  # direct cycle
        _active.add(ident)
        result = type(payload)(redact_delivery(item, _memo=_memo, _active=_active) for item in payload)
        _active.discard(ident)
        _memo[ident] = result
        return result
    return payload


#: Host state keys whose dict KEYS are data (package / extension / unit names),
#: so key-name redaction would turn e.g. a pi extension ``auth-helper`` into
#: "[REDACTED]" and report permanent version drift. Their values are still scanned.
HOST_STATE_VALUE_ONLY = ("versions", "pins", "updates", "fleet_update")


def redact_ingest_body(body: Any) -> Any:
    """Redaction for a generic ``/api/v1/ingest`` envelope.

    Host-state envelopes keep their package/unit/extension dict KEYS intact
    (their values are still scanned). Delivery envelopes (tests/builds/reviews/
    pulls) go through the delivery-aware redactor so numeric count fields like
    ``passed`` survive instead of being blanked because their name contains the
    substring ``pass``; their string values are still scanned for secrets.
    Everything else uses the global ``redact_payload``.
    """
    if not (isinstance(body, dict) and isinstance(body.get("payload"), dict)):
        return redact_payload(body)
    entity_type = body.get("entity_type")
    if entity_type in ("test_run", "build", "review", "review_finding", "pull_request"):
        return redact_delivery(body)
    if entity_type != "host":
        return redact_payload(body)
    payload = body["payload"]
    rest = {k: v for k, v in body.items() if k != "payload"}
    out = redact_payload(rest)
    out["payload"] = redact_payload(
        {k: v for k, v in payload.items() if k not in HOST_STATE_VALUE_ONLY})
    for key in HOST_STATE_VALUE_ONLY:
        if key in payload:
            out["payload"][key] = redact_values(payload[key])
    return out
