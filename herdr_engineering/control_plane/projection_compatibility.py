"""Build-bound compatibility for explicitly reviewed operational-only changes.

The bundled policy is release code, never remote configuration. Its contents
are fingerprinted except for the self-reference to that fingerprint. Any code
or policy edit disables aliases until the new build is explicitly reviewed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .projection_store import FORMAT_VERSION

MANIFEST_PATH = Path(__file__).with_name('projection_compatibility.json')
_HASH = re.compile(r'[0-9a-f]{64}')


def _invalid_number(_value):
    raise ValueError('non-finite compatibility policy value')


def _policy():
    try:
        if MANIFEST_PATH.stat().st_size > 64 * 1024:
            return None
        value = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'), parse_constant=_invalid_number)
        return value if type(value) is dict else None
    except (OSError, ValueError, UnicodeError):
        return None


def fingerprint_policy():
    value = _policy()
    if value is None:
        return b'no-compatible-checkpoint-policy'
    value = dict(value)
    value.pop('current_code_fingerprint', None)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def compatible_fingerprints(current):
    if type(current) is not str or not _HASH.fullmatch(current):
        return ()
    value = _policy()
    if (value is None or type(value.get('format_version')) is not int
            or value['format_version'] != 1
            or type(value.get('checkpoint_schema')) is not int
            or value['checkpoint_schema'] != FORMAT_VERSION
            or value.get('current_code_fingerprint') != current):
        return ()
    producers = value.get('compatible_code_fingerprints')
    if (type(producers) is not list or len(producers) > 8
            or any(type(code) is not str or not _HASH.fullmatch(code) for code in producers)):
        return ()
    return tuple(dict.fromkeys(code for code in producers if code != current))
