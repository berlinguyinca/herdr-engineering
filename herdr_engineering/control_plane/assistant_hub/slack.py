"""Slack front door: a small Web API client and the Socket Mode runtime that verifies owner taps.

Only a verified button tap decides anything.  A tap is acted on when the Slack user is the configured owner, the ask id
and one-time nonce in the button match an open, unexpired ask, the message is the very one the hub posted (channel and
timestamp), and the request is still pending and not vetoed.  Any other tap changes nothing and leaves an
``assistant.slack.refused`` audit entry holding ids and a short reason, never command text or a token.

Tokens are read from their files on every call (so a rotation applies at once) and are never logged or put in an
exception; the Socket Mode connection runs on one supervisor thread that reconnects with bounded backoff.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..assistant import AssistantStore
from .actiongrants import ActionRequestRow, GrantState, as_utc
from .deploy import DeployMatch, scope_key, scope_mode
from .deploycheck import (
    FINAL,
    NO_CHECK,
    NOT_OPENED,
    DeployCheck,
    describe_status,
    short_label,
    verify_deploy,
)
from .evidence import propose_if_ready
from .policy import (
    ActivationRefused,
    activate_grant,
    create_standing_grant,
    current_judge_version,
    in_shadow,
    record_owner_decision,
)
from .preconditions import HealthClient, PreconditionClients
from .slack_asks import SlackAsk, button_value, new_ask_id, new_nonce
from .verbs import ActionKey, Verb

__all__ = ["SlackClient", "SlackError", "SlackRuntime", "ASK_ACTIONS", "DEPLOY_ACTIONS", "slack_text", "slack_api_call", "PERMANENT_ERRORS",
           "refusal_sentence"]

_log = logging.getLogger("herdr_engineering.assistant_hub.slack")
_MAX_REPLY = 1024 * 1024
# Errors that retrying cannot fix: the ask fails at once, the queue moves on, and the sweep offers it again later.
PERMANENT_ERRORS = frozenset({"channel_not_found", "invalid_auth", "not_in_channel", "account_inactive",
                              "token_revoked", "is_archived", "token_expired", "missing_scope", "disabled", "token"})
ASK_ACTIONS = {"ask_approve": (True, False), "ask_deny": (False, False), "ask_remember": (True, True)}
DEPLOY_ACTIONS = {"deploy_confirm": True, "deploy_cancel": False}       # kind "deploy" asks only
_ID = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
SLACK_ACTOR = "hub:slack"


class SlackError(Exception):
    """A Slack call failed.  ``code`` is a short word (Slack's own error code or ``io``/``token``); never a body or token."""

    def __init__(self, code: str, retry_after: float = 0.0) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after       # seconds Slack asked us to wait (Retry-After), 0 when absent


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:     # a bearer token must not follow a redirect
        return None


def _read_token(path: str) -> str:
    try:
        with open(path, encoding="ascii") as handle:
            token = handle.read(4097).strip()
    except (OSError, ValueError):
        raise SlackError("token") from None
    if not token or len(token) > 4096 or any(c.isspace() for c in token):
        raise SlackError("token")
    return token


def _retry_after(value: object) -> float:
    try:
        return min(max(float(str(value)), 0.0), 3600.0) if value is not None else 0.0
    except ValueError:
        return 0.0


def slack_api_call(base_url: str, method: str, token: str, body: dict[str, Any] | None, timeout: float,
                   ) -> dict[str, Any]:
    """One Web API call that never uses an environment proxy and never follows a redirect (a token rides on it).

    ``body`` None sends an empty form (``apps.connections.open``); otherwise the body is JSON."""
    headers = {"Authorization": f"Bearer {token}"}
    if body is None:
        data, headers["Content-Type"] = b"", "application/x-www-form-urlencoded"
    else:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json; charset=utf-8"
    req = urllib.request.Request(base_url.rstrip("/") + "/" + method, method="POST", data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)    # never via a proxy
    retry_after = 0.0
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(_MAX_REPLY)
            retry_after = _retry_after(response.headers.get("Retry-After"))
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_REPLY)
        retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
    except (OSError, ValueError):
        raise SlackError("io") from None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise SlackError("bad_reply") from None
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        error = doc.get("error") if isinstance(doc, dict) else None
        raise SlackError(error if isinstance(error, str) and _ID.fullmatch(error) else "bad_reply", retry_after)
    return doc


class SlackClient:
    """Slack Web API calls with the bot token.  Built per call from the live config, so rotation applies."""

    def __init__(self, cfg_getter: Callable[[], dict[str, Any]], timeout: float = 10.0) -> None:
        self._cfg = cfg_getter
        self._timeout = timeout

    def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg.get("enabled"):
            raise SlackError("disabled")
        return slack_api_call(cfg["base_url"], method, _read_token(cfg["bot_token_file"]), body, self._timeout)

    def post_message(self, channel: str, blocks: list[dict[str, Any]], text: str, *, thread_ts: str = "") -> str:
        body: dict[str, Any] = {"channel": channel, "blocks": blocks, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        ts = self._call("chat.postMessage", body).get("ts")
        if not isinstance(ts, str) or not ts:
            raise SlackError("bad_reply")
        return ts

    def update_message(self, channel: str, ts: str, blocks: list[dict[str, Any]], text: str) -> None:
        self._call("chat.update", {"channel": channel, "ts": ts, "blocks": blocks, "text": text})

    def open_dm(self, user_id: str) -> str:
        channel = self._call("conversations.open", {"users": user_id}).get("channel")
        cid = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(cid, str) or not cid:
            raise SlackError("bad_reply")
        return cid


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
_TEXT_MAX = 300


def slack_text(value: str, limit: int = _TEXT_MAX) -> str:
    """Worker-controlled text made inert for Slack mrkdwn: control characters are dropped, ``& < >`` are escaped
    (so ``<!channel>``, ``<@U123>`` and ``<url|label>`` print literally), ``@`` gets a zero-width space so a bare
    ``@here`` cannot notify, backticks become the look-alike modifier grave accent, and the result is at most 300
    characters (``limit``; never cut inside an entity)."""
    out = _CONTROL.sub(" ", str(value))
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace("@", "@\u200b").replace("`", "\u02cb")
    if len(out) > limit:
        out = out[:limit - 1]
        amp = out.rfind("&")
        if amp > out.rfind(";"):
            out = out[:amp]
        out += "\u2026"
    return out


_QUESTION_TEXT = 1500
_ANSWER_BYTES = 4096
_ANSWER_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]+")
_SENTENCES = {
    "still_in_shadow": "the learning period (shadow mode) has not ended yet",
    "insufficient_labels": "not enough of your decisions have been labeled yet for this kind of action",
    "judge_disabled": "the safety judge is not switched on",
    "backend_unconfirmed": "you have not yet confirmed that a real model is serving the safety judge",
    "insufficient_diversity": "your labeled decisions come from too few different actions",
    "no_discrimination": "there are too few denied or regretted decisions to show the judge can tell good from bad",
    "fn_bound_exceeded": "the judge still misses too many risky actions to be trusted with this",
    "stale_report": "the judge's calibration report is out of date for the current judge",
    "judge_error": "the safety judge could not be checked just now",
    "not_proposed": "there is no proposed permission for this action to turn on",
    "confirmation_missing": "your confirmation was missing",
    "not_enough_approvals": "this action has not been approved enough times yet, across enough sessions and tasks",
    "not_rememberable": "this kind of action can never be remembered, so it will always ask you",
    "grant_cap": "this account already holds the maximum number of permissions",
    "not_configured": "that deploy scope is no longer configured the way you were asked about, so nothing was turned on",
    "mode_not_auto": "auto-deploy is no longer switched on (mode: auto) for that scope, so nothing was turned on",
    "no_worker": "that worker is no longer active, so nothing was turned on",
}


def refusal_sentence(exc: ActivationRefused) -> str:
    """A refusal code in plain words (what is missing), never the code or the raw message."""
    return _SENTENCES.get(exc.code, "one of the safety checks has not passed yet")


