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
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..assistant import AssistantStore
from .actiongrants import as_utc
from .policy import record_owner_decision
from .slack_asks import SlackAsk

__all__ = ["SlackClient", "SlackError", "SlackRuntime", "ASK_ACTIONS"]

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


class SlackClient:
    """Slack Web API calls with the bot token.  Built per call from the live config, so rotation applies."""

    def __init__(self, cfg_getter: Callable[[], dict[str, Any]], timeout: float = 10.0) -> None:
        self._cfg = cfg_getter
        self._timeout = timeout

    def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self._cfg()
        if not cfg.get("enabled"):
            raise SlackError("disabled")
        token = _read_token(cfg["bot_token_file"])
        req = urllib.request.Request(
            cfg["base_url"].rstrip("/") + "/" + method, method="POST", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)    # never via a proxy
        try:
            with opener.open(req, timeout=self._timeout) as response:
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


def _short_id(value: object) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else "-"


class SlackRuntime:
    """Owns the Socket Mode connection and the tap handler.  ``start``/``stop`` are idempotent."""

    def __init__(self, store: AssistantStore, service: Any, cfg_getter: Callable[[], dict[str, Any]],
                 now_fn: Callable[[], datetime], *, min_backoff: float = 0.5, max_backoff: float = 30.0,
                 ping_interval: float = 2.0) -> None:
        self.store = store
        self.service = service
        self._cfg = cfg_getter
        self._now = now_fn
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._ping_interval = ping_interval        # a dead link is noticed after about five missed pings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = threading.Lock()
        self._tap = threading.Lock()           # one tap at a time: a double tap decides once
        self.client = SlackClient(cfg_getter)

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self) -> None:
        with self._state:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="slack-socket-mode", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._state:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is not None:
            thread.join(timeout=15)

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
        client = SocketModeClient(
            app_token=app_token, logger=sdk_log, auto_reconnect_enabled=False, ping_interval=self._ping_interval,
            web_client=WebClient(base_url=cfg["base_url"].rstrip("/") + "/", timeout=10))
        client.socket_mode_request_listeners.append(self._on_request)
        connected = False
        try:
            client.connect()
            connected = client.is_connected()
            while connected and not self._stop.is_set() and client.is_connected():
                self._stop.wait(0.1)
        finally:
            client.close()
        return connected

    def _on_request(self, client: Any, req: Any) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse
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
            return self._refuse("request_not_pending", ask, uid)
        approve, remember = ASK_ACTIONS[action_id]
        try:
            record_owner_decision(self.store, self.service.config["grants"], now, ask.tenant_id, ask.user_id,
                                  ask.request_id, approve, f"slack:{uid}", provenance="owner_slack")
        except (ValueError, KeyError):          # decided or vetoed by another path between the check and the call
            return self._refuse("request_not_pending", ask, uid)
        ask.status, ask.remember = "decided", remember
        self.store.put_slack_ask(ask)
        outcome = "approve" if approve else "deny"
        self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=f"slack:{uid}",
                                action="assistant.slack.decision", subject_id=ask.ask_id,
                                detail=f"{outcome} ask={ask.ask_id} request={ask.request_id}"
                                       f"{' remember' if remember else ''}")
        self._show_outcome(ask, row.display, approve, uid)

    def _show_outcome(self, ask: SlackAsk, display: str, approve: bool, uid: str) -> None:
        verdict = "Approved" if approve else "Denied"
        text = f"{verdict} by <@{uid}>"
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (display or "Request")[:2800]}},
                  {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]
        try:
            self.client.update_message(ask.channel, ask.ts, blocks, text)
        except SlackError as exc:               # the decision stands; the owner just sees stale buttons that now refuse
            self.store.audit_record(tenant_id=ask.tenant_id, user_id=ask.user_id, actor=SLACK_ACTOR,
                                    action="assistant.slack.update_failed", subject_id=ask.ask_id,
                                    detail=f"ask={ask.ask_id} code={exc.code}")
