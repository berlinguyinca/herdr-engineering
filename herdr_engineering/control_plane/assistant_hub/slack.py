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
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..assistant import AssistantStore
from .actiongrants import ActionRequestRow, as_utc
from .policy import record_owner_decision
from .slack_asks import SlackAsk, button_value, new_ask_id, new_nonce

__all__ = ["SlackClient", "SlackError", "SlackRuntime", "ASK_ACTIONS", "slack_text", "slack_api_call"]

_log = logging.getLogger("herdr_engineering.assistant_hub.slack")
_MAX_REPLY = 1024 * 1024
ASK_ACTIONS = {"ask_approve": (True, False), "ask_deny": (False, False), "ask_remember": (True, True)}
_ID = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
SLACK_ACTOR = "hub:slack"


class SlackError(Exception):
    """A Slack call failed.  ``code`` is a short word (Slack's own error code or ``io``/``token``); never a body or token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(_MAX_REPLY)
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_REPLY)
    except (OSError, ValueError):
        raise SlackError("io") from None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise SlackError("bad_reply") from None
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        error = doc.get("error") if isinstance(doc, dict) else None
        raise SlackError(error if isinstance(error, str) and _ID.fullmatch(error) else "bad_reply")
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


def slack_text(value: str) -> str:
    """Worker-controlled text made inert for Slack mrkdwn: control characters are dropped, ``& < >`` are escaped
    (so ``<!channel>``, ``<@U123>`` and ``<url|label>`` print literally), ``@`` gets a zero-width space so a bare
    ``@here`` cannot notify, backticks become the look-alike modifier grave accent, and the result is at most 300
    characters (never cut inside an entity)."""
    out = _CONTROL.sub(" ", str(value))
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace("@", "@\u200b").replace("`", "\u02cb")
    if len(out) > _TEXT_MAX:
        out = out[:_TEXT_MAX - 1]
        amp = out.rfind("&")
        if amp > out.rfind(";"):
            out = out[:amp]
        out += "\u2026"
    return out


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


@dataclass
class _Batch:
    channel: str
    ts: str
    count: int
    since: datetime


_HOUR = timedelta(hours=1)


class SlackRuntime:
    """Owns the Socket Mode connection and the tap handler.  ``start``/``stop`` are idempotent."""

    def __init__(self, store: AssistantStore, service: Any, cfg_getter: Callable[[], dict[str, Any]],
                 now_fn: Callable[[], datetime], *, min_backoff: float = 0.5, max_backoff: float = 30.0,
                 ping_interval: float = 2.0, stop_timeout: float = 15.0,
                 queue_size: int = 200) -> None:
        self.store = store
        self.service = service
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
        self._queue: deque[_Queued] = deque()
        self._qcond = threading.Condition()
        self._drain_thread: threading.Thread | None = None
        self.dropped = 0
        self._posted: dict[tuple[str, str, str, str], deque[datetime]] = {}    # per (worker, task) post times
        self._batches: dict[tuple[str, str, str, str], _Batch] = {}
        self._dm: dict[str, str] = {}
        self._connected = False
        self._last_event: datetime | None = None
        self._tap = threading.Lock()           # one tap at a time: a double tap decides once
        self.client = SlackClient(cfg_getter)

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
                self._drain_thread = threading.Thread(target=self._drain, name="slack-asks", daemon=True)
                self._drain_thread.start()

    def stop(self) -> bool:
        """Stop the runtime.  True when both threads have ended; False when one did not end within ``stop_timeout``
        (it is still winding down, ``start`` refuses until it has, and a later ``stop`` can confirm).  Queued asks
        are kept in memory, so a restart posts them."""
        with self._state:
            threads = [self._thread, self._drain_thread]
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
                self._thread = self._drain_thread = None
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
        if req.type != "interactive" or not isinstance(req.payload, dict):
            return
        try:
            self.handle_interaction(req.payload)
        except Exception as exc:            # a bad payload must never take the connection down
            _log.warning("slack interaction failed: %s", type(exc).__name__)

    # -- owner asks --------------------------------------------------------------------------------
    def notify_pending(self, row: ActionRequestRow, display: str, why: str, *, project: str = "") -> None:
        """Queue a NEW pending request for the owner.  Never blocks and never raises: the caller is a request thread.

        The queue is bounded (oldest dropped); the runtime's own thread posts, retrying while Slack is unreachable."""
        try:
            if not self._cfg().get("enabled"):
                return
            item = _Queued(row.tenant_id, row.user_id, row.request_id, row.worker_id, row.task_id, display, why,
                           project, as_utc(self._now()))
            with self._qcond:
                if len(self._queue) >= self._qsize:
                    self._queue.popleft()
                    self.dropped += 1
                self._queue.append(item)
                self._qcond.notify()
        except Exception as exc:
            _log.warning("slack notify failed: %s", type(exc).__name__)

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
            with self._qcond:
                while not self._queue and not self._stop.is_set():
                    self._qcond.wait(1.0)
                if self._stop.is_set():
                    return
                item = self._queue[0]
            try:
                done = self._post_ask(item)
            except SlackError as exc:
                done = self._expired(item) or False
                if done:
                    self._audit_failed(item, exc.code)
                else:
                    self._stop.wait(delay)
                    delay = min(delay * 2, self._max_backoff)
            except Exception as exc:                  # never kill the loop; the ask stays with the owner's REST view
                _log.warning("slack ask failed: %s", type(exc).__name__)
                done = True
            if done:
                delay = self._min_backoff
                with self._qcond:
                    if self._queue and self._queue[0] is item:
                        self._queue.popleft()

    def _expired(self, item: _Queued) -> bool:
        ttl = timedelta(minutes=int(self._cfg().get("ask_ttl_minutes") or 15))
        return as_utc(self._now()) - item.queued_at >= ttl

    def _audit_failed(self, item: _Queued, code: str) -> None:
        if item.ask is not None:
            item.ask.status = "expired"
            self.store.put_slack_ask(item.ask)
        self.store.audit_record(tenant_id=item.tenant_id, user_id=item.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.ask_failed", subject_id=item.request_id,
                                detail=f"request={item.request_id} code={code}")

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
        row = self.store.get_action_request(item.request_id, tenant_id=item.tenant_id, user_id=item.user_id)
        if row is None or row.status != "pending":
            return True                                  # decided meanwhile (REST) or gone: nothing to ask
        now = as_utc(self._now())
        key = (item.tenant_id, item.user_id, item.worker_id, item.task_id)
        stamps = self._posted.setdefault(key, deque())
        while stamps and now - stamps[0] >= _HOUR:
            stamps.popleft()
        if len(stamps) >= int(cfg.get("max_asks_per_task_per_hour") or 6):
            return self._batch(item, key, cfg, now)
        channel = self._target(cfg)
        ttl = timedelta(minutes=int(cfg.get("ask_ttl_minutes") or 15))
        ask = item.ask
        if ask is None:                  # stored before it is posted: a tap needs the ts, which only a post yields
            ask = item.ask = SlackAsk(ask_id=new_ask_id(), tenant_id=item.tenant_id, user_id=item.user_id,
                                      request_id=item.request_id, task_id=item.task_id, nonce=new_nonce(),
                                      channel=channel, created_at=now, expires_at=now + ttl)
            self.store.put_slack_ask(ask)
        text = f"Approval needed from worker {slack_text(item.worker_id)}"
        ts = self.client.post_message(channel, self._ask_blocks(ask, item), text)
        ask.ts, ask.channel, ask.created_at, ask.expires_at = ts, channel, now, now + ttl
        self.store.put_slack_ask(ask)
        stamps.append(now)
        self.store.audit_record(tenant_id=item.tenant_id, user_id=item.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.ask_posted", subject_id=ask.ask_id,
                                detail=f"ask={ask.ask_id} request={item.request_id} worker={_short_id(item.worker_id)}")
        return True

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

    def _batch(self, item: _Queued, key: tuple[str, str, str, str], cfg: dict[str, Any], now: datetime) -> bool:
        """Over the per-task limit: one line per hour, edited to say how many more are waiting."""
        batch = self._batches.get(key)
        if batch is not None and now - batch.since >= _HOUR:
            batch = None
        count = (batch.count if batch else 0) + 1
        text = (f"{count} more waiting from worker {slack_text(item.worker_id)}, task {slack_text(item.task_id)}"
                " (limit reached; decide them in the dashboard)")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        if batch is None:
            channel = self._target(cfg)
            self._batches[key] = _Batch(channel, self.client.post_message(channel, blocks, text), count, now)
        else:
            self.client.update_message(batch.channel, batch.ts, blocks, text)
            batch.count = count
        self.store.audit_record(tenant_id=item.tenant_id, user_id=item.user_id, actor=SLACK_ACTOR,
                                action="assistant.slack.ask_batched", subject_id=item.request_id,
                                detail=f"request={item.request_id} waiting={count}")
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
        if payload.get("type") != "block_actions" or len(parts) != 2 or action_id not in ASK_ACTIONS:
            return self._refuse("bad_payload", None, uid)
        ask = self.store.find_slack_ask(parts[0])
        if ask is None or ask.kind != "action":
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
                                  ask.request_id, approve, f"slack:{uid}", provenance="owner_slack")
        except (ValueError, KeyError):          # decided or vetoed by another path between the check and the call
            current = self.store.get_action_request(ask.request_id, tenant_id=ask.tenant_id, user_id=ask.user_id)
            return self._close_stale(ask, row.display, current.status if current is not None else "gone", uid)
        ask.status, ask.remember = "decided", remember
        self.store.put_slack_ask(ask)
        outcome = "approve" if approve else "deny"
        self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=f"slack:{uid}",
                                action="assistant.slack.decision", subject_id=ask.ask_id,
                                detail=f"{outcome} ask={ask.ask_id} request={ask.request_id}"
                                       f"{' remember' if remember else ''}")
        self._show_outcome(ask, row.display, approve, uid)

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
