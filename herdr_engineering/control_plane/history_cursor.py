"""Process-private history fingerprints; raw-secret hashes never enter cursors."""
import hashlib
import hmac
import json
import secrets

_KEY = secrets.token_bytes(32)


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()
    return hmac.new(_KEY, encoded, hashlib.sha256).hexdigest()
