"""Cognito authenticator for HCN (ADR-0007): the BinView user pool is the identity provider.

Accepts only Cognito *access* tokens (they carry ``client_id``, never ``aud``):
RS256 signature against the pool's JWKS, issuer, expiry, ``token_use=access``,
an app-client allowlist, and membership of a required group (``cognito:groups``).
The group is the on/off switch for the whole feature. Because a JWT stays valid
until it expires, group membership is also re-checked against Cognito (at most
once per ``recheck_seconds`` per user) when a lookup is available, so removing
someone from the group takes effect within about a minute, not at token expiry.
MFA is a pool setting; this module cannot enforce it.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable

_POOL_RE = re.compile(r"^([a-z]{2}-[a-z]+-\d)_[A-Za-z0-9]+$")


class CognitoAuthenticator:
    def __init__(self, *, pool_id: str, client_ids: list[str], required_group: str,
                 jwks_fetch: Callable[[], dict] | None = None,
                 group_lookup: Callable[[str], list[str] | None] | None = None,
                 recheck_seconds: float = 60.0, now: Callable[[], float] = time.time):
        m = _POOL_RE.match(pool_id or "")
        if not m or not client_ids or not required_group:
            raise ValueError("pool_id, client_ids and required_group are required")
        self.region = m.group(1)
        self.issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{pool_id}"
        self.pool_id = pool_id
        self.client_ids = frozenset(client_ids)
        self.required_group = required_group
        self._fetch = jwks_fetch or self._fetch_jwks
        self._lookup = group_lookup
        self._recheck = recheck_seconds
        self._now = now
        self._keys: dict[str, dict] = {}
        self._keys_at = -1e18
        self._lock = threading.Lock()
        self._checked: dict[str, tuple[float, bool]] = {}
        self._seen: set[str] = set()

    # -- keys (URL is derived from config only: nothing from a token is fetched)
    def _fetch_jwks(self) -> dict:
        with urllib.request.urlopen(self.issuer + "/.well-known/jwks.json", timeout=5) as r:  # noqa: S310
            return json.loads(r.read())

    def _key_for(self, kid: str):
        import jwt
        with self._lock:
            # Refresh hourly, or once per 30 s when a token names an unknown kid.
            stale = self._now() - self._keys_at > 3600
            if kid not in self._keys and (stale or self._now() - self._keys_at > 30):
                try:
                    self._keys = {k["kid"]: k for k in self._fetch().get("keys", [])}
                    self._keys_at = self._now()
                except Exception:
                    if not self._keys:
                        return None
            jwk = self._keys.get(kid)
        return jwt.PyJWK.from_dict(jwk).key if jwk else None

    # -- UserAuthenticator
    def verify(self, token: str) -> tuple[str, float] | None:
        """(user id, token expiry epoch) for a valid access token in the required group."""
        import jwt
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return None
            key = self._key_for(header["kid"])
            if key is None:
                return None
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                options={"verify_aud": False, "require": ["exp", "iss", "sub"]})
        except Exception:
            return None
        if claims.get("token_use") != "access" or claims.get("client_id") not in self.client_ids:
            return None
        user = claims.get("username") or claims.get("sub")
        if not isinstance(user, str) or self.required_group not in (claims.get("cognito:groups") or []):
            return None
        if not self._still_in_group(user):
            return None
        self._seen.add(user)
        return user, float(claims["exp"])

    def verify_id_token(self, token: str, *, user: str) -> str | None:
        """Verified, lower-cased email from an ID token that belongs to ``user``.

        Used only to link a person's email to their (already authenticated) user id so
        they can be invited by email. The ID token must be ours (signature, issuer,
        audience in the client allowlist), unexpired, carry a verified email, and name
        the same Cognito user as the access token that authenticated the request."""
        import jwt
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return None
            key = self._key_for(header["kid"])
            if key is None:
                return None
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=self.issuer,
                                audience=list(self.client_ids),
                                options={"require": ["exp", "iss", "aud", "sub"]})
        except Exception:
            return None
        email = claims.get("email")
        verified = claims.get("email_verified") in (True, "true")
        if (claims.get("token_use") != "id" or not verified or not isinstance(email, str)
                or (claims.get("cognito:username") or claims.get("sub")) != user):
            return None
        return email.strip().lower()

    def user_for(self, token: str) -> str | None:
        got = self.verify(token)
        return got[0] if got else None

    def mark_known(self, user_ids) -> None:
        """Users recorded in the collab log before this process started (restart safety)."""
        self._seen.update(user_ids)

    def exists(self, user_id: str) -> bool:
        """Known and still in the group (stream/agent re-checks come through here)."""
        if self._lookup is None:
            return user_id in self._seen
        return self._still_in_group(user_id)

    def _still_in_group(self, user: str) -> bool:
        if self._lookup is None:
            return True
        now = self._now()
        hit = self._checked.get(user)
        if hit and now - hit[0] < self._recheck:
            return hit[1]
        try:
            groups = self._lookup(user)
        except Exception:
            groups = None  # fail closed: an unreachable directory grants nothing new
            if hit:
                return hit[1] and now - hit[0] < 5 * self._recheck
        ok = bool(groups) and self.required_group in groups
        self._checked[user] = (now, ok)
        return ok


class SyncedDirectory:
    """Cognito JWKS and group membership read from files a host-side job refreshes.

    Lets the public container run with no AWS credentials and no outbound network at all.
    Fail closed: membership older than ``max_age`` seconds raises, so nobody new is admitted
    (and a known user is denied after the grace window) if the sync stops.
    """

    def __init__(self, directory: str, group: str, *, max_age: float = 600.0,
                 now: Callable[[], float] = time.time):
        self.dir, self.group, self.max_age, self._now = directory, group, max_age, now

    def _load(self, name: str) -> dict:
        with open(f"{self.dir}/{name}", encoding="utf-8") as fh:
            return json.load(fh)

    def jwks(self) -> dict:
        return self._load("jwks.json")

    def groups(self, user: str) -> list[str] | None:
        data = self._load("members.json")
        if self._now() - float(data["updated"]) > self.max_age:
            raise RuntimeError("group membership sync is stale")
        return [self.group] if user in data["groups"].get(self.group, []) else []


def boto_group_lookup(pool_id: str, region: str) -> Callable[[str], list[str] | None]:
    """Group lookup through AdminListGroupsForUser (needs cognito-idp:AdminListGroupsForUser)."""
    import boto3
    client = boto3.client("cognito-idp", region_name=region)

    def lookup(user: str) -> list[str] | None:
        try:
            resp = client.admin_list_groups_for_user(UserPoolId=pool_id, Username=user)
        except client.exceptions.UserNotFoundException:
            return None
        return [g["GroupName"] for g in resp.get("Groups", [])]
    return lookup
