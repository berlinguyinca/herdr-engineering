# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""The per-space ticket board, comments and onboarding acknowledgements."""
from tests.control_plane.test_rakazo_bridge import alice, make_server, new_bot, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import validate


def rpc(api, proc, inp, token):
    return api.rpc(proc, inp, token)[0]


def test_board_and_ticket_lifecycle(srv, alice):
    _, api = srv
    assert api.ok("me", {}, alice)["ticketBoardEnabled"] is True
    board = api.ok("boards/get", {}, alice)
    validate("boards/get", board)
    validate("boards/list", api.ok("boards/list", {}, alice))
    renamed = api.ok("boards/rename", {"name": "Lab work", "ticketPrefix": "lab"}, alice)
    validate("boards/rename", renamed)
    assert renamed["id"] == board["id"] and renamed["ticketPrefix"] == "LAB"
    bot = new_bot(api, alice)
    made = api.ok("tickets/create", {"title": "Fix the mass list", "description": "d", "priority": "high",
                                     "assigneeBotId": bot["id"]}, alice)
    validate("tickets/create", made)
    assert made["ref"] == "LAB-1" and made["status"] == "todo" and made["assigneeBotId"] == bot["id"]
    second = api.ok("tickets/create", {"title": "Second", "priority": "normal"}, alice)
    assert second["ref"] == "LAB-2" and second["assigneeBotId"] is None
    got = api.ok("tickets/get", {"ref": "lab-1"}, alice)
    validate("tickets/get", got)
    assert got["id"] == made["id"]
    moved = api.ok("tickets/move", {"id": made["id"], "status": "done"}, alice)
    validate("tickets/move", moved)
    assert moved["completedAt"] is not None
    upd = api.ok("tickets/update", {"id": made["id"], "title": "Renamed", "status": "doing", "assigneeBotId": bot["id"]}, alice)
    validate("tickets/update", upd)
    assert upd["completedAt"] is None and upd["title"] == "Renamed"
    listing = api.ok("tickets/list", {}, alice)
    validate("tickets/list", listing)
    assert [t["number"] for t in listing["tickets"]] == [2, 1]
    assert [t["number"] for t in api.ok("tickets/list", {"status": "doing"}, alice)["tickets"]] == [1]
    assert [t["number"] for t in api.ok("tickets/list", {"q": "second"}, alice)["tickets"]] == [2]
    assert [t["number"] for t in api.ok("tickets/list", {"assigneeBotId": bot["id"]}, alice)["tickets"]] == [1]


def test_comments_and_validation(srv, alice):
    _, api = srv
    t = api.ok("tickets/create", {"title": "T", "priority": "normal"}, alice)
    c = api.ok("tickets/comment", {"ticketId": t["id"], "body": "first"}, alice)
    validate("tickets/comment", c)
    api.ok("tickets/comment", {"ticketId": t["id"], "body": "second"}, alice)
    listing = api.ok("tickets/comments", {"ticketId": t["id"]}, alice)
    validate("tickets/comments", listing)
    assert [x["body"] for x in listing] == ["first", "second"] and listing[0]["authorName"] == "alice"
    other = api.ok("tickets/create", {"title": "U", "priority": "normal"}, alice)
    assert api.ok("tickets/comments", {"ticketId": other["id"]}, alice) == []
    for proc, inp in (("tickets/create", {"title": "", "priority": "normal"}),
                      ("tickets/create", {"title": "x", "priority": "meh"}),
                      ("tickets/create", {"title": "x", "priority": "normal", "assigneeBotId": "nope"}),
                      ("tickets/move", {"id": t["id"], "status": "nope"}),
                      ("tickets/comment", {"ticketId": t["id"], "body": ""}),
                      ("boards/rename", {"ticketPrefix": "9bad"})):
        assert rpc(api, proc, inp, alice) in (400, 404), (proc, inp)
    assert rpc(api, "tickets/get", {"id": "missing"}, alice) == 404


def test_each_space_has_its_own_board(srv, alice):
    _, api = srv
    api.ok("tickets/create", {"title": "Herdr ticket", "priority": "normal"}, alice)
    other = api.ok("spaces/create", {"name": "Other"}, alice)
    status, body, _ = api.rpc("tickets/list", {}, alice, **{"x-rakazo-space-id": other["id"]})
    assert status == 200 and body["json"]["tickets"] == []


def test_onboarding_acknowledgements_and_survey(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    validate("onboarding/start", api.ok("onboarding/start", {"botId": bot["id"]}, alice))
    validate("onboarding/promptFocus", api.ok("onboarding/promptFocus", {"botId": bot["id"]}, alice))
    validate("onboarding/choose", api.ok("onboarding/choose", {"botId": bot["id"], "optionId": "a"}, alice))
    validate("onboarding/dismissFocus", api.ok("onboarding/dismissFocus", {"botId": bot["id"]}, alice))
    validate("onboarding/appConnected", api.ok("onboarding/appConnected", {
        "botId": bot["id"], "provider": "gmail", "connectorId": "composio"}, alice))
    validate("onboarding/signupSurvey", api.ok("onboarding/signupSurvey", {"use": "work", "teamSize": "2-10"}, alice))
    assert rpc(api, "onboarding/signupSurvey", {"use": "nope"}, alice) == 400


def test_concurrent_creates_never_share_a_ticket_number(srv, alice):
    import threading
    _, api = srv
    results = []

    def make(i):
        results.append(api.ok("tickets/create", {"title": f"T{i}", "priority": "normal"}, alice)["number"])
    threads = [threading.Thread(target=make, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == list(range(1, 9))
