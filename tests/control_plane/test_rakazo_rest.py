# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""The rest of the Rakazo procedures: every upstream procedure answers with a parseable result or a reasoned refusal."""
import json
from pathlib import Path

import jsonschema

from herdr_engineering.control_plane import rakazo_rest as rr
from tests.control_plane.test_rakazo_bridge import alice, make_server, new_bot, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import PROCS, validate

REAL = ("scratchpad/list", "scratchpad/create", "scratchpad/update", "scratchpad/remove", "trustedLinkSites/list",
        "trustedLinkSites/trust", "trustedLinkSites/remove", "links/favicon", "preferences/update", "bootstrap",
        "billing/status", "export/bot", "preferences/adoptTimezone")


def rpc(api, proc, inp, token):
    status, body, _ = api.rpc(proc, inp, token)
    return status, body


def test_the_shipped_defaults_are_current_and_valid():
    assert sorted(PROCS) == list(rr.UPSTREAM), "run scripts/rakazo_defaults_export.py"
    assert set(rr.DEFAULTS) == set(rr.READS)
    for name, value in rr.DEFAULTS.items():
        jsonschema.Draft202012Validator(PROCS[name]["output"]).validate(value)


def test_every_upstream_procedure_has_an_answer(srv):
    assert set(srv[0].rakazo._procs) | {"threads/subscribe", "health"} >= set(PROCS)


def test_reads_answer_with_nothing_configured(srv, alice):
    _, api = srv
    for name in sorted(rr.READS):
        if name in ("artifacts/list",):
            continue
        inp = {"botId": "b", "role": "chat", "limit": 5, "query": "q", "usePublicCatalog": False, "status": "open"}
        out = api.ok(name, inp, alice)
        validate(name, out)


def test_other_changes_are_refused_with_a_reason_and_lookups_are_404(srv, alice):
    _, api = srv
    status, body = rpc(api, "vault/put", {"name": "k", "value": "v"}, alice)
    assert status == 400 and "vault" in body["json"]["message"].lower()
    assert rpc(api, "models/connect", {}, alice)[0] == 400
    assert rpc(api, "tickets/get", {"ticketId": "t"}, alice)[0] == 404
    assert rpc(api, "threads/clear", {"botId": "b"}, alice)[0] == 400


def test_scratchpad_round_trip_and_hide(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    item = api.ok("scratchpad/create", {"botId": bot["id"], "title": "idea", "status": "open", "notes": ""}, alice)
    validate("scratchpad/create", item)
    out = api.ok("scratchpad/update", {"itemId": item["id"], "status": "done", "notes": "n"}, alice)
    validate("scratchpad/update", out)
    assert api.ok("scratchpad/list", {"botId": bot["id"]}, alice) == []
    listing = api.ok("scratchpad/list", {"botId": bot["id"], "includeDone": True}, alice)
    validate("scratchpad/list", listing)
    assert [i["notes"] for i in listing] == ["n"]
    validate("scratchpad/remove", api.ok("scratchpad/remove", {"itemId": item["id"]}, alice))
    assert api.ok("scratchpad/list", {"botId": bot["id"], "includeDone": True}, alice) == []
    assert rpc(api, "scratchpad/update", {"itemId": item["id"], "title": "x"}, alice)[0] == 404


def test_trusted_sites_preferences_billing_favicon(srv, alice):
    _, api = srv
    site = api.ok("trustedLinkSites/trust", {"url": "https://Example.org/path?q=1"}, alice)
    validate("trustedLinkSites/trust", site)
    listing = api.ok("trustedLinkSites/list", {}, alice)
    validate("trustedLinkSites/list", listing)
    assert site["site"] == "example.org" and listing == ["example.org"]
    validate("trustedLinkSites/remove", api.ok("trustedLinkSites/remove", {"site": "example.org"}, alice))
    assert rpc(api, "trustedLinkSites/trust", {"url": "///"}, alice)[0] == 400
    me = api.ok("preferences/update", {"avatarStyle": "organic", "timezone": "Europe/Berlin"}, alice)
    validate("preferences/update", me)
    assert api.ok("me", {}, alice)["timezone"] == "Europe/Berlin" and me["avatarStyle"] == "organic"
    validate("preferences/adoptTimezone", api.ok("preferences/adoptTimezone", {"timezone": "UTC"}, alice))
    assert rpc(api, "preferences/update", {"timezone": "Mars/Base"}, alice)[0] == 400
    validate("billing/status", api.ok("billing/status", {}, alice))
    validate("links/favicon", api.ok("links/favicon", {"origin": "https://example.org"}, alice))


def test_bootstrap_and_bot_export(srv, alice):
    _, api = srv
    bot = new_bot(api, alice, "Scout")
    out = api.ok("bootstrap", {"botId": bot["id"]}, alice)
    validate("bootstrap", out)
    assert [b["name"] for b in out["bots"]] == ["Scout"] and out["thread"] is not None
    validate("bootstrap", api.ok("bootstrap", {}, alice))
    exported = api.ok("export/bot", {"botId": bot["id"]}, alice)
    validate("export/bot", exported)
    assert exported["bot"]["name"] == "Scout" and json.dumps(exported)
    assert Path(rr.__file__).with_name("rakazo_defaults.json").exists()
