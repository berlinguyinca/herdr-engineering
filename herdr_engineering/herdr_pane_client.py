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
import stat
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
# ctrl+] and ctrl+space are kept on purpose: herdr returned ok for both on bender.
_KEY = re.compile(r"^(esc|tab|enter|backspace|space|up|down|left|right|f1|shift\+tab|alt\+left|ctrl\+left"
                  r"|ctrl\+[a-z\]]|ctrl\+space|alt\+[a-z])$")
_DIRECTIONS = ("left", "right", "up", "down")


class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        # herdr text is passed on to logs and phones: cap it.
        code, message = str(code)[:64], str(message)[:200]
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
    def _check_socket(self) -> None:
        # Never talk to (or send keystrokes into) something that is not our own socket: a
        # planted file, a symlink or another user's socket under a path override.
        try:
            st = os.lstat(self.socket_path)
        except OSError:
            raise HerdrError("unavailable", "herdr socket not reachable") from None
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise HerdrError("unavailable", "herdr socket is not a socket owned by this user")

    def _connect(self) -> socket.socket:
        self._check_socket()
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
            raise HerdrError(e.get("code", "error"), e.get("message", ""))
        if not isinstance(msg.get("result"), dict):
            raise HerdrError("bad_reply", "herdr reply has no result")
        return msg["result"]

    def _readline(self, s: socket.socket, buf: bytearray, deadline: float | None) -> dict:
        # Reads one JSON line. deadline None = rely on the per-recv socket timeout (subscription).
        idx = buf.find(b"\n")
        while idx < 0:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise HerdrError("timeout", "herdr did not answer in time")
                # A server dripping one byte at a time must not stretch the overall deadline.
                s.settimeout(min(self.timeout, left))
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                raise HerdrError("timeout", "herdr did not answer in time") from None
            except OSError as e:
                raise HerdrError("unavailable", f"herdr socket read failed ({e.__class__.__name__})") from None
            if not chunk:
                raise HerdrError("closed", "herdr closed the connection")
            start = len(buf)  # scan only the new bytes for the newline
            buf += chunk
            idx = buf.find(b"\n", start)
            if idx < 0 and len(buf) > MAX_REPLY:
                raise HerdrError("bad_reply", "herdr reply too large")
        if idx > MAX_REPLY:
            raise HerdrError("bad_reply", "herdr reply too large")
        line = bytes(buf[:idx])
        del buf[:idx + 1]
        try:
            msg = json.loads(line)
        except ValueError:
            raise HerdrError("bad_reply", "herdr reply is not JSON") from None
        if not isinstance(msg, dict):
            raise HerdrError("bad_reply", "herdr reply is not an object")
        return msg

    def _reply(self, s: socket.socket, buf: bytearray, rid: str, deadline: float) -> dict:
        # The reply is the line carrying our request id. Pushed events (no id) that arrive first
        # are skipped; any other id is a protocol error.
        while True:
            msg = self._readline(s, buf, deadline)
            if "id" not in msg and "event" in msg:
                continue
            if msg.get("id") != rid:
                raise HerdrError("bad_reply", "herdr reply id does not match the request")
            return self._unwrap(msg)

    def _call(self, method: str, params: dict) -> dict:
        s = self._connect()
        try:
            rid = self._send(s, method, params)
            return self._reply(s, bytearray(), rid, time.monotonic() + self.timeout)
        finally:
            s.close()

    # -- reads -------------------------------------------------------------------------------
    def list_panes(self) -> list[dict]:
        panes = self._call("pane.list", {}).get("panes")
        if not isinstance(panes, list) or not all(isinstance(p, dict) for p in panes):
            raise HerdrError("bad_reply", "pane.list has no panes")
        return panes

    def layout(self, pane_id: str) -> dict:
        pane_id = _pane(pane_id)
        lay = self._call("pane.layout", {"pane_id": pane_id}).get("layout")
        panes = lay.get("panes") if isinstance(lay, dict) else None
        if not isinstance(panes, list) or not all(isinstance(p, dict) for p in panes):
            raise HerdrError("bad_reply", "pane.layout has no panes")
        for p in panes:
            if p.get("pane_id") == pane_id:
                r = p.get("rect")
                if not (isinstance(r, dict) and all(
                        isinstance(r.get(k), int) and not isinstance(r.get(k), bool) for k in ("width", "height"))):
                    raise HerdrError("bad_reply", "pane.layout rect is malformed")
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
        # NOT a safe-text boundary: newlines and control bytes pass through by design (the phone
        # types into a real terminal). Callers decide what may be sent; we only bound the size.
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_BYTES:
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
            # Not a safe-text boundary either: control bytes and escapes pass through by design.
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
        # Validate now, not on first next(): a plain function returning the generator.
        return self._subscribe(_pane(pane_id))

    def _subscribe(self, pane_id: str) -> Iterator[dict | None]:
        """Yield ``data`` of each push event for the pane; yield None whenever the socket was idle
        for ``timeout`` seconds, so the caller can poll the screen anyway. Raises HerdrError when
        herdr refuses the subscription or the connection drops."""
        s = self._connect()
        try:
            buf = bytearray()
            rid = self._send(s, "events.subscribe", {"subscriptions": [{
                "type": "pane.output_matched", "pane_id": pane_id, "source": "visible",
                "match": {"type": "regex", "value": ".+"}}]})
            self._reply(s, buf, rid, time.monotonic() + self.timeout)
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
