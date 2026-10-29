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
    # OpenAI-style keys: sk-...
    re.compile(r"sk-[A-Za-z0-9\-_]{3,}"),
    # AWS access key id
    re.compile(r"AKIA[0-9A-Z]{16}"),
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


def redact_payload(payload: Any, *, _seen=None, _memo=None) -> Any:
    """Redact every occurrence, memoizing sanitized containers and masking cycles."""
    if isinstance(payload, str):
        return redact_text(payload)
    if not isinstance(payload, (dict, list, tuple)):
        return payload
    active = _seen if _seen is not None else set()
    memo = _memo if _memo is not None else {}
    identity = id(payload)
    if identity in active:
        return _REDACTED
    if identity in memo:
        return memo[identity]
    active.add(identity)
    try:
        if isinstance(payload, dict):
            result = {}
            for key, value in payload.items():
                lk = str(key).lower()
                if any(s in lk for s in _SECRET_KEY_HINTS):
                    result[key] = _REDACTED
                else:
                    result[key] = redact_payload(value, _seen=active, _memo=memo)
        elif isinstance(payload, list):
            result = [redact_payload(item, _seen=active, _memo=memo) for item in payload]
        else:
            result = tuple(redact_payload(item, _seen=active, _memo=memo) for item in payload)
        memo[identity] = result
        return result
    finally:
        active.remove(identity)
