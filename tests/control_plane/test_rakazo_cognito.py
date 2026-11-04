# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Sign-in to the Rakazo bridge with the lab Cognito pool: group-gated, verified tokens, live revocation."""
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_engineering.control_plane import rakazo_cognito as rc
from herdr_engineering.control_plane.rakazo_bridge import rakazo_bridge_config
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    ACCOUNTS,
    PW_A,
    bridge_section,
    make_server,
)

POOL, CLIENT, GROUP = "us-west-2_TestPool1", "clientabc123456", "rakazo-users"
ISS = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CFG = {"pool_id": POOL, "client_ids": [CLIENT], "required_group": GROUP, "group_check": False}


def jwks():
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return {"keys": [{**d, "kid": "k1", "alg": "RS256", "use": "sig"}]}


def tokens(user="carol", email="carol@lab.example", groups=(GROUP,), **over):
    now = time.time()
    access = {"iss": ISS, "sub": "sub-" + user, "username": user, "token_use": "access", "client_id": CLIENT,
              "cognito:groups": list(groups), "exp": now + 3600}
    ident = {"iss": ISS, "sub": "sub-" + user, "cognito:username": user, "token_use": "id", "aud": CLIENT,
             "email": email, "email_verified": True, "exp": now + 3600}
    access.update(over.get("access", {}))
    ident.update(over.get("id", {}))
    enc = lambda c: jwt.encode(c, KEY, algorithm="RS256", headers={"kid": "k1"})  # noqa: E731
    return {"AccessToken": enc(access), "IdToken": enc(ident)}


def signin(login, groups_now=None):
    return rc.CognitoSignIn(CFG, login=login, jwks_fetch=jwks,
                            group_lookup=(lambda u: groups_now()) if groups_now else None)


def test_config_validation():
    assert rc.cognito_config(None) is None
    assert rc.cognito_config(CFG)["required_group"] == GROUP
    for bad in ({**CFG, "required_group": ""}, {k: v for k, v in CFG.items() if k != "required_group"},
                {**CFG, "pool_id": "nope"}, {**CFG, "client_ids": []}, {**CFG, "client_ids": ["a b"]},
                {**CFG, "group_check": "yes"}, {**CFG, "role": "admin"}, {**CFG, "extra": 1}, []):
        with pytest.raises(ValueError, match="cognito"):
            rc.cognito_config(bad)
    assert rakazo_bridge_config(bridge_section(cognito=CFG))["cognito"]["pool_id"] == POOL


def test_authenticate_requires_a_verified_token_in_the_group():
    ok = signin(lambda *a: tokens()).authenticate("carol@lab.example", "pw")
    assert ok == {"email": "carol@lab.example", "user": "carol"}
    for bad in (tokens(groups=("Administrators",)), tokens(access={"client_id": "other"}),
                tokens(access={"exp": time.time() - 5}), tokens(id={"email_verified": False}),
                tokens(id={"email": "mallory@lab.example"}), tokens(id={"cognito:username": "someoneelse", "sub": "x"})):
        assert signin(lambda *a, b=bad: b).authenticate("carol@lab.example", "pw") is None
    assert signin(lambda *a: None).authenticate("carol@lab.example", "pw") is None


def test_bridge_signs_in_a_pool_member_and_revokes_when_they_leave_the_group(make_server):
    server, api = make_server(bridge_section(cognito=CFG))
    groups = [GROUP]
    server.rakazo.cognito = signin(lambda *a: tokens(), lambda: list(groups))
    status, body, _ = api.sign_in("carol@lab.example", "whatever")
    assert status == 200 and body["user"]["email"] == "carol@lab.example"
    token = body["token"]
    assert api.rpc("me", {}, token)[0] == 200
    account = server.rakazo._by_id[next(iter(server.rakazo._cognito_users))]
    assert account.collab_user == "carol" and account.principal_id.startswith("rakazo:")
    assert account.role == "family"                              # chat only unless the operator configures role: operator
    groups.clear()                                              # removed from the group: the live session ends
    server.rakazo.cognito.auth._checked.clear()
    assert api.rpc("me", {}, token)[0] == 401


