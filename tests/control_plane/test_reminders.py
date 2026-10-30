"""Assistant reminders: parsing, exactly-once firing, late delivery, tools, routes and delivery (push + chat)."""
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from herdr_engineering.control_plane import reminders as rm
from herdr_engineering.control_plane.assistant import (
    TOOL_CATALOG,
    AssistantError,
    AssistantService,
    assistant_config,
    tool_sensitive,
)
from herdr_engineering.control_plane.reminders import (
    PostgresReminderPersistence,
    ReminderError,
    ReminderScheduler,
    ReminderStore,
    parse_when,
)
from tests.control_plane.test_assistant_apps_api import KEY, OTHER, VIEWER, _serve, call
from tests.control_plane.test_rakazo_push import TOKEN, make_server_p, rig, wait_for  # noqa: F401

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
LA = ZoneInfo("America/Los_Angeles")


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def fresh(clock=None):
    clock = clock or Clock()
    return ReminderStore(clock=clock), clock


# ---------------------------------------------------------------------------------------- the named behaviours
def test_due_reminder_fires_exactly_once():
    store, clock = fresh()
    r = store.add("t", "u", "call mum", NOW + timedelta(minutes=5))
    got = []
    sched = ReminderScheduler(store, lambda rem, late: got.append((rem.id, late)), clock)
    assert sched.tick() == 0 and got == []
    clock.t = NOW + timedelta(minutes=5, seconds=1)
    results = []
    threads = [threading.Thread(target=lambda: results.append(sched.tick())) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert got == [(r.id, False)] and sum(results) == 1
    assert sched.tick() == 0 and len(got) == 1                  # not re-fired later
    assert store.list("t", "u")[0].state == "fired"


def test_reminder_missed_during_downtime_fires_late_with_the_late_flag():
    store, clock = fresh()
    store.add("t", "u", "standup", NOW + timedelta(minutes=1))
    got = []
    clock.t = NOW + timedelta(hours=3)                          # the server was down
    ReminderScheduler(store, lambda rem, late: got.append(late), clock, interval=15).tick()
    assert got == [True]
    store.add("t", "u", "on time", clock.t + timedelta(seconds=60))
    clock.t += timedelta(seconds=75)                            # 15s after: within 2 intervals
    ReminderScheduler(store, lambda rem, late: got.append(late), clock, interval=15).tick()
    assert got == [True, False]


def test_past_time_is_refused_with_a_question_not_stored():
    store, _ = fresh()
    svc = _service(store)
    for when in ("2020-01-01 10:00", "in 0 minutes", "today 04:00", "2026-10-30T12:00:10Z"):
        with pytest.raises(AssistantError, match="Which time do you mean"):
            _tool(svc, "set_reminder", text="x", when=when)
    assert store.list("t", "u") == []


def test_cancel_only_by_the_owner():
    store, _ = fresh()
    r = store.add("t", "u", "mine", NOW + timedelta(hours=1))
    assert store.cancel(r.id, "t", "intruder") is False
    assert store.cancel(r.id, "other-tenant", "u") is False
    assert store.list("t", "u")[0].state == "pending"
    assert store.cancel(r.id, "t", "u") is True and store.cancel(r.id, "t", "u") is False
    got = []
    ReminderScheduler(store, lambda rem, late: got.append(rem), Clock(NOW + timedelta(hours=2))).tick()
    assert got == []                                             # a cancelled reminder never fires


def test_reminders_are_delivered_even_in_quiet_hours(make_server_p):  # noqa: F811
    svr, api, token, bot, sender = rig(make_server_p)
    api.ok("notifications/registerPush", {"token": TOKEN}, token)
    svr.rakazo.push._quiet = (0, 24)                             # always quiet hours
    user = svr.rakazo.accounts[0].principal_id
    tenant = svr.assistant.tenant_for(user)
    assert svr.rakazo.push.notify(svr.rakazo.accounts[0].account_id, "n", "t", "b", priority="normal") is False
    r = svr.reminders.add(tenant, user, "take the cake out", NOW)
    svr._deliver_reminder(r, False)
    assert wait_for(lambda: sender.sent)
    [msg] = sender.sent[0]
    assert msg["title"] == "Reminder" and msg["body"] == "take the cake out" and msg["data"]["kind"] == "reminder"
    assistant = svr.assistant.list_assistants(tenant_id=tenant, user_id=user)[0]
    thread = svr.assistant.list_threads(tenant_id=tenant, user_id=user, assistant_id=assistant.assistant_id)[0]
    texts = [m.content for m in svr.assistant.store.list_messages(thread.thread_id, tenant_id=tenant, user_id=user)]
    assert "Reminder: take the cake out" in texts
    svr._deliver_reminder(r, False)                              # the same reminder is never pushed twice
    r2 = svr.reminders.add(tenant, user, "second", NOW)
    svr._deliver_reminder(r2, True)                              # a late one says so
    assert wait_for(lambda: len(sender.sent) == 2)
    assert sender.sent[1][0]["body"].startswith("(late, was due 2026-10-30 12:00 UTC)")


def test_no_google_connection_needed():
    store, _ = fresh()
    svc = _service(store)
    assert svc.google_read is None and svc.google_write is None
    out = _tool(svc, "set_reminder", text="water plants", when="in 20 minutes")
    assert out["ok"] and out["reminder"]["state"] == "pending"
    assert _tool(svc, "list_reminders")["reminders"][0]["text"] == "water plants"


# ---------------------------------------------------------------------------------------------- parse_when
@pytest.mark.parametrize("text,expected", [
    ("in 20 minutes", NOW + timedelta(minutes=20)),
    ("In 2 hours", NOW + timedelta(hours=2)),
    ("in 3 days", datetime(2026, 11, 2, 13, 0, tzinfo=UTC)),   # same wall clock across the Nov 1 change
    ("2026-11-02T15:00:00Z", datetime(2026, 11, 2, 15, tzinfo=UTC)),
    ("2026-11-02 15:00+02:00", datetime(2026, 11, 2, 13, tzinfo=UTC)),
    ("2026-11-02 15:00", datetime(2026, 11, 2, 23, tzinfo=UTC)),          # naive = the person's zone (PST)
    ("tomorrow 09:30", datetime(2026, 10, 31, 16, 30, tzinfo=UTC)),
    ("tomorrow morning", datetime(2026, 10, 31, 16, 0, tzinfo=UTC)),
    ("today 17:00", datetime(2026, 10, 31, 0, 0, tzinfo=UTC)),
])
def test_parse_when_in_the_persons_timezone(text, expected):
    assert parse_when(text, NOW, LA) == expected


def test_parse_when_dst_and_utc_default():
    # 2026-11-01 02:00 US clocks fall back; tomorrow 09:00 after that is PST (UTC-8)
    now = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)
    assert parse_when("tomorrow 09:00", now, LA) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)
    assert parse_when("tomorrow 09:00", NOW) == datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    assert rm.zone("Not/AZone") is UTC and rm.zone(None) is UTC


