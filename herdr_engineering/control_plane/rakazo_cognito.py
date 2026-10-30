"""Optional sign-in to the Rakazo bridge with the lab's Cognito user pool (the pool BinView and the LLM gateway use).

The app posts an email and password to ``/api/auth/sign-in/email``. When ``rakazo_bridge.cognito`` is configured and the
email is not one of the local accounts, the password is checked by Cognito (``InitiateAuth`` with ``USER_PASSWORD_AUTH``,
which needs no AWS credentials), and the returned access token must verify against the pool's JWKS (signature, issuer,
expiry, app-client allowlist) AND carry the configured ``required_group``. The ID token supplies the verified email.

Pool members get the chat-only ``family`` role unless ``role: operator`` is configured. Nothing here widens who may sign in
beyond that one group, there is no default group, and with ``group_check`` on (the
default) membership is re-read from Cognito while the session lives, so removing someone from the group ends their
session within about a minute. The person acts as the collaboration user named by their Cognito username, which is the
same identity HCN gives them, so rooms and agents are exactly the ones they already have there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .collab_cognito import CognitoAuthenticator, boto_group_lookup

log = logging.getLogger("herdr.rakazo.cognito")
_POOL = re.compile(r"^([a-z]{2}-[a-z]+-\d)_[A-Za-z0-9]+$")
_CLIENT = re.compile(r"^[A-Za-z0-9]{10,64}$")
_GROUP = re.compile(r"^[A-Za-z0-9_.+=,@-]{1,128}$")
KEYS = frozenset({"pool_id", "client_ids", "required_group", "group_check", "role"})


def cognito_config(raw: Any) -> dict[str, Any] | None:
    """Validate ``control_plane.rakazo_bridge.cognito``; None when not configured."""
    if raw is None:
        return None
    where = "control_plane.rakazo_bridge.cognito"
    if not isinstance(raw, dict) or set(raw) - KEYS:
        raise ValueError(f"{where} must be a mapping with only {', '.join(sorted(KEYS))}")
    pool, clients, group = raw.get("pool_id"), raw.get("client_ids"), raw.get("required_group")
    if not isinstance(pool, str) or not _POOL.match(pool):
        raise ValueError(f"{where}.pool_id must be a Cognito user pool id such as us-west-2_AbCdEf")
    if (not isinstance(clients, list) or not 1 <= len(clients) <= 10
            or not all(isinstance(c, str) and _CLIENT.match(c) for c in clients)):
        raise ValueError(f"{where}.client_ids must list 1 to 10 app client ids")
    if not isinstance(group, str) or not _GROUP.match(group):
        raise ValueError(f"{where}.required_group is required: only members of that group may sign in")
    check = raw.get("group_check", True)
    if not isinstance(check, bool):
        raise ValueError(f"{where}.group_check must be true or false")
    role = raw.get("role", "family")
    if role not in ("family", "operator"):
        raise ValueError(f"{where}.role must be family (chat only, the default) or operator")
    return {"pool_id": pool, "client_ids": tuple(clients), "required_group": group, "group_check": check, "role": role}


class CognitoUnavailable(Exception):
    """Cognito could not be reached; the bridge answers 503 and counts nothing against the sender."""


def initiate_auth(region: str, client_id: str, email: str, password: str, timeout: float = 8.0) -> dict | None:
    """``USER_PASSWORD_AUTH`` against the public endpoint. None for any refusal (wrong password, unknown user, a
    challenge such as a required password change or MFA); CognitoUnavailable when the service cannot be reached."""
    body = json.dumps({"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": client_id,
                       "AuthParameters": {"USERNAME": email, "PASSWORD": password}}).encode()
    req = urllib.request.Request(f"https://cognito-idp.{region}.amazonaws.com/", data=body, method="POST", headers={
        "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoint from config
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            kind = ""
            try:
                kind = str(json.loads(exc.read()).get("__type", ""))
            except Exception:  # noqa: BLE001
                pass
            if "InvalidParameter" in kind:       # e.g. the app client does not allow USER_PASSWORD_AUTH
                log.warning("rakazo cognito: the app client refuses this sign-in flow (%s)", kind)
            return None
        raise CognitoUnavailable from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise CognitoUnavailable from None
    result = data.get("AuthenticationResult") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not result.get("AccessToken") or not result.get("IdToken"):
        if isinstance(data, dict) and data.get("ChallengeName"):
            log.info("rakazo cognito: sign-in needs a %s challenge; finish it in BinView first", data["ChallengeName"])
        return None
    return result


class CognitoSignIn:
    def __init__(self, cfg: dict[str, Any], *, login: Callable[..., dict | None] | None = None,
                 authenticator: CognitoAuthenticator | None = None, jwks_fetch=None, group_lookup=None):
        self.cfg = cfg
        self.region = _POOL.match(cfg["pool_id"]).group(1)
        lookup = group_lookup
        if lookup is None and cfg["group_check"]:
            lookup = boto_group_lookup(cfg["pool_id"], self.region)       # ImportError/credential errors fail at startup
        self.auth = authenticator or CognitoAuthenticator(
            pool_id=cfg["pool_id"], client_ids=list(cfg["client_ids"]), required_group=cfg["required_group"],
            jwks_fetch=jwks_fetch, group_lookup=lookup)
        self._login = login or initiate_auth

    def authenticate(self, email: str, password: str) -> dict[str, str] | None:
        """{email, user} for a verified member of the required group, else None."""
        result = self._login(self.region, self.cfg["client_ids"][0], email, password)
        if not result:
            return None
        claims = self.auth.verify_claims(result["AccessToken"])           # signature, issuer, client, group
        if claims is None:
            return None
        verified = self.auth.verify_id_token(result["IdToken"], user=claims["user"])
        if verified is None or verified != email:
            return None
        return {"email": verified, "user": claims["user"]}

    def still_member(self, user: str) -> bool:
        return self.auth.exists(user)

    @staticmethod
    def account_id(email: str) -> str:
        return hashlib.sha256(b"rakazo-bridge-account:" + email.encode()).hexdigest()[:16]