def _short_id(value: object) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else "-"


@dataclass
class _Queued:
    """One pending request waiting to be posted.  ``ask`` is created on the first attempt and reused by every retry."""
    tenant_id: str
    user_id: str
    request_id: str
    worker_id: str
    task_id: str
    display: str
    why: str
    project: str
    queued_at: datetime
    ask: SlackAsk | None = None
    urgent: bool = False             # goes to the head of the queue and is never the one dropped on overflow
    kind: str = "action"             # action | question | summary (their asks are created up front) | outcome (a deploy
                                     # check id in ``request_id``)


@dataclass
class _Batch:
    channel: str
    ts: str
    count: int
    since: datetime


DEFAULT_WORKER_ASKS = 20            # asks per worker per hour across all its task ids
DEFAULT_GLOBAL_ASKS = 60            # asks per hour across every worker
_HOUR = timedelta(hours=1)
_MAX_OPEN_SUMMARIES = 50           # unfinished task trackers one worker may hold (task ids are worker-controlled)
_SUMMARY_GIVE_UP = timedelta(hours=24)       # a summary that could not be posted for this long is dropped
_SUMMARY_REPORT_KEEP = 1000        # report text kept on the row for the summary
_SUMMARY_REPORT_SHOWN = 600


class SlackRuntime:
    """Owns the Socket Mode connection and the tap handler.  ``start``/``stop`` are idempotent."""

    def __init__(self, store: AssistantStore, service: Any, cfg_getter: Callable[[], dict[str, Any]],
                 now_fn: Callable[[], datetime], *, min_backoff: float = 0.5, max_backoff: float = 30.0,
                 ping_interval: float = 2.0, stop_timeout: float = 15.0,
                 queue_size: int = 200, sweep_interval: float = 300.0, reoffer_backoff: float = 300.0,
                 max_task_keys: int = 1000, judge: Any = None,
                 summary_interval: float = 5.0, health: Any = None, preconditions: PreconditionClients | None = None,
                 check_interval: float = 15.0) -> None:
        self.store = store
        self.service = service
        self.judge = judge                 # the policy's judge runtime: activating a remembered grant uses its gate
        self._cfg = cfg_getter
        self._now = now_fn
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._stop_timeout = stop_timeout
        self._ping_interval = ping_interval        # a dead link is noticed after about five missed pings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = threading.Lock()
        self._qsize = max(1, int(queue_size))
        self._sweep_interval = sweep_interval
        self._reoffer_backoff = reoffer_backoff      # a request that failed is not offered again sooner (monotonic s)
        self._max_keys = max(1, int(max_task_keys))
        self._summary_interval = summary_interval    # how often the idle timer looks at unfinished task trackers
        self._next_summary = 0.0
        self._summ = threading.Lock()                # one tracker row per (worker, task): creation is serialized
        self._deploy_lock = threading.Lock()         # one deploy confirmation posted at a time (it retires the older)
        self._failed_at: dict[str, float] = {}
        self._batched: dict[str, None] = {}          # request ids folded into a batched line: the sweep skips them
        self._current: str = ""                      # request id being posted right now
        self._sweep_due = True
        self._next_sweep = 0.0
        self._queue: deque[_Queued] = deque()
        self._qcond = threading.Condition()
        self._drain_thread: threading.Thread | None = None
        self.dropped = 0
        self._posted: dict[tuple[str, str, str, str, str], deque[datetime]] = {}    # per (worker, task) post times
        self._batches: dict[tuple[str, str, str, str, str], _Batch] = {}
        self._dm: dict[str, str] = {}
        self._connected = False
        self._last_event: datetime | None = None
        self._tap = threading.Lock()           # one tap at a time: a double tap decides once
        self.client = SlackClient(cfg_getter)
        self.health = health if health is not None else HealthClient()     # the deploy outcome check's reader
        self.preconditions = preconditions                                 # GitHub access for the after-deploy main look
        self._check_interval = check_interval
        self._check_thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self) -> None:
        with self._state:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="slack-socket-mode", daemon=True)
                self._thread.start()
            elif self._stop.is_set():
                return          # a stop() that timed out has not finished: never two supervisors
            if self._drain_thread is None or not self._drain_thread.is_alive():
                self._sweep_due = True           # first thing: offer every pending request that has no ask yet
                self._drain_thread = threading.Thread(target=self._drain, name="slack-asks", daemon=True)
                self._drain_thread.start()
            self._start_checks_locked()

    def start_checks(self) -> None:
        """Only the deploy outcome checker (Slack off): open checks still resolve and are stored."""
        with self._state:
            busy = any(t is not None and t.is_alive() for t in (self._thread, self._drain_thread))
            if self._stop.is_set() and busy:
                return                       # a stop() that timed out has not finished
            self._stop.clear()
            self._start_checks_locked()

    def _start_checks_locked(self) -> None:
        if self._check_thread is None or not self._check_thread.is_alive():
            self._check_thread = threading.Thread(target=self._check_loop, name="deploy-checks", daemon=True)
            self._check_thread.start()

    def stop(self) -> bool:
        """Stop the runtime.  True when both threads have ended; False when one did not end within ``stop_timeout``
        (it is still winding down, ``start`` refuses until it has, and a later ``stop`` can confirm).  Queued asks
        are kept in memory, so a restart posts them."""
        with self._state:
            threads = [self._thread, self._drain_thread, self._check_thread]
            self._stop.set()
        with self._qcond:
            self._qcond.notify_all()
        alive = False
        for thread in threads:
            if thread is not None:
                thread.join(timeout=self._stop_timeout)
                alive = alive or thread.is_alive()
        if not alive:
            with self._state:
                self._thread = self._drain_thread = self._check_thread = None
        return not alive

    def _run(self) -> None:
        delay = self._min_backoff
        while not self._stop.is_set():
            connected = False
            try:
                connected = self._session()
            except Exception as exc:        # never into the caller; the type only (a message could echo a ticket URL)
                _log.warning("slack socket session ended: %s", type(exc).__name__)
            if self._stop.is_set():
                break
            delay = self._min_backoff if connected else min(delay * 2, self._max_backoff)
            self._stop.wait(delay)

    def _session(self) -> bool:
        from slack_sdk.socket_mode.builtin import SocketModeClient
        from slack_sdk.web import WebClient

        cfg = self._cfg()
        if not cfg.get("enabled"):
            return False
        app_token = _read_token(cfg["app_token_file"])
        sdk_log = logging.getLogger("herdr_engineering.assistant_hub.slack.sdk")
        sdk_log.setLevel(logging.ERROR)                     # the SDK's own logs may carry connection tickets
        base_url = cfg["base_url"]

        class NoProxySocketModeClient(SocketModeClient):
            def issue_new_wss_url(self) -> str:             # the SDK's WebClient would read HTTP(S)_PROXY from the env
                url = slack_api_call(base_url, "apps.connections.open", app_token, None, 10).get("url")
                if not isinstance(url, str) or not url:
                    raise SlackError("bad_reply")
                return url

        client = NoProxySocketModeClient(
            app_token=app_token, logger=sdk_log, auto_reconnect_enabled=False, ping_interval=self._ping_interval,
            web_client=WebClient(base_url=base_url.rstrip("/") + "/", timeout=10))
        client.proxy = None                                 # the websocket honours only this attribute (set from the env)
        client.socket_mode_request_listeners.append(self._on_request)
        connected = False
        try:
            client.connect()
            connected = client.is_connected()
            self._connected = connected
            self._last_event = as_utc(self._now())
            with self._qcond:
                self._sweep_due = True               # Slack is reachable again: offer whatever is still unasked
                self._qcond.notify()
            while connected and not self._stop.is_set() and client.is_connected():
                self._stop.wait(0.1)
        finally:
            self._connected = False
            client.close()
        return connected

    def _on_request(self, client: Any, req: Any) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse
        self._last_event = as_utc(self._now())
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # ack first
        except Exception as exc:
            _log.warning("slack ack failed: %s", type(exc).__name__)
        if req.type not in ("interactive", "events_api") or not isinstance(req.payload, dict):
            return
        try:
            if req.type == "interactive":
                self.handle_interaction(req.payload)
            else:
                self.handle_event(req.payload)
        except Exception as exc:            # a bad payload must never take the connection down
            _log.warning("slack %s failed: %s", req.type, type(exc).__name__)

    # -- owner asks --------------------------------------------------------------------------------
    def notify_pending(self, row: ActionRequestRow, display: str, why: str, *, project: str = "") -> None:
        """Queue a NEW pending request for the owner.  Never blocks and never raises: the caller is a request thread.

        The queue is bounded (oldest dropped); the runtime's own thread posts, retrying while Slack is unreachable."""
        try:
            if not self._cfg().get("enabled"):
                return
            self._enqueue(_Queued(row.tenant_id, row.user_id, row.request_id, row.worker_id, row.task_id, display,
                                  why, project, as_utc(self._now())))
        except Exception as exc:
            _log.warning("slack notify failed: %s", type(exc).__name__)

    def _enqueue(self, item: _Queued) -> None:
        with self._qcond:
            if len(self._queue) >= self._qsize:
                for i, old in enumerate(self._queue):      # the oldest item that is not urgent goes; urgent never
                    if not old.urgent:
                        del self._queue[i]
                        self.dropped += 1
                        break
            if item.urgent:
                self._queue.appendleft(item)
            else:
                self._queue.append(item)
            self._qcond.notify()

    def is_enabled(self) -> bool:
        return bool(self._cfg().get("enabled"))

    # -- worker questions --------------------------------------------------------------------------
    def ask_question(self, tenant_id: str, user_id: str, worker_id: str, session_id: str, question: str,
                     options: list[str], context: str) -> str:
        """Store an open question ask for the worker and queue it for posting; returns its id.  Never blocks."""
        now = as_utc(self._now())
        ttl = timedelta(minutes=int(self._cfg().get("ask_ttl_minutes") or 15))
        ask = SlackAsk(ask_id=new_ask_id(), tenant_id=tenant_id, user_id=user_id, kind="question", task_id=session_id,
                       nonce=new_nonce(), created_at=now, expires_at=now + ttl, worker_id=worker_id,
                       question=question[:2000], options=[o[:500] for o in options[:20]], context=context[:2000])
        self.store.put_slack_ask(ask)
        self._enqueue(self._question_item(ask, now))
        return ask.ask_id

    @staticmethod
    def _question_item(ask: SlackAsk, now: datetime) -> _Queued:
        return _Queued(ask.tenant_id, ask.user_id, "", ask.worker_id, ask.task_id, ask.question, ask.context, "", now,
                       ask=ask, kind="question")

    def question_status(self, tenant_id: str, user_id: str, worker_id: str, ask_id: str) -> dict[str, Any] | None:
        """What the asking worker may see of its own question, or None when it is not that worker's question."""
        ask = self.store.get_slack_ask(ask_id, tenant_id=tenant_id, user_id=user_id)
        if ask is None or ask.kind != "question" or ask.worker_id != worker_id:
            return None
        if ask.status == "decided" and ask.answer is not None:
            return {"status": "answered", "answer": ask.answer}
        if ask.status == "expired" or (ask.status == "open" and as_utc(self._now()) >= as_utc(ask.expires_at)):
            return {"status": "expired"} if ask.ts else {"status": "expired", "reason": "not_asked"}
        return {"status": "pending"}

    def queued(self) -> int:
        with self._qcond:
            return len(self._queue)

    def status(self, tenant_id: str, user_id: str) -> dict[str, Any]:
        """What the owner API shows: no token, path or channel text."""
        last = self._last_event
        return {"enabled": bool(self._cfg().get("enabled")), "connected": self._connected,
                "last_event_at": last.isoformat() if last is not None else None,
                "open_asks": sum(1 for a in self.store.list_slack_asks(tenant_id, user_id, "open") if a.ts)}

    def _drain(self) -> None:
        delay = self._min_backoff
        while not self._stop.is_set():
            self._maybe_sweep()
            self._maybe_summaries()
            self._prune()
            with self._qcond:
                if not self._queue and not self._stop.is_set() and not self._sweep_due:
                    self._qcond.wait(1.0)
                if self._stop.is_set():
                    return
                item = self._queue[0] if self._queue else None
            if item is None:
                continue
            self._current = item.request_id or (item.ask.ask_id if item.ask is not None else "")
            wait = 0.0
            try:
                done = self._post_ask(item)
            except SlackError as exc:
                if exc.code in PERMANENT_ERRORS or self._expired(item):
                    self._failed(item, exc.code)         # the queue moves on; the sweep offers it again later
                    done = True
                else:                                    # transient: retry, honouring Slack's Retry-After
                    done = False
                    wait = max(delay, exc.retry_after)
                    delay = min(delay * 2, self._max_backoff)
            except Exception as exc:                  # never kill the loop; the ask stays with the owner's REST view
                _log.warning("slack ask failed: %s", type(exc).__name__)
                done = True
            finally:
                self._current = ""
            if done:
                delay = self._min_backoff
                with self._qcond:
                    for i, queued in enumerate(self._queue):     # by identity: an urgent item may have moved the head
                        if queued is item:
                            del self._queue[i]
                            break
            elif wait:
                self._stop.wait(wait)

    def _failed(self, item: _Queued, code: str) -> None:
        self._audit_failed(item, code)
        if item.kind == "action":
            self._failed_at[item.request_id] = time.monotonic()
            self._cap(self._failed_at)
        elif item.kind == "summary" and item.ask is not None:      # stays open: the timer offers it again later
            self._failed_at[item.ask.ask_id] = time.monotonic()
            self._cap(self._failed_at)
        elif item.kind == "outcome":                               # stays unnotified: the sweep offers it again
            self._failed_at[item.request_id] = time.monotonic()
            self._cap(self._failed_at)

    def _cap(self, mapping: dict[Any, Any]) -> None:
        while len(mapping) > self._max_keys:
            del mapping[next(iter(mapping))]            # oldest first

    def _prune(self) -> None:
        """Task keys are worker-controlled: forget the ones idle for an hour and cap what is left."""
        now = as_utc(self._now())
        for key in [k for k, stamps in self._posted.items() if not stamps or now - stamps[-1] >= _HOUR]:
            del self._posted[key]
        for key in [k for k, batch in self._batches.items() if now - batch.since >= _HOUR]:
            del self._batches[key]
        self._cap(self._posted)
        self._cap(self._batches)

    def _maybe_sweep(self) -> None:
        with self._qcond:
            due = self._sweep_due or time.monotonic() >= self._next_sweep
            self._sweep_due = False
        if due:
            self._next_sweep = time.monotonic() + self._sweep_interval
            try:
                self.sweep()
            except Exception as exc:
                _log.warning("slack sweep failed: %s", type(exc).__name__)

    def sweep(self) -> int:
        """Queue every pending request that still deserves an ask and has none; returns how many were queued.

        This is what makes a restart or a long Slack outage lossless: the pending rows are the source of truth.
        Skipped: requests older than ``approval_ttl_minutes`` (a stale approval would be useless), ones with a posted
        ask, ones already queued or being posted, ones folded into a batched line, and ones that failed recently."""
        cfg = self._cfg()
        if not cfg.get("enabled"):
            return 0
        now = as_utc(self._now())
        ttl = timedelta(minutes=int((self.service.config.get("grants") or {}).get("approval_ttl_minutes", 15)))
        mono = time.monotonic()
        queued = 0
        for row in self.store.list_pending_action_requests_any():       # oldest first
            if now - as_utc(row.created_at) > ttl:
                continue
            rid = row.request_id
            if rid in self._batched or mono - self._failed_at.get(rid, -1e9) < self._reoffer_backoff:
                continue
            with self._qcond:
                if len(self._queue) >= self._qsize:
                    break                                          # full: the next sweep picks up the rest
                if rid == self._current or any(i.request_id == rid for i in self._queue):
                    continue
            asks = self.store.slack_asks_for_request(rid)
            if any(a.ts and a.status in ("open", "decided") for a in asks):
                continue
            for orphan in asks:                    # an unposted ask left by a crash or a drop: retire it, post afresh
                if not orphan.ts and orphan.status == "open":
                    orphan.status = "expired"
                    self.store.put_slack_ask(orphan)
            with self._qcond:
                self._queue.append(_Queued(row.tenant_id, row.user_id, rid, row.worker_id, row.task_id, row.display,
                                           row.reason, "", now))
                self._qcond.notify()
            queued += 1
        return queued + self._sweep_questions(now) + self._sweep_outcomes(now)

    # -- task summaries ----------------------------------------------------------------------------
    def note_task(self, tenant_id: str, user_id: str, worker_id: str, task_id: str, *, final: bool = False,
                  text: str | None = None) -> None:
        """A worker's task did something (a report or an action request).  Keeps the task's one summary row up to date;
        a final report queues the summary.  A task with a summary already (posted or given up) is left alone, so it
        never gets a second.  Never blocks and never raises: the caller is a request thread."""
        try:
            if not self._cfg().get("enabled"):
                return
            now = as_utc(self._now())
            with self._summ:
                ask = self.store.find_summary_ask(tenant_id, user_id, worker_id, task_id)
                if ask is None:
                    if not final and len(self.store.list_open_summaries(tenant_id, user_id, worker_id)) \
                            >= _MAX_OPEN_SUMMARIES:
                        return           # a flood of task ids must not push the owner's asks out of the store
                    ask = SlackAsk(ask_id=new_ask_id(), tenant_id=tenant_id, user_id=user_id, kind="summary",
                                   task_id=task_id, nonce=new_nonce(), created_at=now, worker_id=worker_id)
                elif ask.status != "open" or ask.ts:
                    return
                was_final = ask.final
                ask.last_activity, ask.final = now, ask.final or final
                if text:
                    ask.context = text[:_SUMMARY_REPORT_KEEP]
                self.store.put_slack_ask(ask)
            if ask.final and not was_final:
                self._enqueue(self._summary_item(ask, now))
        except Exception as exc:
            _log.warning("slack note_task failed: %s", type(exc).__name__)

    @staticmethod
    def _summary_item(ask: SlackAsk, now: datetime) -> _Queued:
        return _Queued(ask.tenant_id, ask.user_id, "", ask.worker_id, ask.task_id, "", "", "", now, ask=ask,
                       kind="summary")

    def _maybe_summaries(self) -> None:
        mono = time.monotonic()
        if mono < self._next_summary:
            return
        self._next_summary = mono + self._summary_interval
        try:
            self.sweep_summaries()
        except Exception as exc:
            _log.warning("slack summary sweep failed: %s", type(exc).__name__)

    def sweep_summaries(self) -> int:
        """Queue every task summary that is due and unposted: finished tasks (a restart or an outage lost the queue
        entry) and tasks quiet for ``summary_idle_minutes``.  Returns how many were queued."""
        cfg = self._cfg()
        if not cfg.get("enabled"):
            return 0
        now = as_utc(self._now())
        idle = timedelta(minutes=int(cfg.get("summary_idle_minutes") or 30))
        mono = time.monotonic()
        queued = 0
        for ask in self.store.list_open_summaries():
            quiet = now - as_utc(ask.last_activity)
            if not ask.final and quiet < idle:
                continue
            if quiet >= idle + _SUMMARY_GIVE_UP:
                ask.status = "expired"           # could not be posted for a day: give up (still never a second one)
                self.store.put_slack_ask(ask)
                continue
            if mono - self._failed_at.get(ask.ask_id, -1e9) < self._reoffer_backoff:
                continue
            with self._qcond:
                if len(self._queue) >= self._qsize:
                    break
                if ask.ask_id == self._current or any(i.ask is not None and i.ask.ask_id == ask.ask_id
                                                      for i in self._queue):
                    continue
                self._queue.append(self._summary_item(ask, now))
                self._qcond.notify()
            queued += 1
        return queued

    def _post_summary(self, item: _Queued, cfg: dict[str, Any], now: datetime) -> bool:
        current = self.store.get_slack_ask(item.ask.ask_id, tenant_id=item.tenant_id,
                                           user_id=item.user_id) if item.ask is not None else None
        if current is None or current.status != "open" or current.ts:
            return True                      # already posted (a duplicate entry), given up, or gone: never a second
        key = (item.tenant_id, item.user_id, item.worker_id, item.task_id, "summary")   # its own counter
        stamps = self._posted.pop(key, None) or deque()
        self._posted[key] = stamps
        while stamps and now - stamps[0] >= _HOUR:
            stamps.popleft()
        if len(stamps) >= int(cfg.get("max_asks_per_task_per_hour") or 6):
            self._failed_at[current.ask_id] = time.monotonic()      # over the limit: the timer offers it later
            return True
        counts = self.store.task_activity(item.tenant_id, item.user_id, item.worker_id, item.task_id)
        idle = int(cfg.get("summary_idle_minutes") or 30)
        text = f"Task summary from worker {slack_text(item.worker_id)}"
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": self._summary_text(current, counts, idle)}}]
        channel = self._target(cfg)
        ts = self.client.post_message(channel, blocks, text)
        current.ts, current.channel, current.status = ts, channel, "decided"
        self.store.put_slack_ask(current)
        stamps.append(now)
        self.store.audit_record(tenant_id=item.tenant_id, user_id=item.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.summary_posted", subject_id=current.ask_id,
                                detail=f"ask={current.ask_id} worker={_short_id(item.worker_id)}"
                                       f" final={'yes' if current.final else 'no'}")
        return True

    def _summary_text(self, ask: SlackAsk, counts: dict[str, int], idle_minutes: int) -> str:
        """Counts and the worker's inert report text only: never a command, a display string, a secret or a token."""
        lines = [f"*Task summary* from worker `{slack_text(ask.worker_id)}`, task `{slack_text(ask.task_id, 128)}`"]
        if not ask.final:
            lines.append(f"_No final report; the task has been quiet for {idle_minutes} minutes._")
        lines += [f"Authorized under grants: at least {counts['grant']}",
                  f"One-shot approvals used: at least {counts['one_shot']}",
                  f"Denied by you: at least {counts['denied']}",
                  f"Vetoed: at least {counts['vetoed']}"]
        lines += self._summary_deploy_lines(ask)
        if ask.context:
            lines.append("Worker's report: " + slack_text(ask.context, _SUMMARY_REPORT_SHOWN))
        return "\n".join(lines)

    def _summary_deploy_lines(self, ask: SlackAsk) -> list[str]:
        """Deploy outcomes of this task, worded from the hub's own check.  A deploy without a confirmed result says
        "unconfirmed" or "still being checked", never success."""
        checks = self.store.list_deploy_checks(ask.tenant_id, ask.user_id, worker_id=ask.worker_id, task_id=ask.task_id)
        return [f"Deploy {slack_text(c.project, 64)}/{slack_text(c.scope, 64)} {slack_text(c.sha[:12], 12)}: "
                f"{describe_status(c)}" for c in checks[-5:]]

    # -- deploy outcomes ---------------------------------------------------------------------------
    def _check_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_deploy_checks()
            except Exception as exc:                  # never ends the checker
                _log.warning("deploy check pass failed: %s", type(exc).__name__)
            self._stop.wait(self._check_interval)

    def run_deploy_checks(self) -> int:
        """One pass over every open deploy check (resumes whatever a restart left ``awaiting``).  A check that reached
        a final status is queued for its Slack message.  Returns how many reached a final status."""
        finished = 0
        now = as_utc(self._now())
        for check in self.store.list_deploy_checks_any(status="awaiting"):
            if self._stop.is_set():
                break
            done = verify_deploy(self.store, check, self.health, now, reverify=self._reverify,
                                 lineage=self._lineage, deploy_cfg=self.service.config.get("deploy") or {})
            if done.status in FINAL:
                finished += 1
                self.notify_deploy_outcome(done)
        return finished

    def _reverify(self, check: DeployCheck) -> str:
        """After a healthy result: is the deployed commit still on main's history?  (The preconditions were verified
        before the worker ran the deploy; main may have moved in between.)"""
        clients = self.preconditions
        if clients is None or clients.gh is None or not check.repo:
            return "unverified"
        repo = f"github.com/{check.repo}"
        _, tip = clients.gh.default_branch_head(repo)
        if not isinstance(tip, str) or not tip:
            return "unverified"
        if tip == check.sha:
            return "on_main"
        span = clients.gh.commits_between(repo, check.sha, tip)
        if span.status in ("ahead", "identical"):
            return "on_main"
        return "off_main" if span.status in ("behind", "diverged") else "unverified"

    def _lineage(self, check: DeployCheck, served: str) -> str:
        """Is the commit now serving a descendant of the target?  ``descendant`` | ``other`` | ``unverified``."""
        clients = self.preconditions
        if clients is None or clients.gh is None or not check.repo:
            return "unverified"
        span = clients.gh.commits_between(f"github.com/{check.repo}", check.sha, served)
        if span.status in ("ahead", "identical"):
            return "descendant"
        return "other" if span.status in ("behind", "diverged") else "unverified"

    def notify_deploy_outcome(self, check: DeployCheck) -> None:
        """Queue the Slack message for a finished check.  Never blocks and never raises."""
        try:
            if not self._cfg().get("enabled") or check.status not in FINAL or check.notified:
                return
            self._enqueue(_Queued(check.tenant_id, check.user_id, check.check_id, check.worker_id, check.task_id, "",
                                  "", check.project, as_utc(self._now()), kind="outcome",
                                  urgent=check.status == "failed_rollback"))
        except Exception as exc:
            _log.warning("slack outcome notify failed: %s", type(exc).__name__)

    def _sweep_outcomes(self, now: datetime) -> int:
        queued = 0
        mono = time.monotonic()
        for check in self.store.list_deploy_checks_any(unnotified=True):
            if mono - self._failed_at.get(check.check_id, -1e9) < self._reoffer_backoff:
                continue
            urgent = check.status == "failed_rollback"
            with self._qcond:
                if check.check_id == self._current or any(i.kind == "outcome" and i.request_id == check.check_id
                                                          for i in self._queue):
                    continue
                if len(self._queue) >= self._qsize and not urgent:
                    break
            self._enqueue(_Queued(check.tenant_id, check.user_id, check.check_id, check.worker_id,
                                  check.task_id, "", "", check.project, now, kind="outcome", urgent=urgent))
            queued += 1
        return queued

    def _post_outcome(self, item: _Queued, cfg: dict[str, Any]) -> bool:
        """Post one deploy outcome.  Never rate-limited or batched: it does not count against the task's asks, and
        ``failed_rollback`` is an urgent message of its own."""
        check = self.store.get_deploy_check(item.request_id, tenant_id=item.tenant_id, user_id=item.user_id)
        if check is None or check.notified or check.status not in FINAL:
            return True
        urgent = check.status == "failed_rollback"
        text = ("URGENT: a deploy failed to roll back" if urgent else
                f"Deploy {short_label(check)}")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": self._outcome_text(check)}}]
        channel = self._target(cfg)
        self.client.post_message(channel, blocks, text)
        with self.store._lock:
            current = self.store.get_deploy_check(check.check_id, tenant_id=check.tenant_id, user_id=check.user_id)
            if current is not None:
                current.notified = True
                self.store.put_deploy_check(current)
        self.store.audit_record(tenant_id=check.tenant_id, user_id=check.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.deploy_outcome_posted", subject_id=check.check_id,
                                detail=f"check={check.check_id} status={check.status}"
                                       f"{' urgent=yes' if urgent else ''}")
        return True

    @staticmethod
    def _outcome_text(check: DeployCheck) -> str:
        """Ids, the short sha and fixed words only: no command, no remote text."""
        head = f"`{slack_text(check.project, 64)}/{slack_text(check.scope, 64)}` at `{slack_text(check.sha[:12], 12)}`"
        if check.status == "failed_rollback" and check.last_error == NO_CHECK:
            return (f":rotating_light: *URGENT: worker `{slack_text(check.worker_id)}` reported deploy.sh exit 3 "
                    "(failed to roll back), but the hub has no deploy check for it.* The control plane may be broken "
                    "or half-updated. This needs you now.")
        if check.status == "failed_rollback":
            return (f":rotating_light: *URGENT: the deploy of {head} failed to roll back.* The control plane may be "
                    f"broken or half-updated. Worker `{slack_text(check.worker_id)}` reported deploy.sh exit 3. "
                    "This needs you now.")
        if check.status == "healthy":
            line = f"*Deploy healthy:* {head} is serving."
            if check.reverified == "newer":
                line = f"*Deploy healthy:* a newer version that contains {head} is serving."
            elif check.reverified == "off_main":
                line += " Warning: that commit is no longer on main's history."
            elif check.reverified != "on_main":
                line += " Main could not be re-checked after the deploy."
            return line
        if check.status == "rolled_back" and check.exit_code == 1:
            return (f"*Deploy not live:* {head} was rolled back: worker `{slack_text(check.worker_id)}` reported "
                    "deploy.sh exit 1.")
        if check.status == "rolled_back":
            return (f"*Deploy not live:* {head} is not serving. A different version is serving "
                    f"(`{slack_text(check.seen_commit, 12)}`); the new version is not live; cause unknown.")
        if check.last_error == NOT_OPENED:
            return (f"*Deploy NOT verified by the hub:* {head} was authorized but the hub could not open its outcome "
                    "check. Do not assume it worked.")
        return (f"*Deploy unconfirmed:* the hub could not confirm {head} within the check window"
                f"{' (health ' + 'reported ' + slack_text(check.seen_commit, 12) + ')' if check.seen_commit else ''}."
                " Do not assume it worked.")

    def _sweep_questions(self, now: datetime) -> int:
        """Questions stored but never posted (a restart, or the queue dropped them) are queued again until they expire."""
        queued = 0
        for ask in self.store.list_unposted_slack_questions():
            if now >= as_utc(ask.expires_at):
                ask.status = "expired"
                self.store.put_slack_ask(ask)
                continue
            with self._qcond:
                if len(self._queue) >= self._qsize:
                    break
                if ask.ask_id == self._current or any(i.ask is not None and i.ask.ask_id == ask.ask_id
                                                      for i in self._queue):
                    continue
                self._queue.append(self._question_item(ask, now))
                self._qcond.notify()
            queued += 1
        return queued

    def _expired(self, item: _Queued) -> bool:
        ttl = timedelta(minutes=int(self._cfg().get("ask_ttl_minutes") or 15))
        return as_utc(self._now()) - item.queued_at >= ttl

    def _audit_failed(self, item: _Queued, code: str) -> None:
        if item.ask is not None and item.kind != "summary":
            item.ask.status = "expired"
            self.store.put_slack_ask(item.ask)
        subject = item.request_id or (item.ask.ask_id if item.ask is not None else "-")
        self.store.audit_record(tenant_id=item.tenant_id, user_id=item.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.ask_failed", subject_id=subject,
                                detail=f"{'request' if item.request_id else 'ask'}={subject} code={code}")

    def _target(self, cfg: dict[str, Any]) -> str:
        if cfg.get("channel"):
            return str(cfg["channel"])
        owner = str(cfg["owner_user_id"])
        if owner not in self._dm:
            self._dm[owner] = self.client.open_dm(owner)
        return self._dm[owner]

    def _post_ask(self, item: _Queued) -> bool:
        """True when the item is finished (posted, batched, or no longer needed); SlackError means retry later."""
        cfg = self._cfg()
        if not cfg.get("enabled"):
            return True
        now = as_utc(self._now())
        if item.kind == "summary":
            return self._post_summary(item, cfg, now)
        if item.kind == "outcome":
            return self._post_outcome(item, cfg)
        question = item.kind == "question"
        if question:
            current = self.store.get_slack_ask(item.ask.ask_id, tenant_id=item.tenant_id,
                                               user_id=item.user_id) if item.ask is not None else None
            if current is None or current.status != "open" or current.ts or now >= as_utc(current.expires_at):
                return True                  # answered, expired, already posted (a duplicate entry) or gone
            item.ask = current
        else:
            row = self.store.get_action_request(item.request_id, tenant_id=item.tenant_id, user_id=item.user_id)
            if row is None or row.status != "pending":
                return True                              # decided meanwhile (REST) or gone: nothing to ask
            own = item.ask.ask_id if item.ask is not None else ""
            if any(a.ts and a.ask_id != own and a.status in ("open", "decided")
                   for a in self.store.slack_asks_for_request(item.request_id)):
                return True                  # a duplicate queue entry (sweep vs notify): one ask per request
        # three windows, questions counting apart from actions: the task's, the worker's (task ids are the worker's own
        # words, so many ids must not buy many windows) and a global one; the first one that is full takes the item
        windows = (((item.tenant_id, item.user_id, item.worker_id, item.task_id, item.kind),
                    int(cfg.get("max_asks_per_task_per_hour") or 6), "task"),
                   ((item.tenant_id, item.user_id, item.worker_id, "*", item.kind),
                    int(cfg.get("max_asks_per_worker_per_hour") or DEFAULT_WORKER_ASKS), "worker"),
                   (("*", "*", "*", "*", item.kind), int(cfg.get("max_asks_per_hour") or DEFAULT_GLOBAL_ASKS), "all"))
        all_stamps: list[deque[datetime]] = []
        for key, limit, scope in windows:
            stamps = self._posted.pop(key, None) or deque()
            self._posted[key] = stamps                       # most recently used last, so the cap drops the idlest
            while stamps and now - stamps[0] >= _HOUR:
                stamps.popleft()
            all_stamps.append(stamps)
            if len(stamps) >= limit:
                if question and item.ask is not None and item.ask.ask_id in self._batched:
                    return True              # already counted in the batch line; the sweep offers it again later
                return self._batch(item, key, cfg, now, scope)
        channel = self._target(cfg)
        ttl = timedelta(minutes=int(cfg.get("ask_ttl_minutes") or 15))
        ask = item.ask
        if ask is None:                  # stored before it is posted: a tap needs the ts, which only a post yields
            ask = item.ask = SlackAsk(ask_id=new_ask_id(), tenant_id=item.tenant_id, user_id=item.user_id,
                                      request_id=item.request_id, task_id=item.task_id, nonce=new_nonce(),
                                      channel=channel, created_at=now, expires_at=now + ttl)
            self.store.put_slack_ask(ask)
        if question:
            text = f"Question from worker {slack_text(item.worker_id)}"
            blocks = self._question_blocks(ask)
        else:
            text = f"Approval needed from worker {slack_text(item.worker_id)}"
            blocks = self._ask_blocks(ask, item)
        ts = self.client.post_message(channel, blocks, text)
        ask.ts, ask.channel, ask.created_at, ask.expires_at = ts, channel, now, now + ttl
        self.store.put_slack_ask(ask)
        for stamps in all_stamps:
            stamps.append(now)
        self.store.audit_record(tenant_id=item.tenant_id, user_id=item.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.ask_posted", subject_id=ask.ask_id,
                                detail=f"ask={ask.ask_id} request={item.request_id} worker={_short_id(item.worker_id)}"
                                       f"{' kind=question' if question else ''}")
        return True

    @staticmethod
    def _question_blocks(ask: SlackAsk) -> list[dict[str, Any]]:
        lines = [f"*Question* from worker `{slack_text(ask.worker_id)}`", slack_text(ask.question, _QUESTION_TEXT)]
        if ask.options:
            lines.append("Options: " + " | ".join(slack_text(o, 120) for o in ask.options))
        if ask.context:
            lines.append("Context: " + slack_text(ask.context, _QUESTION_TEXT))
        lines.append("_Reply in this thread to answer._")
        return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]

    @staticmethod
    def _ask_blocks(ask: SlackAsk, item: _Queued) -> list[dict[str, Any]]:
        lines = [f"*Approval needed* from worker `{slack_text(item.worker_id)}`"]
        if item.project:
            lines.append(f"Project: {slack_text(item.project)}")
        lines.append("```" + slack_text(item.display) + "```")
        lines.append(f"Why: {slack_text(item.why)}")
        value = button_value(ask)

        def button(label: str, action_id: str, style: str = "") -> dict[str, Any]:
            out: dict[str, Any] = {"type": "button", "text": {"type": "plain_text", "text": label},
                                   "action_id": action_id, "value": value}
            if style:
                out["style"] = style
            return out

        return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
                {"type": "actions", "elements": [button("Approve", "ask_approve", "primary"),
                                                 button("Deny", "ask_deny", "danger"),
                                                 button("Approve and remember", "ask_remember")]}]

    def _batch(self, item: _Queued, key: tuple[str, str, str, str, str], cfg: dict[str, Any], now: datetime,
               scope: str = "task") -> bool:
        """Over the per-task, per-worker or global limit: one line per hour and limit, edited to say how many more are
        waiting."""
        batch = self._batches.get(key)
        if batch is not None and now - batch.since >= _HOUR:
            batch = None
        count = (batch.count if batch else 0) + 1
        who = {"task": f"worker {slack_text(item.worker_id)}, "
                       f"{'session' if item.kind == 'question' else 'task'} {slack_text(item.task_id)}",
               "worker": f"worker {slack_text(item.worker_id)} (many tasks)", "all": "all workers"}[scope]
        if item.kind == "question":
            text = f"{count} more questions from {who} waiting (limit reached; they are posted as the hour allows)"
        else:
            text = f"{count} more waiting from {who} (limit reached; decide them in the dashboard)"
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        if batch is None:
            channel = self._target(cfg)
            self._batches[key] = _Batch(channel, self.client.post_message(channel, blocks, text), count, now)
        else:
            self.client.update_message(batch.channel, batch.ts, blocks, text)
            batch.count = count
        subject = item.request_id
        if item.kind == "question" and item.ask is not None:
            subject = item.ask.ask_id                # stays open and unposted: the sweep posts it when the window frees
            self._batched[subject] = None
        else:
            self._batched[item.request_id] = None
        self._cap(self._batched)
        self.store.audit_record(tenant_id=item.tenant_id, user_id=item.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.ask_batched", subject_id=subject,
                                detail=f"{'ask' if item.kind == 'question' else 'request'}={subject} waiting={count}")
        return True

    # -- taps --------------------------------------------------------------------------------------
    def _scope(self) -> tuple[str, str]:
        hub = self.service.config.get("hub") or {}
        return str(hub.get("tenant") or "default"), "slack-owner"

    def _refuse(self, reason: str, ask: SlackAsk | None, uid: object) -> None:
        tenant, user = (ask.tenant_id, ask.user_id) if ask is not None else self._scope()
        ask_id = ask.ask_id if ask is not None else "-"
        self.store.audit_record(tenant_id=tenant, user_id=user, actor=SLACK_ACTOR, action="assistant.slack.refused",
                                subject_id=ask_id, detail=f"reason={reason} ask={ask_id} user={_short_id(uid)}")

    def handle_interaction(self, payload: dict[str, Any]) -> None:
        with self._tap:
            self._handle(payload)

    def _handle(self, payload: dict[str, Any]) -> None:
        cfg = self._cfg()
        user = payload.get("user")
        uid = user.get("id") if isinstance(user, dict) else None
        owner = cfg.get("owner_user_id") or ""
        if not owner or not isinstance(uid, str) or not hmac.compare_digest(uid.encode(), owner.encode()):
            return self._refuse("not_owner", None, uid)
        actions = payload.get("actions")
        action = actions[0] if isinstance(actions, list) and len(actions) == 1 and isinstance(actions[0], dict) else {}
        value, action_id = action.get("value"), action.get("action_id")
        parts = value.split(":") if isinstance(value, str) else []
        deploy_tap = action_id in DEPLOY_ACTIONS
        if payload.get("type") != "block_actions" or len(parts) != 2 or not (action_id in ASK_ACTIONS or deploy_tap):
            return self._refuse("bad_payload", None, uid)
        ask = self.store.find_slack_ask(parts[0])
        if ask is None or ask.kind != ("deploy" if deploy_tap else "action"):      # a button only acts on its own kind
            return self._refuse("no_ask", None, uid)
        if not ask.nonce or not hmac.compare_digest(parts[1].encode(), ask.nonce.encode()):
            return self._refuse("bad_nonce", ask, uid)
        if ask.status != "open":
            return self._refuse("not_open", ask, uid)
        now = as_utc(self._now())
        if now >= as_utc(ask.expires_at):
            return self._refuse("expired", ask, uid)
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
        if (not ask.channel or not ask.ts or channel.get("id") != ask.channel or message.get("ts") != ask.ts
                or "edited" in message):
            return self._refuse("wrong_message", ask, uid)
        if deploy_tap:
            return self._deploy_tap(ask, DEPLOY_ACTIONS[action_id], uid, now)
        row = self.store.get_action_request(ask.request_id, tenant_id=ask.tenant_id, user_id=ask.user_id)
        if row is None:
            return self._refuse("no_request", ask, uid)
        if row.status == "vetoed":
            return self._refuse("vetoed", ask, uid)
        if row.status != "pending":
            return self._close_stale(ask, row.display, row.status, uid)
        approve, remember = ASK_ACTIONS[action_id]
        try:
            record_owner_decision(self.store, self.service.config["grants"], now, ask.tenant_id, ask.user_id,
                                  ask.request_id, approve, f"slack:{uid}", provenance="owner_slack",
                                  judge_version=current_judge_version(self.judge))
        except (ValueError, KeyError):          # decided or vetoed by another path between the check and the call
            current = self.store.get_action_request(ask.request_id, tenant_id=ask.tenant_id, user_id=ask.user_id)
            return self._close_stale(ask, row.display, current.status if current is not None else "gone", uid)
        ask.status, ask.remember = "decided", remember
        ask.worker_id, ask.key_id = row.worker_id, row.key_id
        if approve and remember:
            ask.activation = "pending"              # kept until a grant is active: a refusal does not forget the wish
        self.store.put_slack_ask(ask)
        outcome = "approve" if approve else "deny"
        self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=f"slack:{uid}",
                                action="assistant.slack.decision", subject_id=ask.ask_id,
                                detail=f"{outcome} ask={ask.ask_id} request={ask.request_id}"
                                       f"{' remember' if remember else ''}")
        self._show_outcome(ask, row.display, approve, uid)
        if approve:
            self._remember(ask, row, uid, now)

    # -- standing deploy permission ----------------------------------------------------------------
    def _deploy_scope(self, project: str, scope: str) -> tuple[str, str] | None:
        """(repo, command) of a configured deploy scope, or None."""
        proj = ((self.service.config.get("deploy") or {}).get("projects") or {}).get(project)
        found = ((proj or {}).get("scopes") or {}).get(scope)
        if not isinstance(proj, dict) or not isinstance(found, dict):
            return None
        return str(proj["repo"]), str(found["command"])

    def ask_deploy_auto(self, tenant_id: str, user_id: str, project: str, scope: str, worker_id: str) -> SlackAsk:
        """Post the owner's confirmation for turning on auto-deploy and return its ask.  NOTHING is granted here: the
        standing grant exists only after the owner's verified Confirm tap.  Older open asks for the same worker and scope
        are retired.  Raises ``SlackError`` (no usable ask is left behind) when it cannot be posted."""
        cfg = self._cfg()
        if not cfg.get("enabled"):
            raise SlackError("disabled")
        found = self._deploy_scope(project, scope)
        if found is None:
            raise SlackError("not_configured")
        repo, command = found
        now = as_utc(self._now())
        ttl = timedelta(minutes=int(cfg.get("ask_ttl_minutes") or 15))
        with self._deploy_lock:
            for old in self.store.list_slack_asks(tenant_id, user_id, "open"):
                if (old.kind, old.project, old.scope, old.worker_id) == ("deploy", project, scope, worker_id):
                    old.status = "expired"
                    self.store.put_slack_ask(old)
            channel = self._target(cfg)
            ask = SlackAsk(ask_id=new_ask_id(), tenant_id=tenant_id, user_id=user_id, kind="deploy", nonce=new_nonce(),
                           channel=channel, created_at=now, expires_at=now + ttl, worker_id=worker_id,
                           project=project, scope=scope, repo=repo, command=command)
            self.store.put_slack_ask(ask)
            try:
                ts = self.client.post_message(channel, self._deploy_blocks(ask, repo, command),
                                              f"Turn on auto-deploy for {slack_text(project)} / {slack_text(scope)}?")
            except SlackError:
                ask.status = "expired"
                self.store.put_slack_ask(ask)
                raise
            ask.ts = ts
            self.store.put_slack_ask(ask)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.ask_posted", subject_id=ask.ask_id,
                                detail=f"ask={ask.ask_id} kind=deploy project={_short_id(project)} "
                                       f"scope={_short_id(scope)} worker={_short_id(worker_id)}")
        return ask

    @staticmethod
    def _deploy_blocks(ask: SlackAsk, repo: str, command: str) -> list[dict[str, Any]]:
        lines = ["*Turn on auto-deploy?*",
                 f"Project: `{slack_text(ask.project)}`  Scope: `{slack_text(ask.scope)}`",
                 f"Repo: `{slack_text(repo)}`  Command: `{slack_text(command)}`",
                 f"Worker: `{slack_text(ask.worker_id)}`",
                 "If you confirm, this worker may deploy this scope without asking you each time, for any commit "
                 "that passes the hub's own checks, until the permission expires or you revoke it. "
                 "The safety judge, the shadow period and every veto still apply. Cancel does nothing."]
        value = button_value(ask)

        def button(label: str, action_id: str, style: str) -> dict[str, Any]:
            return {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action_id,
                    "value": value, "style": style}

        return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
                {"type": "actions", "elements": [button("Confirm", "deploy_confirm", "primary"),
                                                 button("Cancel", "deploy_cancel", "danger")]}]

    def retire_deploy_asks(self, tenant_id: str, user_id: str, project: str, scope: str, worker_id: str) -> int:
        """Close every open confirmation for this worker and scope (the owner revoked it): a later tap is refused."""
        closed = 0
        with self._tap:
            for old in self.store.list_slack_asks(tenant_id, user_id, "open"):
                if (old.kind, old.project, old.scope, old.worker_id) == ("deploy", project, scope, worker_id):
                    old.status = "expired"
                    self.store.put_slack_ask(old)
                    closed += 1
        return closed

    def _deploy_tap(self, ask: SlackAsk, confirm: bool, uid: str, now: datetime) -> None:
        """A verified tap on a deploy confirmation (owner, nonce, open, unexpired, the very message).  The ask is spent
        first, so a second tap can never create a second grant; Confirm creates the standing grant through the real
        gates and Cancel creates nothing."""
        if ask.status != "open":
            return self._refuse("not_open", ask, uid)
        ask.status = "decided"
        self.store.put_slack_ask(ask)
        outcome = "deploy_confirm" if confirm else "deploy_cancel"
        self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=f"slack:{uid}",
                                action="assistant.slack.decision", subject_id=ask.ask_id,
                                detail=f"{outcome} ask={ask.ask_id} project={_short_id(ask.project)} "
                                       f"scope={_short_id(ask.scope)} worker={_short_id(ask.worker_id)}")
        label = f"{slack_text(ask.project)} / {slack_text(ask.scope)}"
        if not confirm:
            return self._edit_message(ask, f"Auto-deploy for {label}", f"Cancelled by <@{uid}>; nothing was turned on")
        refusal = self._confirm_deploy(ask, uid, now)
        if refusal is None:
            self._edit_message(ask, f"Auto-deploy for {label}", f"Confirmed by <@{uid}>")
        else:
            self._edit_message(ask, f"Auto-deploy for {label}", f"Not turned on (confirmed by <@{uid}>)")
            self._say(ask, f"Auto-deploy was not turned on: {_SENTENCES.get(refusal, _SENTENCES['judge_error'])}.")

    def _confirm_deploy(self, ask: SlackAsk, uid: str, now: datetime) -> str | None:
        """None when the standing grant exists (now or already), otherwise the refusal code.  Re-checks everything the
        ask said against the CURRENT config and worker: a changed scope or a revoked worker creates nothing."""
        found = self._deploy_scope(ask.project, ask.scope)
        worker = self.store.workers.get(ask.worker_id)
        if found is None or found != (ask.repo, ask.command):
            return "not_configured"
        if scope_mode(self.service.config.get("deploy"), ask.project, ask.scope, ask.repo) != "auto":
            return "mode_not_auto"
        if worker is None or worker.status != "active" or (worker.tenant_id, worker.user_id) != (ask.tenant_id,
                                                                                               ask.user_id):
            return "no_worker"
        key = scope_key(DeployMatch(ask.project, ask.scope, found[0], "", ""))
        try:
            create_standing_grant(self.store, self.service.config["grants"], now, ask.tenant_id, ask.user_id,
                                  ask.worker_id, key, f"slack:{uid}", current_judge_version(self.judge), self.judge)
        except ActivationRefused as exc:
            return exc.code
        except Exception as exc:              # nothing was created; the owner is told the check could not run
            _log.warning("slack deploy confirm failed: %s", type(exc).__name__)
            return "judge_error"
        return None

    # -- approve and remember ----------------------------------------------------------------------
    def _remember(self, ask: SlackAsk, row: ActionRequestRow, uid: str, now: datetime) -> None:
        """After an owner_slack approval: try to turn every kept "remember" wish for this worker and key into an active
        grant through the real gates.  No gate is relaxed and nothing polls; the next approval is the next attempt."""
        wishes = [a for a in self.store.list_slack_asks(ask.tenant_id, ask.user_id)
                  if a.activation == "pending" and a.worker_id == row.worker_id and a.key_id == row.key_id]
        if not wishes:
            return
        try:
            refusal = self._activate(ask, row, uid, now)
        except Exception as exc:                  # the approval stands; the wish is kept for the next one
            _log.warning("slack remember failed: %s", type(exc).__name__)
            refusal = "judge_error"
        if refusal is None:
            for wish in wishes:
                wish.activation = "done"
                self.store.put_slack_ask(wish)
                self._say(wish, "Remembered. This worker may now run this exact action without asking you, until the "
                                "permission expires or you revoke it in the dashboard.")
        elif refusal == "not_rememberable":
            for wish in wishes:
                wish.activation = ""
                self.store.put_slack_ask(wish)
        if refusal is not None and ask.activation == "pending":
            tail = "" if refusal == "not_rememberable" else " I will try again after your next approval of this action."
            self._say(ask, f"Approved, but not remembered yet: {_SENTENCES.get(refusal, _SENTENCES['judge_error'])}."
                           f"{tail}")

    def _activate(self, ask: SlackAsk, row: ActionRequestRow, uid: str, now: datetime) -> str | None:
        """None when the grant is active (now or already), otherwise the refusal code."""
        cfg = self.service.config["grants"]
        if row.verb == Verb.UNKNOWN.value or not row.key_canonical:
            return "not_rememberable"
        key = ActionKey.from_canonical(row.key_canonical)
        if key.key_id() != row.key_id:
            return "not_rememberable"
        if in_shadow(self.store, cfg, now):
            return "still_in_shadow"
        version = current_judge_version(self.judge)
        grant = propose_if_ready(self.store, cfg, now, ask.tenant_id, ask.user_id, row.worker_id, key, vetoed=False,
                                 judge_version=version)
        if grant is None:
            return "not_enough_approvals"
        if grant.effective_state(now, int(cfg["policy_version"]), version) is GrantState.ACTIVE:
            return None
        try:
            activate_grant(self.store, cfg, now, ask.tenant_id, ask.user_id, grant.grant_id,
                           confirmed_by=f"slack:{uid}", judge=self.judge)
        except ActivationRefused as exc:
            return exc.code
        return None

    def _say(self, ask: SlackAsk, text: str) -> None:
        """A plain message in the ask's thread.  Our own fixed text only; a failure never undoes a decision."""
        try:
            self.client.post_message(ask.channel, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text,
                                     thread_ts=ask.ts)
        except SlackError as exc:
            self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=SLACK_ACTOR,
                                    action="assistant.slack.update_failed", subject_id=ask.ask_id,
                                    detail=f"ask={ask.ask_id} code={exc.code}")

    # -- owner replies -----------------------------------------------------------------------------
    def handle_event(self, payload: dict[str, Any]) -> None:
        with self._tap:
            self._handle_event(payload)

    def _handle_event(self, payload: dict[str, Any]) -> None:
        """The owner's threaded text reply to a question.  The text is data for the worker, never an approval."""
        event = payload.get("event") if payload.get("type") == "event_callback" else None
        if not isinstance(event, dict) or event.get("type") != "message":
            return
        if event.get("bot_id") or event.get("subtype") not in (None, "thread_broadcast"):
            return                      # our own posts echo back as bot messages; edits and joins carry a subtype
        parent, channel = event.get("thread_ts"), event.get("channel")
        if not isinstance(parent, str) or not parent or parent == event.get("ts") or not isinstance(channel, str):
            return                      # not a thread reply: channel traffic is none of our business, not even audited
        ask = self.store.find_slack_ask_by_message(channel, parent)
        if ask is None:
            return                      # not in the thread of a message we posted: silent, nothing written
        uid = event.get("user")
        owner = self._cfg().get("owner_user_id") or ""
        if not owner or not isinstance(uid, str) or not hmac.compare_digest(uid.encode(), owner.encode()):
            return self._refuse("not_owner", ask, uid)
        if ask.kind != "question":
            return self._refuse("not_question", ask, uid)
        if ask.status != "open":
            return self._refuse("not_open", ask, uid)
        if as_utc(self._now()) >= as_utc(ask.expires_at):
            ask.status = "expired"
            self.store.put_slack_ask(ask)
            return self._refuse("expired", ask, uid)
        text = event.get("text")
        answer = _ANSWER_CONTROL.sub("", text).strip() if isinstance(text, str) else ""
        answer = answer.encode("utf-8")[:_ANSWER_BYTES].decode("utf-8", "ignore")
        if not answer:
            return self._refuse("empty", ask, uid)
        ask.answer, ask.status = answer, "decided"
        self.store.put_slack_ask(ask)
        self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=f"slack:{uid}",
                                action="assistant.slack.answer", subject_id=ask.ask_id,
                                detail=f"ask={ask.ask_id} worker={_short_id(ask.worker_id)} bytes={len(answer.encode())}")
        self._say(ask, "Recorded. The worker will see your answer.")

    def _close_stale(self, ask: SlackAsk, display: str, status: str, uid: object) -> None:
        """The request was decided another way: refuse the tap, close the ask and take the buttons off the message.

        Only a decided ask's message is edited: the runtime refuses taps on a message that carries ``edited``."""
        self._refuse("request_not_pending", ask, uid)
        ask.status = "decided"
        self.store.put_slack_ask(ask)
        self._edit_message(ask, display, f"Already {slack_text(status)} elsewhere")

    def _show_outcome(self, ask: SlackAsk, display: str, approve: bool, uid: str) -> None:
        verdict = "Approved" if approve else "Denied"
        self._edit_message(ask, display, f"{verdict} by <@{uid}>")

    def _edit_message(self, ask: SlackAsk, display: str, text: str) -> None:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": slack_text(display or "Request")}},
                  {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]
        try:
            self.client.update_message(ask.channel, ask.ts, blocks, text)
        except SlackError as exc:               # the decision stands; the owner just sees stale buttons that now refuse
            self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=SLACK_ACTOR,
                                    action="assistant.slack.update_failed", subject_id=ask.ask_id,
                                    detail=f"ask={ask.ask_id} code={exc.code}")
