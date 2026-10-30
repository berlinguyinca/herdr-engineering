"""The Rakazo vault: credentials the person keeps for their agents, encrypted at rest.

Values are encrypted with Fernet under a key derived from an operator-supplied key file (``vault_key_file`` under
``control_plane.rakazo_bridge``; at least 32 bytes, e.g. ``head -c 48 /dev/urandom > key; chmod 600 key``). Without that file the
vault is off and says so. Only ciphertext reaches the prefs table; list calls return metadata, never the value; the value
leaves only through ``vault/reveal`` (the app asks the person to confirm their identity first). Neither a value nor a ciphertext
is ever logged. The vault is namespaced by account (``scope: account``) or by space (``scope: space``), bot secrets by bot, and
agent environment secrets by account. "Remove" tombstones the entry after wiping its ciphertext; nothing else is deleted.

This stores and reveals credentials. Injecting them into an agent's outbound requests is not part of the bridge.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import UTC, datetime
from typing import Any

VAULT_KIND, BOT_KIND, ENV_KIND = "vault", "bot_secret", "agent_secret"
MAX_ENTRIES = 200
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ENV = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,120}\Z")
_OFF = "The vault is off: the operator has not set vault_key_file for the bridge."


def load_cipher(path: str | None):
    """A Fernet for the key file, or None when no file is configured. A bad file stops startup."""
    if not path:
        return None
    from cryptography.fernet import Fernet  # noqa: PLC0415 - optional, only with a vault key
    try:
        raw = open(path, "rb").read()                      # noqa: SIM115
    except OSError as exc:
        raise ValueError(f"control_plane.rakazo_bridge.vault_key_file cannot be read: {exc.strerror}") from None
    if len(raw.strip()) < 32:
        raise ValueError("control_plane.rakazo_bridge.vault_key_file must hold at least 32 bytes")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"rakazo-vault:" + raw.strip()).digest()))


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Vault:
    def __init__(self, bridge, error_type, cipher):
        self.b = bridge
        self.Error = error_type
        self.prefs = bridge.prefs
        self.cipher = cipher

    def procs(self) -> dict[str, Any]:
        return {"vault/list": self.list, "vault/put": self.put, "vault/reveal": self.reveal, "vault/remove": self.remove,
                "botSecrets/list": self.bot_list, "botSecrets/put": self.bot_put, "botSecrets/remove": self.bot_remove,
                "agentSecrets/list": self.env_list, "agentSecrets/put": self.env_put, "agentSecrets/remove": self.env_remove}

    # plumbing -----------------------------------------------------------------------------------------------
    def _need(self):
        if self.cipher is None:
            raise self.Error(400, "BAD_REQUEST", _OFF)

    def _space(self, ctx, scope) -> str:
        if scope not in (None, "account", "space"):
            raise self.Error(400, "BAD_REQUEST", "scope must be account or space.")
        return "-" if scope == "account" else ctx.space

    def _rows(self, ctx, space, kind):
        return {k: r for k, r in self.prefs.items(ctx.account.account_id, space, kind).items() if not r.get("removed")}

    def _destination(self, inp) -> dict[str, Any]:
        d = inp.get("destination")
        if not isinstance(d, dict) or set(d) - {"name", "origin", "auth"}:
            raise self.Error(400, "BAD_REQUEST", "destination must be {name, origin, auth}.")
        name, origin, auth = d.get("name"), d.get("origin", ""), d.get("auth")
        if not isinstance(name, str) or not _NAME.match(name):
            raise self.Error(400, "BAD_REQUEST", "The name must be lowercase letters, digits, - or _.")
        if not isinstance(origin, str) or len(origin) > 2048:
            raise self.Error(400, "BAD_REQUEST", "origin is too long.")
        kind = auth.get("type") if isinstance(auth, dict) else None
        ok = ((kind in ("bearer", "login", "command") and set(auth) == {"type"})
              or (kind == "header" and set(auth) == {"type", "name"} and isinstance(auth["name"], str)
                  and _HEADER.match(auth["name"]))
              or (kind == "basic" and set(auth) == {"type", "username"} and isinstance(auth["username"], str)
                  and 0 < len(auth["username"]) <= 200 and not re.search(r"[:\r\n]", auth["username"])))
        if not ok:
            raise self.Error(400, "BAD_REQUEST", "auth is not a valid credential type.")
        return {"name": name, "origin": origin, "auth": auth}

    def _value(self, inp) -> str:
        v = inp.get("value")
        if not isinstance(v, str) or not v or len(v) > 16384:
            raise self.Error(400, "BAD_REQUEST", "A value of 1 to 16384 characters is required.")
        return v

    def _seal(self, value: str) -> str:
        return self.cipher.encrypt(value.encode()).decode()

    def _meta(self, row) -> dict[str, Any]:
        return {"name": row["name"], "origin": row["origin"], "auth": row["auth"], "createdAt": row["createdAt"],
                "updatedAt": row["updatedAt"]}

    def _store(self, ctx, space, kind, key, dest, value):
        self._need()
        rows = self._rows(ctx, space, kind)
        if key not in rows and len(rows) >= MAX_ENTRIES:
            raise self.Error(400, "BAD_REQUEST", "Too many saved credentials.")
        stamp = rows.get(key, {}).get("createdAt") or _now()
        row = {**dest, "ct": self._seal(value), "createdAt": stamp, "updatedAt": _now()}
        self.b.prefs_put(ctx.account.account_id, space, kind, key, row)
        return self._meta(row)

    def _wipe(self, ctx, space, kind, key) -> None:
        rows = self._rows(ctx, space, kind)
        if key not in rows:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        self.b.prefs_put(ctx.account.account_id, space, kind, key, {"removed": _now()})     # the ciphertext is overwritten

    # vault --------------------------------------------------------------------------------------------------
    def list(self, ctx, inp):
        rows = self._rows(ctx, self._space(ctx, inp.get("scope")), VAULT_KIND)
        return [self._meta(r) for _, r in sorted(rows.items())]

    def put(self, ctx, inp):
        dest, value = self._destination(inp), self._value(inp)
        return self._store(ctx, self._space(ctx, inp.get("scope")), VAULT_KIND, dest["name"], dest, value)

    def reveal(self, ctx, inp):
        self._need()
        row = self._rows(ctx, self._space(ctx, inp.get("scope")), VAULT_KIND).get(inp.get("name"))
        if row is None:
            raise self.Error(404, "NOT_FOUND", "Not found.")
        self.b._audit(ctx.account, "rakazo.bridge.vault_reveal")          # who looked, never what
        return {"value": self.cipher.decrypt(row["ct"].encode()).decode()}

    def remove(self, ctx, inp):
        self._wipe(ctx, self._space(ctx, inp.get("scope")), VAULT_KIND, inp.get("name"))
        return {"ok": True}

    # bot secrets --------------------------------------------------------------------------------------------
    def bot_list(self, ctx, inp):
        bot = self.b._assistant(ctx, inp.get("botId")).assistant_id
        prefix = bot + ":"
        return [self._meta(r) for k, r in sorted(self._rows(ctx, ctx.space, BOT_KIND).items()) if k.startswith(prefix)]

    def bot_put(self, ctx, inp):
        bot = self.b._assistant(ctx, inp.get("botId")).assistant_id
        dest, value = self._destination(inp), self._value(inp)
        return self._store(ctx, ctx.space, BOT_KIND, f"{bot}:{dest['name']}", dest, value)

    def bot_remove(self, ctx, inp):
        bot = self.b._assistant(ctx, inp.get("botId")).assistant_id
        self._wipe(ctx, ctx.space, BOT_KIND, f"{bot}:{inp.get('name')}")
        return {"ok": True}

    # agent environment secrets ------------------------------------------------------------------------------
    def env_list(self, ctx, inp):
        return [{"id": k, "name": r["name"], "createdAt": r["createdAt"], "updatedAt": r["updatedAt"]}
                for k, r in sorted(self._rows(ctx, "-", ENV_KIND).items(), key=lambda kv: kv[1]["name"])]

    def env_put(self, ctx, inp):
        name = inp.get("name")
        if not isinstance(name, str) or not _ENV.match(name):
            raise self.Error(400, "BAD_REQUEST", "The name must be upper-case letters, digits and _.")
        value = self._value(inp)
        existing = next((k for k, r in self._rows(ctx, "-", ENV_KIND).items() if r["name"] == name), None)
        key = existing or "env" + secrets.token_hex(8)
        meta = self._store(ctx, "-", ENV_KIND, key, {"name": name, "origin": "", "auth": None}, value)
        return {"id": key, "name": name, "createdAt": meta["createdAt"], "updatedAt": meta["updatedAt"]}

    def env_remove(self, ctx, inp):
        self._wipe(ctx, "-", ENV_KIND, inp.get("id"))
        return {"ok": True}