@pytest.mark.parametrize("text", ["", "soon", "next week", "whenever", "tomorrow 25:00", "tomorrow 9", "in a while",
                                  "2026-11-02", "in -5 minutes", "in 20 parsecs", None, 5, "x" * 100,
                                  "2026-13-45 10:00"])
def test_parse_when_unclear_times_ask_for_a_clearer_one(text):
    with pytest.raises(ReminderError, match=r"Which time do you mean\? e\.g\. 'in 20 minutes' or '2026-11-02 15:00'"):
        parse_when(text, NOW, LA)


def test_parse_when_bounds():
    assert parse_when("in 31 seconds".replace("seconds", "minutes"), NOW) > NOW
    with pytest.raises(ReminderError):
        parse_when("in 400 days", NOW)
    with pytest.raises(ReminderError):
        parse_when("2028-01-01 10:00", NOW)


def test_text_is_cleaned_and_capped_and_pending_is_limited():
    store, _ = fresh()
    r = store.add("t", "u", "a\x00b\x1b[31m‮\nc" + "x" * 600, NOW + timedelta(hours=1))
    assert len(r.text) == rm.MAX_TEXT and "\x00" not in r.text and "‮" not in r.text and "\n" not in r.text
    with pytest.raises(ReminderError):
        store.add("t", "u", "  \x00 ", NOW + timedelta(hours=1))
    for _ in range(rm.MAX_PENDING_PER_PERSON - 1):
        store.add("t", "u", "n", NOW + timedelta(hours=1))
    with pytest.raises(ReminderError, match="too many"):
        store.add("t", "u", "n", NOW + timedelta(hours=1))


