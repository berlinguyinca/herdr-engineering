# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""The Rakazo vault: encrypted at rest, metadata-only lists, reveal on request, off without a key."""
import json
import logging

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane import rakazo_vault as rv
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    alice,
    bridge_section,
    make_server,
    new_bot,
    srv,
)
from tests.control_plane.test_rakazo_contract import validate

DEST = {"name": "netbird-key", "origin": "https://api.example.org", "auth": {"type": "bearer"}}


@pytest.fixture()
def keyed(srv, tmp_path):
    key = tmp_path / "vault.key"
    key.write_bytes(b"k" * 48)
    srv[0].rakazo.vault.cipher = rv.load_cipher(str(key))
    return srv


def rpc(api, proc, inp, token):
    return api.rpc(proc, inp, token)[0]


def test_without_a_key_the_vault_is_off_and_says_so(srv, alice):
    _, api = srv
    assert api.ok("vault/list", {}, alice) == []
    status, body, _ = api.rpc("vault/put", {"destination": DEST, "value": "s3cret"}, alice)
    assert status == 400 and "vault_key_file" in body["json"]["message"]
    assert rpc(api, "vault/reveal", {"name": "netbird-key"}, alice) == 400


def test_put_list_reveal_remove_with_encryption_at_rest(keyed, alice, caplog):
    svr, api = keyed
    caplog.set_level(logging.DEBUG)
    meta = api.ok("vault/put", {"destination": DEST, "value": "s3cret-value"}, alice)
    validate("vault/put", meta)
    listing = api.ok("vault/list", {}, alice)
    validate("vault/list", listing)
    assert [m["name"] for m in listing] == ["netbird-key"] and "s3cret-value" not in json.dumps(listing)
    stored = json.dumps(svr.rakazo.prefs.items(svr.rakazo.accounts[0].account_id, "herdr", "vault"))
    assert "s3cret-value" not in stored and '"ct"' in stored                    # only ciphertext is stored
    revealed = api.ok("vault/reveal", {"scope": "space", "name": "netbird-key"}, alice)
    validate("vault/reveal", revealed)
    assert revealed == {"value": "s3cret-value"}
    assert "s3cret-value" not in caplog.text
    assert api.ok("vault/list", {"scope": "account"}, alice) == []              # scopes are separate namespaces
    validate("vault/remove", api.ok("vault/remove", {"name": "netbird-key"}, alice))
    assert api.ok("vault/list", {}, alice) == [] and rpc(api, "vault/reveal", {"name": "netbird-key"}, alice) == 404
    stored = json.dumps(svr.rakazo.prefs.items(svr.rakazo.accounts[0].account_id, "herdr", "vault"))
    assert "gAAAA" not in stored                                                # the ciphertext itself is gone


def test_bad_destinations_and_values_are_refused(keyed, alice):
    _, api = keyed
    for dest in ({**DEST, "name": "Bad Name"}, {**DEST, "auth": {"type": "header"}}, {**DEST, "auth": {"type": "x"}},
                 {**DEST, "auth": {"type": "basic", "username": "a:b"}}, {**DEST, "extra": 1}):
        assert rpc(api, "vault/put", {"destination": dest, "value": "v"}, alice) == 400, dest
    assert rpc(api, "vault/put", {"destination": DEST, "value": ""}, alice) == 400
    ok = {**DEST, "name": "mail", "auth": {"type": "header", "name": "X-Api-Key"}}
    assert api.rpc("vault/put", {"destination": ok, "value": "v"}, alice)[0] == 200


def test_bot_and_agent_secrets(keyed, alice):
    _, api = keyed
    bot, other = new_bot(api, alice), new_bot(api, alice, "Other")
    meta = api.ok("botSecrets/put", {"botId": bot["id"], "destination": DEST, "value": "v1"}, alice)
    validate("botSecrets/put", meta)
    listing = api.ok("botSecrets/list", {"botId": bot["id"]}, alice)
    validate("botSecrets/list", listing)
    assert len(listing) == 1 and api.ok("botSecrets/list", {"botId": other["id"]}, alice) == []
    validate("botSecrets/remove", api.ok("botSecrets/remove", {"botId": bot["id"], "name": "netbird-key"}, alice))
    assert api.ok("botSecrets/list", {"botId": bot["id"]}, alice) == []
    env = api.ok("agentSecrets/put", {"name": "GH_TOKEN", "value": "ghp"}, alice)
    validate("agentSecrets/put", env)
    again = api.ok("agentSecrets/put", {"name": "GH_TOKEN", "value": "ghp2"}, alice)
    assert again["id"] == env["id"]
    listing = api.ok("agentSecrets/list", {}, alice)
    validate("agentSecrets/list", listing)
    assert [e["name"] for e in listing] == ["GH_TOKEN"]
    assert rpc(api, "agentSecrets/put", {"name": "lower", "value": "x"}, alice) == 400
    validate("agentSecrets/remove", api.ok("agentSecrets/remove", {"id": env["id"]}, alice))
    assert api.ok("agentSecrets/list", {}, alice) == []


def test_the_key_file_is_validated(tmp_path):
    assert rv.load_cipher(None) is None
    with pytest.raises(ValueError, match="cannot be read"):
        rv.load_cipher(str(tmp_path / "missing"))
    short = tmp_path / "short"
    short.write_bytes(b"x")
    with pytest.raises(ValueError, match="32 bytes"):
        rv.load_cipher(str(short))
    with pytest.raises(ValueError, match="vault_key_file"):
        rb.rakazo_bridge_config({**bridge_section(), "vault_key_file": 3})
