"""A failed or partial load of the saved rows must fail closed: blocked calendars never read as 'nothing blocked'."""
import json
import os
import urllib.parse

import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.app_credentials import AppsError, CredentialStore, SecretBox
from herdr_engineering.control_plane.assistant_apps import AppsService
from herdr_engineering.control_plane.google_oauth import SCOPES

from .test_google_workspace import Google


class Persist:
    def __init__(self, box):
        self.box, self.fail_blocked, self.fail_all, self.saved = box, False, False, []

    def load_clients(self):
        if self.fail_all:
            raise OSError("db down")
        return [("google", "cid", self.box.seal("sec"))]

    def load_connections(self):
        if self.fail_all:
            raise OSError("db down")
        return [("default", "u1", "google", "u1@x.org", json.dumps(list(SCOPES)), "connected", self.box.seal("r1"), "2026-10-30")]

    def load_blocked(self):
        if self.fail_blocked or self.fail_all:
            raise OSError("db down")
        return [("default", "u1", "google", json.dumps(["secret@x.org"]))]

    def save_blocked(self, *a):
        self.saved.append(a)

    def save_connection(self, *a):
        pass

    def save_client(self, *a):
        pass


@pytest.fixture
def env(tmp_path):
    f = tmp_path / "key"
    f.write_bytes(os.urandom(32))
    box = SecretBox(str(f))
    p = Persist(box)
    return box, p


def make(box, p):
    store = CredentialStore(box, p, retry_interval=0)
    apps = AppsService(store, redirect_base="https://cp.example/")
    google = Google()
    return store, apps, gw.GoogleWorkspace(store, apps=apps, opener=google), google


def calendar_requests(g):
    return [r for r in g.requests if "calendar" in r[1] or r[1] == gw.FREEBUSY_URL]


def test_blocked_load_failure_makes_no_calendar_request_then_recovers(env):
    box, p = env
    p.fail_blocked = True
    store, apps, w, g = make(box, p)
    assert store.get_connection("default", "u1", "google") is not None and not store.blocked_loaded
    calls = [
        lambda: w.list_calendars("default", "u1"),
        lambda: w.list_events("default", "u1", "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z", 5, None),
        lambda: w.find_free_time("default", "u1", 30, "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z"),
        lambda: w.create_event("default", "u1", "Lunch", "2026-11-02T12:00:00Z", "2026-11-02T13:00:00Z", "secret@x.org"),
        lambda: w.create_event("default", "u1", "Lunch", "2026-11-02T12:00:00Z", "2026-11-02T13:00:00Z", None),
    ]
    for call in calls:
        with pytest.raises(gw.GoogleError) as exc:
            call()
        assert exc.value.reason == "unavailable"
    assert calendar_requests(g) == []
    with pytest.raises(AppsError) as e:
        apps.set_blocked_calendars("default", "u1", [])
    assert e.value.reason == "unavailable" and p.saved == []
    p.fail_blocked = False       # the database comes back: the next use reloads it
    assert apps.blocked_calendars("default", "u1") == ["secret@x.org"]
    with pytest.raises(gw.GoogleError):
        w.create_event("default", "u1", "Lunch", "2026-11-02T12:00:00Z", "2026-11-02T13:00:00Z", "secret@x.org")
    apps.set_blocked_calendars("default", "u1", ["a@x.org"])
    assert p.saved


def test_retry_is_rate_limited(env):
    box, p = env
    p.fail_blocked = True
    store = CredentialStore(box, p)
    assert store.retry_interval > 0
    p.fail_blocked = False
    assert store.unavailable()       # inside the minimum retry interval: still not loaded, no second database call
    store._retry_after = 0
    assert not store.unavailable() and store.get_blocked("default", "u1", "google") == ["secret@x.org"]


def test_database_down_at_start_reports_unavailable_and_refuses_enable(env):
    box, p = env
    p.fail_all = True
    store, apps, w, g = make(box, p)
    cards = {c["id"]: c for c in apps.providers("default", "u1")}
    assert cards["gmail"]["status"] == "unavailable" and cards["calendar"]["status"] == "unavailable"
    with pytest.raises(AppsError) as e:
        apps.connect_start("default", "u1", "google")
    assert e.value.reason == "unavailable"
    with pytest.raises(AppsError):
        store.put_connection("default", "u1", "google", account="a", scopes=[], refresh_token="r")
    p.fail_all = False
    cards = {c["id"]: c for c in apps.providers("default", "u1")}
    assert cards["gmail"]["status"] == "connected"
    assert urllib.parse.urlparse(apps.connect_start("default", "u1", "google")).query
