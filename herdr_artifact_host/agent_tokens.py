"""Agent tokens: the bearer secret an agent presents at ``/mcp/artifacts``.

A token is ``hat_`` plus 256 random bits.  It is returned once, by ``mint``; the database keeps only its SHA-256 digest
(the secret is high entropy, so an unsalted digest is enough), bound to one member.  Lookup is by digest and the digest
is compared in constant time; unknown, revoked and malformed tokens are indistinguishable (``authenticate`` -> None).
Listing never returns a secret or a digest.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime
from typing import Any, Final

import asyncpg

from .db import LOCK_NS_AGENT_TOKEN

TOKEN_PREFIX: Final = "hat_"
TOKEN_RE: Final = re.compile(r"hat_[A-Za-z0-9_-]{43}")
TOKEN_ID_RE: Final = re.compile(r"atk_[0-9a-f]{16}")
MAX_ACTIVE_TOKENS: Final = 10
MAX_LABEL: Final = 80


class TokenError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def digest_of(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class AgentTokenStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def mint(self, member: str, label: str) -> dict[str, Any]:
        label = label.strip()
        if len(label) > MAX_LABEL or any(ord(c) < 32 for c in label):
            raise TokenError(400, "bad_request", "label is invalid")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = "atk_" + secrets.token_hex(8)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1, hashtext($2))", LOCK_NS_AGENT_TOKEN, member
            )
            active = await conn.fetchval(
                "SELECT count(*) FROM agent_tokens WHERE member_id=$1 AND revoked_at IS NULL",
                member,
            )
            if int(active) >= MAX_ACTIVE_TOKENS:
                raise TokenError(429, "token_cap", "too many active agent tokens; revoke one first")
            row = await conn.fetchrow(
                "INSERT INTO agent_tokens (token_id, member_id, label, digest) VALUES ($1,$2,$3,$4) "
                "RETURNING created_at",
                token_id,
                member,
                label,
                digest_of(token),
            )
        assert row is not None
        return {
            "token_id": token_id,
            "label": label,
            "token": token,  # the one and only place the secret is shown
            "created_at": _iso(row["created_at"]),
        }

    async def list(self, member: str) -> dict[str, Any]:
        rows = await self._pool.fetch(
            "SELECT token_id, label, created_at, last_used_at, revoked_at FROM agent_tokens "
            "WHERE member_id=$1 ORDER BY created_at DESC, token_id LIMIT 100",
            member,
        )
        return {
            "tokens": [
                {
                    "token_id": r["token_id"],
                    "label": r["label"],
                    "created_at": _iso(r["created_at"]),
                    "last_used_at": _iso(r["last_used_at"]),
                    "revoked": r["revoked_at"] is not None,
                }
                for r in rows
            ]
        }

    async def revoke(self, member: str, token_id: str) -> dict[str, Any]:
        done = await self._pool.execute(
            "UPDATE agent_tokens SET revoked_at=coalesce(revoked_at, now()) "
            "WHERE token_id=$1 AND member_id=$2",
            token_id,
            member,
        )
        if done != "UPDATE 1":
            raise TokenError(404, "not_found", "not found")
        return {"token_id": token_id, "revoked": True}

    async def authenticate(self, presented: str) -> tuple[str, str] | None:
        """``(token_id, member_id)`` for a live token, else None (the same answer for every kind of failure)."""
        if not TOKEN_RE.fullmatch(presented):
            return None
        want = digest_of(presented)
        row = await self._pool.fetchrow(
            "SELECT token_id, member_id, digest FROM agent_tokens WHERE digest=$1 AND revoked_at IS NULL",
            want,
        )
        if row is None or not hmac.compare_digest(row["digest"], want):
            return None
        await self._pool.execute(
            "UPDATE agent_tokens SET last_used_at=now() WHERE token_id=$1 "
            "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
            row["token_id"],
        )
        return row["token_id"], row["member_id"]
