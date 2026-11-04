# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Rakazo routines over the assistant routines: schedule mapping, test runs, history and the due-routine runner."""
from datetime import timedelta

import pytest

from herdr_engineering.control_plane.rakazo_routines import cron_to_trigger
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    alice,
    make_server,
    new_bot,
    srv,
    wait_idle,
)
from tests.control_plane.test_rakazo_contract import validate


def rpc(api, proc, inp, token):
    status, _, _ = api.rpc(proc, inp, token)
    return status


def make(api, alice, bot, **extra):
    body = {"botId": bot["id"], "name": "Digest", "prompt": "summarise the day", "crons": ["*/15 * * * *"],
            "timezone": "UTC", "notify": True, "active": True, "webhookEnabled": False, "githubEnabled": False,
            "messageProvider": None, "modelProvider": None, "modelId": None, "thinkingLevel": None, "watch": None}
    body.update(extra)
    return api.ok("routines/create", body, alice)


@pytest.mark.parametrize("cron,expected", [
    ("*/5 * * * *", ("interval", {"every": "minutes", "amount": 5})),
    ("0 */6 * * *", ("interval", {"every": "hours", "amount": 6})),
    ("30 7 * * *", ("daily", {"time": "07:30", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]})),
    ("0 9 * * 1-5", ("daily", {"time": "09:00", "days": ["mon", "tue", "wed", "thu", "fri"]})),
    ("0 9 * * 0,6", ("daily", {"time": "09:00", "days": ["sat", "sun"]})),
])
def test_supported_crons_map_exactly(cron, expected):
    assert cron_to_trigger(cron) == expected


@pytest.mark.parametrize("cron", ["0 9 1 * *", "*/5 9 * * *", "0 9 * * 1-8", "nope", "0 25 * * *", "5 */2 * * *"])
def test_other_crons_are_refused_not_approximated(cron):
    assert cron_to_trigger(cron) is None


def test_create_list_update_remove(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    created = make(api, alice, bot)
    validate("routines/create", created)
    assert created["active"] is True and created["nextRunAt"] and created["crons"] == ["*/15 * * * *"]
    listing = api.ok("routines/list", {"botId": bot["id"]}, alice)
    validate("routines/list", listing)
    mentions = api.ok("routines/listForMentions", {"botIds": [bot["id"], "nope"]}, alice)
    validate("routines/listForMentions", mentions)
    assert [len(m["routines"]) for m in mentions] == [1, 0]
    out = api.ok("routines/update", {"routineId": created["id"], "name": "Evening", "crons": ["0 18 * * 1-5"],
                                     "active": False}, alice)
    validate("routines/update", out)
    assert (out["name"], out["active"], out["crons"]) == ("Evening", False, ["0 18 * * 1-5"])
    validate("routines/remove", api.ok("routines/remove", {"routineId": created["id"]}, alice))
    assert api.ok("routines/list", {"botId": bot["id"]}, alice) == []
    assert created["id"] in srv[0].rakazo.svc.store.routines          # hidden, never deleted
    assert rpc(api, "routines/update", {"routineId": created["id"], "name": "x"}, alice) == 404


def test_unwired_triggers_and_bad_schedules_are_refused(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    for extra in ({"webhookEnabled": True}, {"githubEnabled": True}, {"crons": ["0 9 1 * *"]},
                  {"crons": ["* * * * *", "0 9 * * *"]}, {"crons": [], "active": True}, {"timezone": "Mars/Base"},
                  {"thinkingLevel": "extreme"}):
        assert rpc(api, "routines/create", {"botId": bot["id"], "name": "x", "prompt": "p", "crons": ["*/5 * * * *"],
                                            **extra}, alice) == 400, extra


def test_test_run_sends_the_prompt_and_history_lists_it(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    routine = make(api, alice, bot, active=False)
    sent = api.ok("routines/testRun", {"routineId": routine["id"], "clientNonce": "t1"}, alice)
    validate("routines/testRun", sent)
    wait_idle(api, alice, bot["id"])
    history = api.ok("routines/history", {"routineId": routine["id"]}, alice)
    validate("routines/history", history)
    assert [r["id"] for r in history["runs"]] == [sent["runId"]] and history["nextCursor"] is None
    texts = [m["blocks"][0]["text"] for m in api.ok("threads/messages", {"botId": bot["id"], "limit": 20}, alice)["messages"]
             if m["role"] == "user"]
    assert texts == ["summarise the day"]


def test_runner_fires_only_due_active_routines_once(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    routine = make(api, alice, bot)
    stored = svr.rakazo.svc.store.routines[routine["id"]]
    assert svr.rakazo.run_due_routines() == 0                        # not due yet
    stored.next_at = stored.next_at - timedelta(hours=1)
    assert svr.rakazo.run_due_routines() == 1
    wait_idle(api, alice, bot["id"])
    assert svr.rakazo.run_due_routines() == 0                        # the bucket moved on
    assert len(api.ok("routines/history", {"routineId": routine["id"]}, alice)["runs"]) == 1