def test_list_is_pending_first_then_recent_fired_capped():
    store, clock = fresh()
    for i in range(3):
        store.add("t", "u", f"old{i}", NOW + timedelta(minutes=i + 1))
    clock.t = NOW + timedelta(hours=1)
    ReminderScheduler(store, lambda *a: None, clock).tick()
    store.add("t", "u", "later", NOW + timedelta(days=1))
    store.add("t", "other", "theirs", NOW + timedelta(days=1))
    rows = store.list("t", "u")
    assert [r.text for r in rows] == ["later", "old2", "old1", "old0"]


# ------------------------------------------------------------------------------ scheduler thread + persistence
def test_scheduler_thread_starts_fires_and_stops_with_an_injected_clock():
    store, clock = fresh()
    store.add("t", "u", "go", NOW + timedelta(seconds=40))
    got, boom = [], threading.Event()

    def deliver(rem, late):
        got.append(rem.id)
        boom.set()
        raise RuntimeError("delivery failed: secret text")      # must not end the loop

    sched = ReminderScheduler(store, deliver, clock, interval=0.01)
    sched.start()
    sched.start()                                                # idempotent
    time.sleep(0.05)
    assert got == []
    clock.t = NOW + timedelta(seconds=41)
    assert boom.wait(2)
    time.sleep(0.05)
    assert len(got) == 1
    sched.stop()
    assert sched._thread is None


def test_delivery_failure_is_logged_without_the_text(caplog):
    store, clock = fresh()
    store.add("t", "u", "TOPSECRET-TEXT", NOW)
    clock.t = NOW + timedelta(seconds=1)

    def bad(rem, late):
        raise RuntimeError("TOPSECRET-TEXT")
    with caplog.at_level("WARNING"):
        ReminderScheduler(store, bad, clock).tick()
    assert "TOPSECRET" not in caplog.text and "could not be delivered" in caplog.text


class FakeDb:
    """A tiny stand-in for the table, speaking the three statements the persistence uses."""

    def __init__(self):
        self.rows, self.sql = {}, []

    def connect(self):
        db = self

        class Cur:
            rowcount = 0
            _res = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args=()):
                db.sql.append(sql)
                if sql.startswith("INSERT"):
                    db.rows.setdefault(args[0], list(args))
                elif sql.startswith("SELECT"):
                    self._res = [tuple(r) for r in db.rows.values()]
                elif "SET state = 'fired'" in sql:
                    r = db.rows.get(args[0])
                    ok = bool(r and r[5] == "pending")
                    if ok:
                        r[5] = "fired"
                    self.rowcount = int(ok)
                elif "SET state = 'cancelled'" in sql:
                    r = db.rows.get(args[0])
                    ok = bool(r and r[1] == args[1] and r[2] == args[2] and r[5] == "pending")
                    if ok:
                        r[5] = "cancelled"
                    self.rowcount = int(ok)

            def fetchall(self):
                return self._res

        class Conn:
            def cursor(self):
                return Cur()

            def commit(self):
                pass

            def close(self):
                pass
        return Conn()


def test_postgres_persistence_is_exactly_once_across_two_stores():
    db = FakeDb()
    a = ReminderStore(PostgresReminderPersistence("dsn", db.connect))
    r = a.add("t", "u", "once", NOW + timedelta(minutes=1))
    b = ReminderStore(PostgresReminderPersistence("dsn", db.connect))       # a second server process
    assert [x.id for x in b.due(NOW + timedelta(minutes=2))] == [r.id]
    assert a.mark_fired(r.id) is True and b.mark_fired(r.id) is False
    assert b.list("t", "u")[0].state == "fired"
    assert all("dsn" not in s for s in db.sql)


def test_a_broken_database_refuses_to_set_and_does_not_fire(caplog):
    def broken():
        raise OSError("postgres://user:pw@host/db")
    store = ReminderStore(PostgresReminderPersistence("postgres://user:pw@host/db", broken))
    with caplog.at_level("WARNING"), pytest.raises(ReminderError, match="unavailable"):
        store.add("t", "u", "x", NOW + timedelta(hours=1))
    assert "pw@host" not in caplog.text


