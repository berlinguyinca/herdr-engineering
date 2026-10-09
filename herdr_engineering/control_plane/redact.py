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
    # private keys
    re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
               re.DOTALL),
    # generic connection strings with embedded credentials
    re.compile(r"(?i)(postgres|postgresql|mysql|redis|rediss|mongodb|mongodb\+srv|"
               r"amqp|amqps|s3|minio)://[^:\s]+:[^@\s]+@"),
    # OpenSSH private keys (BEGIN OPENSSH PRIVATE KEY block)
    re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----.*?"
               r"-----END OPENSSH PRIVATE KEY-----", re.DOTALL),
]

_REDACTED = "[REDACTED]"

#: Keys whose values are redacted wholesale (lowercased substring match).
_SECRET_KEY_HINTS = (
    "password", "passwd", "pass", "pwd", "secret", "token", "apikey",
    "api_key", "client_secret", "authorization", "x-api-key", "auth",
    "bearer", "private_key", "private-key", "credential", "dsn",
    "connection_string",
)


def redact_text(text: str) -> str:
    """Replace secret-looking substrings in a string with [REDACTED]."""
    if not text:
        return text
    out = text
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
            if any(s in lk for s in _SECRET_KEY_HINTS):
                result[key] = _REDACTED
            else:
                result[key] = redact_payload(value, _seen=_seen)
        return result
    if isinstance(payload, list):
        return [redact_payload(item, _seen=_seen) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_payload(item, _seen=_seen) for item in payload)
    return payload
