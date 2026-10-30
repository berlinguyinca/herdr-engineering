"""Reminders for the personal assistant: set by time, fired once by a server-side scheduler.

A reminder holds only text, a fire time and a state; it never needs Google or any other connector. ``mark_fired`` is the
single atomic step that makes delivery exactly-once, even with two scheduler ticks or two server processes.
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MAX_TEXT = 500
MAX_PENDING_PER_PERSON = 50
MAX_LISTED = 50
MIN_LEAD = timedelta(seconds=30)
MAX_AHEAD = timedelta(days=365)
REMINDER_ID = re.compile(r"rem_[0-9a-f]{16}")
REMINDER_TOOLS = frozenset({"set_reminder", "list_reminders", "cancel_reminder"})

ASK = "Which time do you mean? e.g. 'in 20 minutes' or '2026-11-02 15:00'"
UNCLEAR = "I could not read that time. " + ASK
PAST = "That time is not in the future. " + ASK
TOO_FAR = "That is more than a year away. " + ASK


class ReminderError(Exception):
    """A refusal with a fixed, person-safe message (it asks for a clearer time or says what is wrong)."""


@dataclass(frozen=True)
class Reminder:
    id: str
    tenant: str
    user: str
    text: str
    fire_at: datetime            # UTC
    state: str                   # pending | fired | cancelled
    created_at: datetime

    def as_dict(self, tz: tzinfo = UTC) -> dict:
        return {"id": self.id, "text": self.text, "fire_at": self.fire_at.astimezone(tz).isoformat(),
                "fire_at_utc": self.fire_at.astimezone(UTC).isoformat(), "state": self.state,
                "created_at": self.created_at.astimezone(UTC).isoformat()}


_WARNED_ZONES: set[str] = set()


def zone(name: str | None) -> tzinfo:
    try:
        return ZoneInfo(name) if name else UTC
    except Exception:  # noqa: BLE001 - an unknown zone is UTC rather than a failure
        if name not in _WARNED_ZONES:
            _WARNED_ZONES.add(name)
            log.warning("reminders: the configured timezone is not valid; using UTC")
        return UTC


def clean_text(value) -> str:
    if not isinstance(value, str):
        raise ReminderError("What should I remind you about?")
    text = "".join(" " if ch in "\t\n\r" else ch for ch in value)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp")).strip()
    if not text:
        raise ReminderError("What should I remind you about?")
    return text[:MAX_TEXT]


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?", re.I)
_REL = re.compile(r"in\s+(\d{1,4})\s*(minutes?|mins?|hours?|hrs?|days?)", re.I)
_DAY = re.compile(r"(today|tomorrow)\s+(?:at\s+)?(\d{1,2}):(\d{2})", re.I)
_MORNING = re.compile(r"tomorrow\s+morning", re.I)


def parse_when(text, now: datetime, tz: tzinfo = UTC) -> datetime:
    """The UTC fire time for ``text``, or a ReminderError that asks for a clearer time. Never guesses."""
    if not isinstance(text, str) or not text.strip() or len(text) > 64:
        raise ReminderError(UNCLEAR)
    s = " ".join(text.split())
    now = now.astimezone(UTC)
    local_now = now.astimezone(tz)
    fire: datetime
    if (m := _REL.fullmatch(s)):
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("d"):          # calendar days keep the wall-clock time across a clock change
            wall = local_now.replace(tzinfo=None) + timedelta(days=n)
            fire = _at(wall.date(), wall.hour, wall.minute, tz, strict=False).replace(second=local_now.second)
        else:
            fire = now + (timedelta(minutes=n) if unit.startswith("m") else timedelta(hours=n))
    elif _MORNING.fullmatch(s):
        fire = _at(local_now.date() + timedelta(days=1), 9, 0, tz)
    elif (m := _DAY.fullmatch(s)):
        hour, minute = int(m.group(2)), int(m.group(3))
        if hour > 23 or minute > 59:
            raise ReminderError(UNCLEAR)
        day = local_now.date() + (timedelta(days=1) if m.group(1).lower() == "tomorrow" else timedelta(0))
        fire = _at(day, hour, minute, tz)
    elif _ISO.fullmatch(s):
        try:
            parsed = datetime.fromisoformat(s.replace("z", "Z"))
        except ValueError:
            raise ReminderError(UNCLEAR) from None
        if parsed.tzinfo is None:
            _check_local(parsed, tz)
            fire = parsed.replace(tzinfo=tz)
        else:
            fire = parsed
    else:
        raise ReminderError(UNCLEAR)
    fire = fire.astimezone(UTC)
    if fire < now + MIN_LEAD:
        raise ReminderError(PAST)
    if fire > now + MAX_AHEAD:
        raise ReminderError(TOO_FAR)
    return fire


DST_ASK = ("That local time is skipped or happens twice when the clocks change. Give an explicit offset, "
           "e.g. '2026-11-01T01:30-07:00', or another time.")


def _check_local(naive: datetime, tz: tzinfo) -> None:
    """Refuse a wall-clock time in a clock-change gap (does not exist) or fold (happens twice)."""
    naive = naive.replace(tzinfo=None)
    first, second = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    gap = first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive
    if gap or first.utcoffset() != second.utcoffset():
        raise ReminderError(DST_ASK)


def _at(day, hour: int, minute: int, tz: tzinfo, strict: bool = True) -> datetime:
    if strict:
        _check_local(datetime(day.year, day.month, day.day, hour, minute), tz)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


class PostgresReminderPersistence:
    """``assistant_reminders`` (migration 0044). Holds text, time and state only. Short timeouts; the DSN is never logged."""

    def __init__(self, dsn: str, connect: Callable | None = None):
        self._dsn, self._connect = dsn, connect

    def _run(self, sql: str, args: tuple = (), fetch: bool = False):
        if self._connect is not None:
            conn = self._connect()
        else:
            import psycopg2
            conn = psycopg2.connect(self._dsn, connect_timeout=3, options="-c statement_timeout=3000 -c lock_timeout=3000")
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                out = cur.fetchall() if fetch else cur.rowcount
            conn.commit()
            return out
        finally:
            conn.close()

    def load(self) -> list[Reminder]:
        rows = self._run("SELECT id, tenant_id, user_id, text, fire_at, state, created_at FROM assistant_reminders "
                         "WHERE state = 'pending' OR created_at > now() - interval '7 days'", fetch=True)
        return [Reminder(r[0], r[1], r[2], r[3], _utc(r[4]), r[5], _utc(r[6])) for r in rows]

    def save(self, r: Reminder) -> None:
        self._run("INSERT INTO assistant_reminders (id, tenant_id, user_id, text, fire_at, state, created_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                  (r.id, r.tenant, r.user, r.text, r.fire_at, r.state, r.created_at))

    def mark_fired(self, rid: str) -> bool:
        return self._run("UPDATE assistant_reminders SET state = 'fired' WHERE id = %s AND state = 'pending'", (rid,)) == 1

    def cancel(self, rid: str, tenant: str, user: str) -> bool:
        return self._run("UPDATE assistant_reminders SET state = 'cancelled' WHERE id = %s AND tenant_id = %s "
                         "AND user_id = %s AND state = 'pending'", (rid, tenant, user)) == 1


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ReminderStore:
    """Memory is the working copy; Postgres (when given) is the authority for the fired/cancelled transitions.

    Deployment model: ONE control-plane instance. ``due``, ``list`` and ``cancel`` read the memory loaded from the database
    (so a second instance would not see the first one's new reminders); ``mark_fired`` is atomic in the database, so a
    brief overlap of two instances still fires each reminder once."""

    def __init__(self, persistence=None, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._p, self._clock, self._lock = persistence, clock, threading.Lock()
        self._rows: dict[str, Reminder] = {}
        self._loaded = persistence is None
        self._load()

    def _load(self) -> bool:
        """Read the saved reminders once. A failure is retried on the next tick, add or list, so a database that is down at
        startup does not lose the pending reminders in it."""
        if self._loaded:
            return True
        try:
            for r in self._p.load():
                self._rows.setdefault(r.id, r)
            self._loaded = True
        except Exception:  # noqa: BLE001
            log.warning("reminders: saved reminders could not be loaded")
        return self._loaded

    def add(self, tenant: str, user: str, text: str, fire_at: datetime) -> Reminder:
        text = clean_text(text)
        with self._lock:
            if not self._load():
                raise ReminderError("Reminders are unavailable right now; nothing was set.")
            if sum(1 for r in self._rows.values() if r.tenant == tenant and r.user == user and r.state == "pending") >= MAX_PENDING_PER_PERSON:
                raise ReminderError("You have too many pending reminders; cancel one first.")
            r = Reminder("rem_" + secrets.token_hex(8), tenant, user, text, fire_at.astimezone(UTC), "pending",
                         self._clock().astimezone(UTC))
            if self._p is not None:
                try:
                    self._p.save(r)
                except Exception:  # noqa: BLE001
                    log.warning("reminders: a reminder could not be saved")
                    raise ReminderError("Reminders are unavailable right now; nothing was set.") from None
            self._rows[r.id] = r
            return r

    def due(self, now: datetime) -> list[Reminder]:
        with self._lock:
            self._load()
            return sorted((r for r in self._rows.values() if r.state == "pending" and r.fire_at <= now),
                          key=lambda r: r.fire_at)

    def mark_fired(self, rid: str) -> bool:
        """True for exactly one caller per reminder."""
        with self._lock:
            r = self._rows.get(rid)
            if r is None or r.state != "pending":
                return False
            if self._p is not None:
                try:
                    if not self._p.mark_fired(rid):
                        self._rows[rid] = _with_state(r, "fired")      # another process fired it
                        return False
                except Exception:  # noqa: BLE001 - not marked, so the next tick tries again
                    log.warning("reminders: a reminder could not be marked fired")
                    return False
            self._rows[rid] = _with_state(r, "fired")
            return True

    def cancel(self, rid: str, tenant: str, user: str) -> bool:
        with self._lock:
            r = self._rows.get(rid)
            if r is None or r.tenant != tenant or r.user != user or r.state != "pending":
                return False
            if self._p is not None:
                try:
                    if not self._p.cancel(rid, tenant, user):
                        return False
                except Exception:  # noqa: BLE001
                    log.warning("reminders: a reminder could not be cancelled")
                    return False
            self._rows[rid] = _with_state(r, "cancelled")
            return True

    def list(self, tenant: str, user: str) -> list[Reminder]:
        with self._lock:
            self._load()
            mine = [r for r in self._rows.values() if r.tenant == tenant and r.user == user]
        pending = sorted((r for r in mine if r.state == "pending"), key=lambda r: r.fire_at)
        fired = sorted((r for r in mine if r.state == "fired"), key=lambda r: r.fire_at, reverse=True)
        return (pending + fired)[:MAX_LISTED]


def _with_state(r: Reminder, state: str) -> Reminder:
    return Reminder(r.id, r.tenant, r.user, r.text, r.fire_at, state, r.created_at)


class ReminderScheduler:
    """Every ``interval`` seconds fires what is due, once. A reminder missed while the server was down fires late and says so."""

    def __init__(self, store: ReminderStore, deliver: Callable[[Reminder, bool], None],
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC), interval: float = 15):
        self.store, self._deliver, self._clock, self.interval = store, deliver, clock, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        now = self._clock()
        fired = 0
        for r in self.store.due(now):
            if not self.store.mark_fired(r.id):
                continue
            late = (now - r.fire_at).total_seconds() > 2 * self.interval
            try:
                self._deliver(r, late)
            except Exception:  # noqa: BLE001 - never the text, and never the end of the loop
                log.warning("reminders: a reminder could not be delivered")
            fired += 1
        return fired

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="assistant-reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.warning("reminders: a scheduler pass failed")
            self._stop.wait(self.interval)


def run_tool(store: ReminderStore, tool_id: str, tenant: str, user: str, payload: dict, now: datetime,
             tz_name: str | None) -> dict:
    """The three assistant tools. Raises ReminderError with a message fit to show the person."""
    tz = zone(tz_name)
    if tool_id == "set_reminder":
        text = clean_text(payload.get("text"))
        fire = parse_when(payload.get("when"), now, tz)
        return {"ok": True, "reminder": store.add(tenant, user, text, fire).as_dict(tz), "timezone": str(tz)}
    if tool_id == "list_reminders":
        return {"ok": True, "reminders": [r.as_dict(tz) for r in store.list(tenant, user)], "timezone": str(tz)}
    rid = payload.get("id")
    if not isinstance(rid, str) or not REMINDER_ID.fullmatch(rid):
        raise ReminderError("Which reminder? Give its id from list_reminders.")
    if not store.cancel(rid, tenant, user):
        raise ReminderError("There is no pending reminder with that id.")
    return {"ok": True, "cancelled": rid}
