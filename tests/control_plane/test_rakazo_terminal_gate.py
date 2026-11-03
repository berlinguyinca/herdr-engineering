"""Who may use terminal/*: the YAML owner only, and the shared request limiter never throttles keystrokes."""
import base64
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    ACCOUNTS, Api, alice, bridge_section, make_server, srv)

LIVE_INPUT = {"terminalId": "nope", "data": base64.b64encode(b"x").decode(), "n": 1}


def login(api, email, password):
    status, body, _ = api.raw("POST", "/api/auth/sign-in/email", {"email": email, "password": password})
    assert status == 200
    import json
    return json.loads(body)["token"]


def test_a_second_yaml_operator_is_refused(srv, alice):
    from tests.control_plane.test_rakazo_bridge import PW_B
    _, api = srv
    bob = login(api, "bob@example.com", PW_B)
    for proc, inp in (("terminal/open", {"id": "a", "rows": 24, "cols": 80}), ("terminal/input", LIVE_INPUT),
                      ("terminal/resize", {"terminalId": "x", "rows": 24, "cols": 80}),
                      ("terminal/close", {"terminalId": "x"}), ("terminal/subscribe", {"terminalId": "x", "cursor": 0})):
        assert api.rpc(proc, inp, bob)[0] == 403, proc
    assert api.rpc("terminal/open", {"id": "a", "rows": 24, "cols": 80}, alice)[0] == 404    # the owner gets as far as the agent


def test_a_cognito_derived_operator_is_refused(srv):
    svr, api = srv
    bridge = svr.rakazo
    acct = rb.Account("cog1", "c@example.com", "rakazo:cog1", "operator", bridge._dummy, 0.0, "cog-user")
    bridge._by_id[acct.account_id] = acct
    bridge._by_principal[acct.principal_id] = acct
    bridge._cognito_users[acct.account_id] = "cog-user"
    bridge.cognito = SimpleNamespace(still_member=lambda user: True)
    token = bridge.tokens.mint(acct.account_id, "127.0.0.1")
    assert api.rpc("terminal/open", {"id": "a", "rows": 24, "cols": 80}, token)[0] == 403
    assert not bridge.is_owner(acct)


def test_the_owner_is_the_first_yaml_operator(srv):
    svr, _ = srv
    bridge = svr.rakazo
    first, second = bridge.accounts[0], bridge.accounts[1]
    assert bridge.is_owner(first) and not bridge.is_owner(second)


def test_input_and_resize_are_not_throttled_but_everything_else_still_is(srv, alice):
    _, api = srv
    codes = {api.rpc("terminal/input", LIVE_INPUT, alice)[0] for _ in range(300)}
    assert codes == {404}                                     # unknown terminal, never 429
    codes = {api.rpc("terminal/resize", {"terminalId": "x", "rows": 24, "cols": 80}, alice)[0] for _ in range(130)}
    assert codes == {404}
    seen = {api.rpc("me", None, alice)[0] for _ in range(130)}
    assert 429 in seen                                        # the limiter still applies to every other procedure