def test_a_wrong_pool_password_or_non_member_fails_like_any_wrong_password(make_server):
    server, api = make_server(bridge_section(cognito=CFG))
    server.rakazo.cognito = signin(lambda *a: None)
    assert api.sign_in("carol@lab.example", "bad")[0] == 401
    server.rakazo.cognito = signin(lambda *a: tokens(groups=("Users",)))
    assert api.sign_in("carol@lab.example", "pw")[0] == 401


def test_local_accounts_never_go_to_cognito_and_an_outage_is_a_503(make_server):
    server, api = make_server(bridge_section(cognito=CFG))
    calls = []

    def login(*a):
        calls.append(a)
        raise rc.CognitoUnavailable
    server.rakazo.cognito = signin(login)
    assert api.sign_in("alice@example.com", PW_A)[0] == 200 and not calls
    assert api.sign_in("alice@example.com", "wrong")[0] == 401 and not calls
    assert api.sign_in("carol@lab.example", "pw")[0] == 503 and len(calls) == 1


def test_a_pool_sign_in_does_not_erase_failed_guesses_at_a_local_password(make_server):
    server, api = make_server(bridge_section(cognito=CFG))
    server.rakazo.cognito = signin(lambda *a: tokens())
    for _ in range(4):
        assert api.sign_in("alice@example.com", "wrong")[0] == 401
    assert api.sign_in("carol@lab.example", "pw")[0] == 200       # a pool member from the same address
    assert api.sign_in("alice@example.com", "wrong")[0] == 401    # the 5th failure still counts ...
    assert api.sign_in("alice@example.com", PW_A)[0] == 429       # ... so the address is now locked out


def test_sync_dir_membership_is_read_from_files_and_fails_closed(tmp_path):
    cfg = rc.cognito_config({**CFG, "group_check": True, "sync_dir": str(tmp_path)})
    assert cfg["sync_dir"] == str(tmp_path)
    for bad in ("relative/dir", 5):
        with pytest.raises(ValueError, match="sync_dir"):
            rc.cognito_config({**CFG, "sync_dir": bad})
    members = tmp_path / "members.json"

    def write(users, age=0):
        members.write_text(json.dumps({"updated": time.time() - age, "groups": {GROUP: users}}))

    sign = rc.CognitoSignIn(cfg, login=lambda *a: tokens(), jwks_fetch=jwks)
    write(["carol"])
    assert sign.authenticate("carol@lab.example", "pw") == {"email": "carol@lab.example", "user": "carol"}
    write([])                                           # removed from the group: the live re-check sees it
    sign.auth._checked.clear()
    assert sign.still_member("carol") is False
    write(["carol"], age=3600)                          # a stalled sync admits nobody
    sign.auth._checked.clear()
    assert sign.still_member("carol") is False
    members.unlink()                                    # a missing file fails closed too
    sign.auth._checked.clear()
    assert sign.still_member("carol") is False


def test_a_dead_sync_job_revokes_members_already_cached_as_allowed(tmp_path):
    cfg = rc.cognito_config({**CFG, "group_check": True, "sync_dir": str(tmp_path)})
    members = tmp_path / "members.json"
    now = [time.time()]
    members.write_text(json.dumps({"updated": now[0], "groups": {GROUP: ["carol"]}}))
    sign = rc.CognitoSignIn(cfg, login=lambda *a: tokens(), jwks_fetch=jwks)
    sign.auth._now = lambda: now[0]
    assert sign.still_member("carol") is True           # cached as allowed
    # The sync job dies: the file goes stale. Once the cached answer is due for a re-check,
    # the member is out, with no extra grace on top of the file's own max age.
    members.write_text(json.dumps({"updated": time.time() - 3600, "groups": {GROUP: ["carol"]}}))
    now[0] += 3600
    assert sign.still_member("carol") is False