# ------------------------------------------------------------------------------------------- tools + catalog
def _service(store, tz="America/Los_Angeles"):
    clock = Clock()
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True, "notifications": {"timezone": tz}}}),
                           now=clock)
    svc.reminders = store
    svc._a = svc.create_assistant(tenant_id="t", user_id="u", name="A")
    return svc


def _tool(svc, tool_id, **payload):
    return svc.execute_tool(tenant_id="t", user_id="u", assistant_id=svc._a.assistant_id, tool_id=tool_id, payload=payload)


def test_reminder_tools_are_registered_non_sensitive_and_dispatch_through_execute_tool():
    for tool in ("set_reminder", "list_reminders", "cancel_reminder"):
        assert tool in TOOL_CATALOG and not tool_sensitive(tool)
    svc = _service(ReminderStore(clock=Clock()))
    out = _tool(svc, "set_reminder", text="lunch", when="tomorrow 12:00")
    assert out["reminder"]["fire_at"] == "2026-10-31T12:00:00-07:00" and out["timezone"] == "America/Los_Angeles"
    rid = out["reminder"]["id"]
    assert [r["id"] for r in _tool(svc, "list_reminders")["reminders"]] == [rid]
    with pytest.raises(AssistantError, match="no pending"):
        _cancel_as_other(svc, rid)
    assert _tool(svc, "cancel_reminder", id=rid) == {"ok": True, "cancelled": rid}
    with pytest.raises(AssistantError, match="Which reminder"):
        _tool(svc, "cancel_reminder", id="../etc")
    with pytest.raises(AssistantError, match="What should I remind"):
        _tool(svc, "set_reminder", text=5, when="in 5 minutes")


def _cancel_as_other(svc, rid):
    other = svc.create_assistant(tenant_id="t", user_id="v", name="B")
    return svc.execute_tool(tenant_id="t", user_id="v", assistant_id=other.assistant_id, tool_id="cancel_reminder",
                            payload={"id": rid})


def test_without_a_store_the_tools_say_so():
    svc = _service(None)
    svc.reminders = None
    with pytest.raises(AssistantError, match="not enabled"):
        _tool(svc, "list_reminders")


# ----------------------------------------------------------------------------------------------- HTTP routes
@pytest.fixture
def server(acknowledged_sink):
    svr = _serve(acknowledged_sink, None)
    yield svr
    svr.shutdown()


def test_server_starts_and_stops_the_scheduler_only_with_a_store(server, acknowledged_sink):
    assert server.reminders is not None and server.reminder_scheduler._thread is not None
    server.shutdown()
    assert server.reminder_scheduler._thread is None
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    bare = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), api_keys={KEY: "operator"})
    assert bare.reminders is None and bare.reminder_scheduler is None


def test_list_and_delete_are_pinned_to_the_calling_person(server):
    # create through the store using the ids the HTTP layer will resolve for each key
    from hashlib import sha256
    ids = {k: "key:" + sha256(k.encode()).hexdigest()[:12] for k in (KEY, OTHER)}
    mine = server.reminders.add(server.assistant.tenant_for(ids[KEY]), ids[KEY], "mine", datetime.now(UTC) + timedelta(hours=1))
    theirs = server.reminders.add(server.assistant.tenant_for(ids[OTHER]), ids[OTHER], "theirs", datetime.now(UTC) + timedelta(hours=1))
    status, body = call(server, "/api/v1/assistant-reminders")
    assert status == 200 and [r["id"] for r in body["reminders"]] == [mine.id] and body["timezone"] == "UTC"
    assert call(server, f"/api/v1/assistant-reminders/{theirs.id}", method="DELETE")[0] == 404
    assert call(server, "/api/v1/assistant-reminders/not-an-id", method="DELETE")[0] == 400
    assert call(server, f"/api/v1/assistant-reminders/{mine.id}", method="DELETE") == (200, {"ok": True, "id": mine.id})
    assert call(server, "/api/v1/assistant-reminders")[1]["reminders"] == []
    assert server.reminders.list(server.assistant.tenant_for(ids[OTHER]), ids[OTHER])[0].state == "pending"
    assert call(server, f"/api/v1/assistant-reminders/{theirs.id}", method="DELETE", key=VIEWER)[0] in (401, 403)
    assert call(server, "/api/v1/assistant-reminders", key="")[0] in (401, 403)


