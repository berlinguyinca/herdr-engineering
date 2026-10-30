# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Rakazo bot management on the mobile bridge: edit, sections, archive/restore (never delete), instruction history."""
from tests.control_plane.test_rakazo_bridge import alice, make_server, new_bot, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import validate


def rpc(api, proc, inp, token):
    status, body, _ = api.rpc(proc, inp, token)
    return status, body["json"] if status == 200 else body


def test_update_edits_name_instructions_and_app_settings(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    out = api.ok("bots/update", {"botId": bot["id"], "name": "Scout", "instructions": "new", "description": "d",
                                 "pinned": True, "color": "#123456", "thinkingLevel": "high",
                                 "disabledBuiltinTools": ["x"]}, alice)
    validate("bots/update", out)
    assert (out["name"], out["instructions"], out["description"], out["pinned"], out["thinkingLevel"]) == (
        "Scout", "new", "d", True, "high")
    assert api.ok("bots/get", {"botId": bot["id"]}, alice)["color"] == "#123456"


def test_update_refuses_bad_values_and_stale_instructions(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    assert rpc(api, "bots/update", {"botId": bot["id"], "avatarSet": "nope"}, alice)[0] == 400
    assert rpc(api, "bots/update", {"botId": bot["id"], "instructions": "x", "expectedInstructions": "stale"}, alice)[0] == 409
    assert rpc(api, "bots/update", {"botId": bot["id"], "sectionId": "missing"}, alice)[0] == 404


def test_instruction_history_can_be_restored(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    api.ok("bots/update", {"botId": bot["id"], "instructions": "second"}, alice)
    history = srv[0].rakazo.prefs.items(next(iter(srv[0].rakazo.accounts)).account_id, "herdr", "bot_instructions")
    version = history[bot["id"]][0]["versionId"]
    out = api.ok("bots/restoreInstructions", {"botId": bot["id"], "versionId": version,
                                              "expectedInstructions": "second"}, alice)
    validate("bots/restoreInstructions", out)
    assert out["instructions"] == "be brief"
    assert rpc(api, "bots/restoreInstructions", {"botId": bot["id"], "versionId": version,
                                                 "expectedInstructions": "second"}, alice)[0] == 409


def test_archive_remove_and_restore_never_delete(srv, alice):
    _, api = srv
    one, two = new_bot(api, alice, "One"), new_bot(api, alice, "Two")
    validate("bots/archive", api.ok("bots/archive", {"botId": one["id"]}, alice))
    validate("bots/remove", api.ok("bots/remove", {"botId": two["id"], "deleteMemories": False}, alice))
    assert api.ok("bots/list", {}, alice) == []
    archived = api.ok("bots/listArchived", {}, alice)
    validate("bots/listArchived", archived)
    assert sorted(b["name"] for b in archived) == ["One", "Two"] and all(b["archivedAt"] for b in archived)
    validate("bots/restore", api.ok("bots/restore", {"botId": one["id"]}, alice))
    assert [b["name"] for b in api.ok("bots/list", {}, alice)] == ["One"]
    assert api.ok("bots/get", {"botId": one["id"]}, alice)["archivedAt"] is None


def test_sections_group_bots_and_removal_keeps_them(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    section = api.ok("botSections/create", {"name": "Work", "botId": bot["id"]}, alice)
    validate("botSections/create", section)
    assert api.ok("bots/get", {"botId": bot["id"]}, alice)["sectionId"] == section["id"]
    renamed = api.ok("botSections/update", {"sectionId": section["id"], "name": "Lab"}, alice)
    validate("botSections/update", renamed)
    listing = api.ok("botSections/list", {}, alice)
    validate("botSections/list", listing)
    assert [s["name"] for s in listing] == ["Lab"]
    validate("botSections/remove", api.ok("botSections/remove", {"sectionId": section["id"]}, alice))
    assert api.ok("botSections/list", {}, alice) == []
    assert api.ok("bots/get", {"botId": bot["id"]}, alice)["sectionId"] is None
    assert rpc(api, "botSections/remove", {"sectionId": section["id"]}, alice)[0] == 404


def test_duplicate_computer_unread_and_misc(srv, alice):
    _, api = srv
    bot = new_bot(api, alice)
    api.ok("bots/update", {"botId": bot["id"], "color": "#abcdef"}, alice)
    copy = api.ok("bots/duplicate", {"botId": bot["id"]}, alice)
    validate("bots/duplicate", copy)
    assert copy["id"] != bot["id"] and copy["instructions"] == "be brief" and copy["color"] == "#abcdef"
    validate("bots/setComputer", api.ok("bots/setComputer", {"botId": bot["id"], "mode": "dedicated"}, alice))
    assert api.ok("bots/get", {"botId": bot["id"]}, alice)["computerMode"] == "dedicated"
    validate("threads/markUnread", api.ok("threads/markUnread", {"botId": bot["id"], "messageId": "m"}, alice))
    assert api.ok("bots/get", {"botId": bot["id"]}, alice)["unread"] is True
    api.ok("threads/markRead", {"botId": bot["id"]}, alice)
    assert api.ok("bots/get", {"botId": bot["id"]}, alice)["unread"] is False
    validate("bots/reorder", api.ok("bots/reorder", {"botIds": [bot["id"]]}, alice))
    validate("threads/endCall", api.ok("threads/endCall", {"botId": bot["id"], "callId": "c1"}, alice))
