"""Cognito authenticator: token validation, group gating, revocation (pure, real crypto, no server)."""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_artifact_host.cognito import CognitoAuthenticator

POOL = "us-west-2_TestPool1"
ISS = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
CLIENT = "client-abc"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key=KEY, kid="k1"):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**d, "kid": kid, "alg": "RS256", "use": "sig"}]}


def token(user="alice", *, groups=("dev",), key=KEY, kid="k1", alg="RS256", **over):
    claims = {
        "iss": ISS,
        "sub": f"sub-{user}",
        "username": user,
        "token_use": "access",
        "client_id": CLIENT,
        "cognito:groups": list(groups),
        "exp": time.time() + 3600,
        "iat": time.time(),
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def auth(**kw):
    return CognitoAuthenticator(
        pool_id=POOL, client_ids=[CLIENT], required_group="dev", jwks_fetch=lambda: jwks(), **kw
    )


def test_valid_access_token_in_group_is_accepted():
    a = auth()
    uid, exp = a.verify(token("alice"))
    assert uid == "alice" and exp > time.time()


@pytest.mark.parametrize(
    "bad",
    [
        lambda: token(iss="https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Other"),
        lambda: token(exp=time.time() - 5),
        lambda: token(token_use="id"),
        lambda: token(client_id="someone-elses-client"),
        lambda: token(client_id=None),
        lambda: token(groups=()),
        lambda: token(groups=("Administrators", "llm-users")),  # other groups grant nothing
        lambda: token(key=OTHER),  # signed by a different key
        lambda: token(kid="unknown-kid"),
        lambda: jwt.encode(
            {
                "iss": ISS,
                "sub": "x",
                "username": "x",
                "token_use": "access",
                "client_id": CLIENT,
                "cognito:groups": ["dev"],
                "exp": time.time() + 99,
            },
            "secret",
            algorithm="HS256",
            headers={"kid": "k1"},
        ),  # algorithm confusion
        lambda: jwt.encode({"iss": ISS, "username": "x"}, None, algorithm="none"),
        lambda: "not.a.jwt",
        lambda: "",
    ],
)
def test_invalid_tokens_are_rejected(bad):
    assert auth().verify(bad()) is None


def test_aud_is_not_required_for_access_tokens():
    assert auth().verify(token(aud=None)) is not None


def test_removed_from_group_takes_effect_within_recheck_window():
    clock = [1000.0]
    groups = {"alice": ["dev"]}
    a = auth(group_lookup=lambda u: groups.get(u), now=lambda: clock[0])
    tok = token("alice", exp=time.time() + 99999)
    assert a.verify(tok) and a.exists("alice")
    groups["alice"] = []  # removed from dev (JWT still says dev)
    assert a.verify(tok) is not None  # cached for the recheck window
    clock[0] += 61
    assert a.verify(tok) is None and not a.exists("alice")


def test_directory_outage_fails_closed_for_new_users():
    def boom(_):
        raise RuntimeError("down")

    assert auth(group_lookup=boom).verify(token("alice")) is None


def test_jwks_refetch_is_rate_limited():
    calls = []
    clock = [10_000.0]
    a = CognitoAuthenticator(
        pool_id=POOL,
        client_ids=[CLIENT],
        required_group="dev",
        jwks_fetch=lambda: calls.append(1) or jwks(),
        now=lambda: clock[0],
    )
    for _ in range(20):
        a.verify(token(kid="nope"))
    assert len(calls) == 1


def test_config_validation():
    with pytest.raises(ValueError):
        CognitoAuthenticator(pool_id="bad", client_ids=["c"], required_group="dev")
    with pytest.raises(ValueError):
        CognitoAuthenticator(pool_id=POOL, client_ids=[], required_group="dev")


def id_token(user="alice", email="alice@example.org", **over):
    claims = {
        "iss": ISS,
        "sub": f"sub-{user}",
        "cognito:username": user,
        "token_use": "id",
        "aud": CLIENT,
        "email": email,
        "email_verified": True,
        "exp": time.time() + 3600,
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.mark.parametrize(
    "bad",
    [
        lambda: id_token(token_use="access"),
        lambda: id_token(email_verified=False),
        lambda: id_token(email_verified=None),
        lambda: id_token(aud="other-client"),
        lambda: id_token(user="mallory"),  # someone else's id token
        lambda: id_token(exp=time.time() - 1),
        lambda: id_token(email=None),
        lambda: id_token(
            email="\u212aim@example.org"
        ),  # KELVIN SIGN: .lower() would make it "kim@example.org"
        lambda: "junk",
    ],
)
def test_id_token_linking_rejects_bad_tokens(bad):
    assert auth().verify_id_token(bad(), user="alice") is None


def test_id_token_gives_normalised_email():
    assert (
        auth().verify_id_token(id_token(email="Alice@Example.ORG"), user="alice")
        == "alice@example.org"
    )