def test_delete_refuses_a_cross_origin_request(server):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/assistant-reminders/rem_0123456789abcdef",
                                 method="DELETE", headers={"X-API-Key": KEY, "Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 403


# ------------------------------------------------------------------------------ review fixes: lazy load, DST, logs
def test_a_failed_startup_load_is_retried_and_cap_waits_for_it():
    db = FakeDb()
    seed = ReminderStore(PostgresReminderPersistence("dsn", db.connect))
    for _ in range(rm.MAX_PENDING_PER_PERSON):
        seed.add("t", "u", "saved", NOW + timedelta(hours=1))
    real, fail = db.connect, [True]

    def flaky():
        if fail[0] and not db.sql[-1:] == ["x"]:
            raise OSError("down")
        return real()
    store = ReminderStore(PostgresReminderPersistence("dsn", flaky))
    assert store.list("t", "u") == [] and store._loaded is False
    with pytest.raises(ReminderError, match="unavailable"):
        store.add("t", "u", "new", NOW + timedelta(hours=1))
    assert store.due(NOW + timedelta(hours=2)) == []
    fail[0] = False                                              # the database is back: the next tick loads it
    assert len(store.due(NOW + timedelta(hours=2))) == rm.MAX_PENDING_PER_PERSON and store._loaded
    with pytest.raises(ReminderError, match="too many"):
        store.add("t", "u", "new", NOW + timedelta(hours=1))


def test_dst_gap_and_ambiguous_local_times_ask_for_an_offset():
    before = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ReminderError, match="explicit offset"):
        parse_when("2026-03-08 02:30", before, LA)               # skipped hour
    with pytest.raises(ReminderError, match="explicit offset"):
        parse_when("2026-11-01 01:30", NOW, LA)                  # happens twice
    assert parse_when("2026-11-01T01:30:00-07:00", NOW, LA) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    assert parse_when("2026-03-08 03:30", before, LA) == datetime(2026, 3, 8, 10, 30, tzinfo=UTC)
    sat = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)                # 04:00 PST Saturday
    with pytest.raises(ReminderError, match="explicit offset"):
        parse_when("tomorrow 02:30", sat, LA)


def test_in_n_days_and_tomorrow_keep_the_wall_clock_across_a_clock_change():
    sat = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)                # 20:00 PDT on Oct 31
    assert parse_when("in 2 days", sat, LA) == datetime(2026, 11, 3, 4, 0, tzinfo=UTC)   # 20:00 PST, not 19:00
    assert parse_when("in 24 hours", sat, LA) == sat + timedelta(hours=24)
    assert parse_when("tomorrow morning", sat, LA) == datetime(2026, 11, 1, 17, 0, tzinfo=UTC)   # 09:00 PST


def test_invalid_timezone_warns_once_and_falls_back(caplog):
    rm._WARNED_ZONES.clear()
    with caplog.at_level("WARNING"):
        assert rm.zone("Mars/Base") is UTC and rm.zone("Mars/Base") is UTC
    assert caplog.text.count("not valid") == 1 and "Mars" not in caplog.text


def test_push_collapse_id_is_per_reminder_and_unreachable_reminders_are_logged(make_server_p, caplog):  # noqa: F811
    svr, api, token, bot, sender = rig(make_server_p)
    api.ok("notifications/registerPush", {"token": TOKEN}, token)
    user = svr.rakazo.accounts[0].principal_id
    tenant = svr.assistant.tenant_for(user)
    r1, r2 = (svr.reminders.add(tenant, user, t, NOW) for t in ("one", "two"))
    svr._deliver_reminder(r1, False)
    svr._deliver_reminder(r2, False)
    assert wait_for(lambda: len(sender.sent) == 2)
    assert sender.sent[0][0]["collapseId"] != sender.sent[1][0]["collapseId"]
    lost = svr.reminders.add("t", "nobody", "TOPSECRET", NOW)
    with caplog.at_level("WARNING"):
        svr._deliver_reminder(lost, True)
    assert "neither a phone nor the chat" in caplog.text and "TOPSECRET" not in caplog.text
