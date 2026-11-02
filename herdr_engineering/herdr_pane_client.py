"""Client for herdr's local socket API (herdr 0.9.1, protocol 22), used by the host relay.

Wire format, confirmed against ``herdr api schema --json`` and a live server on bender:

* Unix stream socket ``~/.config/herdr/herdr.sock`` (0600, same user; override with the
  env var ``HERDR_SOCKET_PATH``). One JSON object per line, ``\\n`` terminated.
* Request  ``{"id": "<str>", "method": "pane.read", "params": {...}}``.
* Success  ``{"id": "<str>", "result": {"type": "<kind>", ...}}``; failure
  ``{"id": "<str>", "error": {"code": "<str>", "message": "<str>"}}``.
  One connection per request is fine: the server answers one line and we close.
* Methods used (params -> result payload key):
  ``pane.list {}`` -> ``panes``; ``pane.layout {pane_id}`` -> ``layout`` (area, panes[].rect);
  ``pane.read {pane_id, source: visible|recent, format: text|ansi, strip_ansi, lines}`` -> ``read.text``;
  ``pane.send_text {pane_id, text}``; ``pane.send_keys {pane_id, keys[]}`` (herdr's own key names,
  ``ctrl+c`` yes, ``ctrl-c``/Home/End/PageUp/PageDown/Delete/Insert no);
  ``pane.send_input {pane_id, text}`` writes ``text`` to the pty verbatim, so raw escape
  sequences (``\\x1b[H``) ride in it (verified: ``cat -v`` shows ``^[[H``);
  ``pane.zoom {pane_id, mode: on|off|toggle}``; ``pane.resize {pane_id, direction, amount}``.
* Subscription: ``events.subscribe {subscriptions: [{type, ...}]}`` answers
  ``{"result": {"type": "subscription_started"}}`` and then keeps the connection open, pushing
  ``{"event": "<kind>", "data": {...}}`` lines. herdr has no "output changed" event; the closest
  is ``pane.output_matched`` with a regex that matches any non-empty line, whose ``data.read``
  carries the visible screen and a ``revision``. Callers still re-read the screen on each event.

Every method is reachable over the socket, so there is no CLI fallback.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import time
import uuid
from typing import Iterator

from herdr_engineering.control_plane.terminal_attach import _PANE

MAX_BYTES = 4096        # per send_text / send_input call
MAX_KEYS = 32
MAX_REPLY = 8 * 1024 * 1024  # a pane list with many agents is large, but never this large
MAX_LINES = 100000

# Names herdr's send-keys accepts (confirmed on bender); anything else is refused here rather
# than relying on herdr to reject it. Unsupported keys go through send_input as raw bytes.
_KEY = re.compile(r"^(esc|tab|enter|backspace|space|up|down|left|right|f1|shift\+tab|alt\+left|ctrl\+left"
                  r"|ctrl\+[a-z\]]|ctrl\+space|alt\+[a-z])$")
_DIRECTIONS = ("left", "right", "up", "down")


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _pane(pane_id) -> str:
    if not isinstance(pane_id, str) or not _PANE.fullmatch(pane_id):
        raise ValueError("invalid pane id")
    return pane_id


class HerdrPaneClient:
    def __init__(self, socket_path: str | None = None, *, timeout: float = 3.0):
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH") or os.path.join(
            os.path.expanduser("~"), ".config", "herdr", "herdr.sock")
        self.timeout = timeout

    # -- transport ---------------------------------------------------------------------------
    def _connect(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.socket_path)
        except socket.timeout:
            s.close()
            raise HerdrError("timeout", "herdr socket did not accept in time") from None
        except OSError as e:
            s.close()
            raise HerdrError("unavailable", f"herdr socket not reachable ({e.__class__.__name__})") from None
        return s

    def _send(self, s: socket.socket, method: str, params: dict) -> str:
        rid = uuid.uuid4().hex
        try:
            s.sendall(json.dumps({"id": rid, "method": method, "params": params}).encode() + b"\n")
        except socket.timeout:
            raise HerdrError("timeout", "herdr socket write timed out") from None
        except OSError as e:
            raise HerdrError("unavailable", f"herdr socket write failed ({e.__class__.__name__})") from None
        return rid

    @staticmethod
    def _unwrap(msg: dict) -> dict:
        if isinstance(msg.get("error"), dict):
            e = msg["error"]
            raise HerdrError(str(e.get("code", "error")), str(e.get("message", "")))
        if not isinstance(msg.get("result"), dict):
            raise HerdrError("bad_reply", "herdr reply has no result")
        return msg["result"]

    def _readline(self, s: socket.socket, buf: bytearray, deadline: float | None) -> dict:
        # Reads one JSON line. deadline None = rely on the per-recv socket timeout (subscription).
        while b"\n" not in buf:
            if deadline is not None and time.monotonic() > deadline:
                raise HerdrError("timeout", "herdr did not answer in time")
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                raise HerdrError("timeout", "herdr did not answer in time") from None
            except OSError as e:
                raise HerdrError("unavailable", f"herdr socket read failed ({e.__class__.__name__})") from None
            if not chunk:
                raise HerdrError("closed", "herdr closed the connection")
            buf += chunk
            if len(buf) > MAX_REPLY:
                raise HerdrError("bad_reply", "herdr reply too large")
        line, _, rest = bytes(buf).partition(b"\n")
        buf[:] = rest
        try:
            msg = json.loads(line)
        except ValueError:
            raise HerdrError("bad_reply", "herdr reply is not JSON") from None
        if not isinstance(msg, dict):
            raise HerdrError("bad_reply", "herdr reply is not an object")
        return msg

    def _call(self, method: str, params: dict) -> dict:
        s = self._connect()
        try:
            self._send(s, method, params)
            return self._unwrap(self._readline(s, bytearray(), time.monotonic() + self.timeout))
        finally:
            s.close()

    # -- reads -------------------------------------------------------------------------------
    def list_panes(self) -> list[dict]:
        panes = self._call("pane.list", {}).get("panes")
        if not isinstance(panes, list):
            raise HerdrError("bad_reply", "pane.list has no panes")
        return panes

    def layout(self, pane_id: str) -> dict:
        pane_id = _pane(pane_id)
        lay = self._call("pane.layout", {"pane_id": pane_id}).get("layout") or {}
        for p in lay.get("panes") or []:
            if p.get("pane_id") == pane_id and isinstance(p.get("rect"), dict):
                r = p["rect"]
                return {"width": r["width"], "height": r["height"], "rect": r,
                        "area": lay.get("area"), "zoomed": bool(lay.get("zoomed"))}
        raise HerdrError("pane_not_in_layout", "pane is not in its tab layout")

    def _read(self, params: dict) -> str:
        text = (self._call("pane.read", params).get("read") or {}).get("text")
        if not isinstance(text, str):
            raise HerdrError("bad_reply", "pane.read has no text")
        return text

    def read_visible(self, pane_id: str) -> str:
        # strip_ansi False + ansi format: the phone renders colours.
        return self._read({"pane_id": _pane(pane_id), "source": "visible", "format": "ansi",
                           "strip_ansi": False})

    def read_recent(self, pane_id: str, lines: int) -> str:
        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= MAX_LINES:
            raise ValueError("invalid line count")
        return self._read({"pane_id": _pane(pane_id), "source": "recent", "lines": lines})

    # -- writes ------------------------------------------------------------------------------
    def send_text(self, pane_id: str, text: str) -> None:
        pane_id = _pane(pane_id)
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
            raise ValueError("invalid text")
        self._call("pane.send_text", {"pane_id": pane_id, "text": text})

    def send_keys(self, pane_id: str, keys: list[str]) -> None:
        pane_id = _pane(pane_id)
        if (not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS
                or not all(isinstance(k, str) and _KEY.fullmatch(k) for k in keys)):
            raise ValueError("invalid keys")
        self._call("pane.send_keys", {"pane_id": pane_id, "keys": list(keys)})

    def send_input(self, pane_id: str, data: bytes) -> None:
        pane_id = _pane(pane_id)
        if not isinstance(data, (bytes, bytearray)) or not 1 <= len(data) <= MAX_BYTES:
            raise ValueError("invalid input")
        try:
            # The API carries the bytes as a JSON string, so they must be valid UTF-8.
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("input is not valid UTF-8") from None
        self._call("pane.send_input", {"pane_id": pane_id, "text": text})

    def zoom(self, pane_id: str, on: bool) -> None:
        pane_id = _pane(pane_id)
        if not isinstance(on, bool):
            raise ValueError("invalid zoom")
        self._call("pane.zoom", {"pane_id": pane_id, "mode": "on" if on else "off"})

    def resize_split(self, pane_id: str, direction: str, amount: float) -> None:
        pane_id = _pane(pane_id)
        if direction not in _DIRECTIONS:
            raise ValueError("invalid direction")
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not math.isfinite(amount) or not 0 < amount <= 1):
            raise ValueError("invalid amount")
        self._call("pane.resize", {"pane_id": pane_id, "direction": direction, "amount": float(amount)})

    # -- subscription ------------------------------------------------------------------------
    def subscribe_output(self, pane_id: str) -> Iterator[dict | None]:
        """Yield ``data`` of each push event for the pane; yield None whenever the socket was idle
        for ``timeout`` seconds, so the caller can poll the screen anyway. Raises HerdrError when
        herdr refuses the subscription or the connection drops."""
        pane_id = _pane(pane_id)
        s = self._connect()
        try:
            buf = bytearray()
            self._send(s, "events.subscribe", {"subscriptions": [{
                "type": "pane.output_matched", "pane_id": pane_id, "source": "visible",
                "match": {"type": "regex", "value": ".+"}}]})
            self._unwrap(self._readline(s, buf, time.monotonic() + self.timeout))
            while True:
                try:
                    msg = self._readline(s, buf, None)
                except HerdrError as e:
                    if e.code != "timeout":
                        raise
                    yield None
                    continue
                if isinstance(msg.get("data"), dict):
                    yield msg["data"]
        finally:
            s.close()
