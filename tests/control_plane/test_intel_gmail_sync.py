import base64

import pytest

from herdr_engineering.control_plane import intel_gmail_sync as gs
from herdr_engineering.control_plane import project_intelligence as pi

CFG = {"enabled": True, "ingest": True, "derive": True}
VIEW = pi.Viewer("o", None, "restricted")


def b64(t):
    return base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")


def msg(mid, body="hello", subject="s"):
    return {"id": mid, "threadId": "t" + mid, "internalDate": "1790000000000", "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": "A <a@x.org>"}, {"name": "Subject", "value": subject}],
                        "body": {"data": b64(body)}}}


class Fake:
    def __init__(self):
        self.msgs = {"m1": msg("m1", "see https://github.com/o/r/pull/9"), "m2": msg("m2")}
        self.hist = {"added": [], "deleted": [], "history_id": "h2"}
        self.expired = False
        self.revoked = False
        self.calls = []

    def _chk(self):
        if self.revoked:
            raise gs.AuthRevoked()

    def profile(self):
        self._chk()
        return {"history_id": "h1"}

    def list_recent(self, days, limit):
        self._chk()
        return ["m1", "m2"]

    def history(self, since):
        self._chk()
        self.calls.append(since)
        if self.expired:
            raise gs.HistoryExpired()
        return self.hist

    def get_message(self, mid):
        self._chk()
        return self.msgs.get(mid)


def sync(client=None, cfg=None):
    store, cur = pi.IntelligenceStore(), {"v": None}
    s = gs.GmailSync(client or Fake(), store, lambda: CFG if cfg is None else cfg, org_id="o", account="acct", project_ids=("p1",),
                     cursor_get=lambda: cur["v"], cursor_set=lambda v: cur.__setitem__("v", v))
    return s, store, cur


def count(store):
    return store.timeline(VIEW, project_id="p1")["total"]


def test_dry_run_backfill_writes_nothing_and_does_not_move_the_cursor():
    s, store, cur = sync()
    assert s.backfill() == {"status": "dry_run", "would_fetch": 2}
    assert count(store) == 0 and cur["v"] is None


def test_first_poll_backfills_then_cursor_advances_and_repeat_is_idempotent():
    s, store, cur = sync()
    assert s.poll()["status"] == "ok" and cur["v"] == "h1"
    n = count(store)
    assert n == 2 and s.stats["links"] == 1
    s.backfill(dry_run=False)
    assert count(store) == n          # replay creates no duplicates


def test_incremental_poll_adds_new_and_tombstones_deleted():
    c = Fake()
    s, store, cur = sync(c)
    s.poll()
    c.msgs["m3"] = msg("m3", subject="new")
    c.hist = {"added": ["m3"], "deleted": ["m2"], "history_id": "h3"}
    r = s.poll()
    assert r == {"status": "ok", "added": 1, "deleted": 1} and cur["v"] == "h3" and c.calls == ["h1"]
    titles = {i["title"]: i["tombstoned"] for i in store.timeline(VIEW, project_id="p1")["items"]}
    assert titles["new"] is False and titles["[removed at source]"] is True


def test_expired_cursor_reanchors_with_bounded_backfill():
    c = Fake()
    s, store, cur = sync(c)
    s.poll()
    c.expired = True
    cur["v"] = "stale"
    assert s.poll()["status"] == "ok" and cur["v"] == "h1"


def test_revoked_consent_pauses_clears_cursor_and_tombstones():
    c = Fake()
    s, store, cur = sync(c)
    s.poll()
    c.revoked = True
    out = s.poll()
    assert out["status"] == "revoked" and out["tombstoned"] >= 2 and cur["v"] is None
    assert all(i["tombstoned"] for i in store.timeline(VIEW, project_id="p1")["items"])
    c.revoked = False
    assert s.poll()["status"] in ("paused", "ok")      # a paused source refuses to ingest again
    assert count(store) == 2


@pytest.mark.parametrize("cfg", [{}, {"enabled": True}, {"enabled": True, "ingest": True, "kill_switch": True}])
def test_off_flags_and_kill_switch_do_nothing(cfg):
    s, store, cur = sync(cfg=cfg)
    assert s.poll() == {"status": "off"} and s.backfill(dry_run=False) == {"status": "off"}
    assert count(store) == 0


def test_paused_source_does_not_advance_the_cursor():
    s, store, cur = sync()
    store.pause_source("o", "gmail", True)
    assert s.poll()["status"] == "paused" and cur["v"] is None
