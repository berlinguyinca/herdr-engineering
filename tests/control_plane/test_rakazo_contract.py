# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Every Rakazo procedure the bridge answers is checked against the upstream app's own output schema.

``fixtures/rakazo_contract.json`` is exported from ``packages/contracts/src/rpc.ts`` at a pinned upstream SHA by
``scripts/rakazo_contract_export.ts`` (zod 4 ``toJSONSchema``). A response that omits a field the app requires, or has the
wrong type, fails here instead of on a phone. Adding a procedure to the bridge without a case in ``CASES`` fails too."""
import json
from pathlib import Path

import jsonschema
import pytest

# fixtures and helpers are shared with the bridge suite, imported the way the rest of tests/control_plane does
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    alice,
    make_approval,
    make_server,
    new_bot,
    send,
    srv,
)

CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "rakazo_contract.json").read_text())
PROCS = CONTRACT["procedures"]
STREAMED = {"threads/subscribe"}          # an event stream, not a JSON body: covered by test_rakazo_bridge
NEEDS_SETUP = {"threads/answer"}          # needs a pending approval: its own test below


def validate(proc, value):
    schema = PROCS[proc]["output"]
    assert schema is not None, f"{proc} has no output schema in the contract"
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)


# proc -> input builder(ctx); ctx has api, token and a bot id. Inputs match what apps/mobile sends.
CASES = {
    "me": lambda c: {},
    "spaces/list": lambda c: {},
    "bots/list": lambda c: {},
    "bots/get": lambda c: {"botId": c["bot"]},
    "bots/create": lambda c: {"name": "Second", "description": "", "instructions": "x", "notifyOnFinish": True,
                              "computerMode": "team"},
    "threads/head": lambda c: {"botId": c["bot"]},
    "threads/get": lambda c: {"botId": c["bot"]},
    "threads/messages": lambda c: {"botId": c["bot"], "limit": 20},
    "threads/send": lambda c: {"botId": c["bot"], "text": "hello", "clientNonce": "contract-1"},
    "threads/stop": lambda c: {"botId": c["bot"]},
    "threads/markRead": lambda c: {"botId": c["bot"]},
    "runs/list": lambda c: {"filter": "recent"},
    "aiConsent/status": lambda c: {"botId": c["bot"]},
    "aiConsent/allow": lambda c: {"botId": c["bot"]},
    "aiConsent/revoke": lambda c: {"botId": c["bot"]},
    "notifications/registerPush": lambda c: {"token": "ExponentPushToken-abcdef123456"},
    "notifications/unregisterPush": lambda c: {},
    "analytics/screen": lambda c: {"screen": "thread"},
}


def test_the_contract_fixture_is_pinned_and_complete():
    assert len(CONTRACT["sha"]) == 40 and CONTRACT["upstream"] == "elie222/rakazo"
    assert len(PROCS) > 200 and PROCS["threads/send"]["output"] is not None


def test_every_bridge_procedure_exists_upstream_and_has_a_contract_case(srv):
    implemented = set(srv[0].rakazo._procs) - STREAMED
    assert implemented <= set(PROCS), f"the bridge answers procedures the app does not have: {implemented - set(PROCS)}"
    assert implemented == set(CASES) | NEEDS_SETUP, f"add or remove contract cases: {implemented ^ (set(CASES) | NEEDS_SETUP)}"


@pytest.mark.parametrize("proc", sorted(CASES))
def test_bridge_response_satisfies_the_app_contract(srv, alice, proc):
    _, api = srv
    ctx = {"bot": new_bot(api, alice)["id"]}
    validate(proc, api.ok(proc, CASES[proc](ctx), alice))
    if proc == "threads/send":                # the run settles before the fixture is torn down
        send(api, alice, ctx["bot"], "again")


def test_sign_in_and_capabilities_match_the_app_schemas(srv):
    _, api = srv
    status, body, _ = api.sign_in()
    assert status == 200 and {"token", "user"} <= set(body)
    status, raw, _ = api.raw("GET", "/api/auth/capabilities")
    assert status == 200
    caps = json.loads(raw)
    assert {"passwordAuth", "sso", "emailCode", "billing"} <= set(caps)


def test_an_approval_card_and_its_answer_satisfy_the_app_contract(srv, alice):
    svr, api = srv
    bot, sent, approval = make_approval(svr, api, alice)
    validate("threads/get", api.ok("threads/get", {"botId": bot["id"]}, alice))      # includes the ask card
    answer = api.ok("threads/answer", {"botId": bot["id"], "runId": sent["runId"],
                                       "messageId": "approval:" + approval.approval_id, "answer": "deny"}, alice)
    validate("threads/answer", answer)
    validate("threads/get", api.ok("threads/get", {"botId": bot["id"]}, alice))      # and the answered card
