"""Host side of the Shepherd terminal: mirror a herdr pane's screen and translate phone input.

``PaneMirror`` turns the pane's visible screen into full-screen replacement frames. herdr has no
per-redraw output event (only ``pane.output_matched``), so the screen is polled every 100 ms and
compared by hash; the subscription is not needed for correctness.

``InputTranslator`` turns the bytes a terminal emulator on the phone produces into actions that
``apply`` performs through the herdr client: printable runs as text, keys herdr's send-keys knows
as key names (never a name the client's allow-list refuses), everything else as raw bytes through
``pane.send_input``. Timing contract: the relay flushes a lone ESC after 50 ms of silence, so an ESC
followed by a letter in the NEXT chunk after that is Esc then text, not alt+letter. Nothing here runs a process or opens a socket of its own.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
import unicodedata

from herdr_engineering.herdr_pane_client import MAX_BYTES, MAX_KEYS, HerdrError

# An Action is ("text", str), ("keys", [names]) or ("raw", bytes).
Action = tuple

# The mirror owns the cap on the ENCODED (base64) frame, so a busy coloured screen cannot flood the
# broker: over the cap the colour is stripped, and if that is not enough trailing lines are dropped.
MAX_FRAME = 150 * 1024
_LAYOUT_TTL = 5.0  # seconds a layout (or its failure) is trusted before asking herdr again
_PASTE_MAX = 2 * 1024 * 1024  # paste text without an end marker after which key translation resumes
_PASTE_IDLE = 30.0            # ...or seconds without a paste byte
_CLEAR = "\x1b[0m\x1b[H\x1b[2J"
_POLL = 0.1
_EPS = 1e-6  # clock slack: a sleep of exactly min_interval must count as elapsed
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


class PaneMirror:
    def __init__(self, client, pane_id, *, now=time.monotonic, min_interval=0.1, sleep=time.sleep):
        self.client = client
        self.pane_id = pane_id
        self._now = now
        self._sleep = sleep
        self.min_interval = min_interval
        self._hash: bytes | None = None
        self._last: float | None = None  # when the last frame was handed out
        self._seen: tuple[str, bytes, float] | None = None  # (text, digest, read time) from wait_for_change
        self._size_at: float | None = None
        self._layout: tuple[int, int] | None = None  # (rows, cols) from pane.layout, None when it failed
        self._viewport: int | None = None

    @staticmethod
    def _digest(text: str) -> bytes:
        return hashlib.sha1(text.encode("utf-8", "surrogatepass")).digest()

    def _throttled(self) -> float:
        # Seconds left before another frame may go out (<= 0 when none to wait for).
        if self._last is None:
            return 0.0
        return self._last + self.min_interval - self._now() - _EPS

    @staticmethod
    def _width(line: str) -> int:
        w = 0
        for ch in _ANSI.sub("", line).expandtabs(8):
            if unicodedata.combining(ch):
                continue
            w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return w

    def _size(self, text: str) -> tuple[int, int]:
        # The layout (or the fact that it failed) is cached, so a failing layout() does not cost a
        # list_panes() call per frame.
        now = self._now()
        if self._size_at is None or now - self._size_at >= _LAYOUT_TTL:
            self._size_at, self._layout, self._viewport = now, None, None
            try:
                lay = self.client.layout(self.pane_id)
                self._layout = (int(lay["height"]), int(lay["width"]))
            except (HerdrError, ValueError, KeyError, TypeError, OSError):
                try:
                    for p in self.client.list_panes():
                        v = (p.get("scroll") or {}).get("viewport_rows")
                        if p.get("pane_id") == self.pane_id and isinstance(v, int) and not isinstance(v, bool) and v > 0:
                            self._viewport = v
                except (HerdrError, OSError):
                    pass
        if self._layout:
            return self._layout
        lines = text.split("\n")
        return self._viewport or len(lines), max(1, max(self._width(ln) for ln in lines))

    @staticmethod
    def _encode(lines: list[str]) -> str:
        body = "\r\n".join(lines)
        return base64.b64encode((_CLEAR + body).encode("utf-8", "replace")).decode("ascii")

    def _fit(self, text: str) -> str:
        lines = text.removesuffix("\n").replace("\r\n", "\n").split("\n")
        data = self._encode(lines)
        if len(data) <= MAX_FRAME:
            return data
        lines = [_ANSI.sub("", ln) for ln in lines]  # colour first ...
        data = self._encode(lines)
        while len(data) > MAX_FRAME and len(lines) > 1:  # ... then trailing lines
            del lines[max(1, len(lines) * MAX_FRAME // len(data)):]
            data = self._encode(lines)
        if len(data) > MAX_FRAME:  # one enormous line
            lines = [lines[0].encode("utf-8", "replace")[:(MAX_FRAME * 3 // 4) - len(_CLEAR)].decode("utf-8", "ignore")]
            data = self._encode(lines)
        return data

    def frame(self) -> dict | None:
        """The screen as one full replacement frame, or None when it is unchanged (or the last
        frame went out less than ``min_interval`` ago; the change is picked up on a later call)."""
        if self._throttled() > 0:
            return None
        seen, self._seen = self._seen, None
        if seen and self._now() - seen[2] <= 1.0:  # the read wait_for_change just made
            text, digest = seen[0], seen[1]
        else:
            text = self.client.read_visible(self.pane_id)
            digest = self._digest(text)
        if digest == self._hash:
            return None
        self._hash = digest
        self._last = self._now()
        rows, cols = self._size(text)
        return {"data": self._fit(text), "rows": rows, "cols": cols}

    def wait_for_change(self, timeout: float) -> bool:
        """Poll the screen every 100 ms until it differs from the last frame; False on timeout.
        Returns only once ``frame()`` is allowed to emit, so a caller loop does not spin; that
        can be up to ``min_interval`` after ``timeout``. The read that detected the change is kept
        for ``frame()`` (one read per emitted frame). HerdrError propagates: the relay must catch
        it and back off."""
        deadline = self._now() + timeout
        while True:
            text = self.client.read_visible(self.pane_id)
            digest = self._digest(text)
            if digest != self._hash:
                self._seen = (text, digest, self._now())
                wait = self._throttled()
                if wait > 0:
                    self._sleep(wait + _EPS)
                return True
            left = deadline - self._now()
            if left <= 0:
                return False
            self._sleep(min(_POLL, left))


# -- input translation -------------------------------------------------------------------------
_CSI_KEYS = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
             b"\x1b[Z": "shift+tab", b"\x1b[1;3D": "alt+left", b"\x1b[1;5D": "ctrl+left"}
_SS3_KEYS = {b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left", b"\x1bOP": "f1"}
_PASTE_ON = b"\x1b[200~"
_PASTE_OFF = b"\x1b[201~"
_MAX_SEQ = 32  # an escape sequence longer than this is not a key: forward it raw


def _split_text(s: str) -> list[Action]:
    b = s.encode("utf-8")
    if len(b) <= MAX_BYTES:
        return [("text", s)]
    out, pos = [], 0
    while pos < len(b):
        end = min(pos + MAX_BYTES, len(b))
        while end < len(b) and 0x80 <= b[end] < 0xc0:  # never cut inside a character
            end -= 1
        out.append(("text", b[pos:end].decode("utf-8")))
        pos = end
    return out


def _raw(data: bytes) -> list[Action]:
    return [("raw", data[i:i + MAX_BYTES]) for i in range(0, len(data), MAX_BYTES)]


_ESCAPED = re.compile("[\udc80-\udcff]+")  # bytes that were not valid UTF-8 (surrogateescape)


def _text_actions(run: bytes, at_end: bool) -> tuple[list[Action], bytes]:
    """Decode a run of printable bytes: valid UTF-8 is text, invalid bytes are raw. A truncated
    character at the very end of the buffer is returned as pending when more bytes may follow.
    One decode plus one regex pass: linear however the invalid bytes are scattered."""
    pending = b""
    if at_end:
        for k in range(1, min(4, len(run)) + 1):
            b = run[-k]
            if b < 0x80:
                break
            if b >= 0xc0:
                need = 2 if b < 0xe0 else 3 if b < 0xf0 else 4 if b < 0xf8 else 0
                if need > k:
                    pending, run = run[-k:], run[:-k]
                break
    s = run.decode("utf-8", "surrogateescape")
    out: list[Action] = []
    pos = 0
    for m in _ESCAPED.finditer(s):
        if m.start() > pos:
            out += _split_text(s[pos:m.start()])
        out += _raw(m.group().encode("utf-8", "surrogateescape"))
        pos = m.end()
    if pos < len(s):
        out += _split_text(s[pos:])
    return out, pending


class InputTranslator:
    def __init__(self, *, now=time.monotonic):
        self._now = now
        self._buf = b""    # an incomplete escape sequence or UTF-8 character, kept for the next call
        self._paste = False
        self._paste_bytes = 0
        self._paste_at = 0.0
        self._cr = False   # the last byte was CR: a directly following LF is the same Enter

    def _paste_expired(self) -> bool:
        # A lost end marker must not capture later keystrokes (Ctrl-C, arrows) for good.
        if self._paste and self._now() - self._paste_at > _PASTE_IDLE:
            self._paste = False
        return not self._paste

    def feed(self, data: bytes) -> list[Action]:
        self._paste_expired()
        if self._paste:
            self._paste_at = self._now()
        buf, self._buf = self._buf + bytes(data), b""
        out: list[Action] = []
        i, n = 0, len(buf)
        while i < n:
            if self._paste:
                i = self._feed_paste(buf, i, out)
                continue
            c = buf[i]
            cr, self._cr = self._cr, False
            if c == 0x0a and cr:
                i += 1
            elif c == 0x1b:
                used = self._escape(buf, i, out)
                if used == 0:
                    self._buf = buf[i:]
                    break
                i += used
            elif c in (0x0d, 0x0a):
                out.append(("keys", ["enter"])); i += 1
                self._cr = c == 0x0d
            elif c == 0x7f:
                out.append(("keys", ["backspace"])); i += 1
            elif c == 0x09:
                out.append(("keys", ["tab"])); i += 1
            elif c < 0x20:
                out.append(("keys", ["ctrl+" + chr(0x60 + c)]) if 1 <= c <= 0x1a else ("raw", bytes([c]))); i += 1
            else:
                j = i
                while j < n and buf[j] >= 0x20 and buf[j] != 0x7f:
                    j += 1
                acts, self._buf = _text_actions(buf[i:j], j == n)
                out += acts
                i = j
        return out

    def _feed_paste(self, buf: bytes, i: int, out: list[Action]) -> int:
        n = len(buf)
        self._paste_bytes += n - i
        end = buf.find(_PASTE_OFF, i)
        if end >= 0:
            out += _text_actions(buf[i:end], False)[0]
            out.append(("raw", _PASTE_OFF))
            self._paste = False
            return end + len(_PASTE_OFF)
        hold = 0  # a tail that may be the start of the end marker waits for the next call
        for k in range(min(len(_PASTE_OFF) - 1, n - i), 0, -1):
            if buf[n - k:] == _PASTE_OFF[:k]:
                hold = k
                break
        acts, pend = _text_actions(buf[i:n - hold], hold == 0)
        out += acts
        self._buf = pend + buf[n - hold:]
        if self._paste_bytes > _PASTE_MAX:
            self._paste = False  # no end marker in sight: resume key translation
        return n

    def _escape(self, buf: bytes, i: int, out: list[Action]) -> int:
        """Handle the escape at buf[i]; returns bytes consumed, 0 when the sequence is incomplete."""
        n = len(buf)
        if i + 1 >= n:
            return 0
        nx = buf[i + 1]
        if nx == 0x5b:  # CSI
            k = i + 2
            while k < n and 0x30 <= buf[k] <= 0x3f:
                k += 1
            while k < n and 0x20 <= buf[k] <= 0x2f:
                k += 1
            if k >= n:
                return self._incomplete(buf, i, out)
            if not 0x40 <= buf[k] <= 0x7e:  # malformed: forward what we have, resume at the culprit
                out += _raw(buf[i:k])
                return k - i
            end = k + 1
            if buf[i:end] == b"\x1b[M":  # X10 mouse report: three more bytes
                end += 3
                if end > n:
                    return self._incomplete(buf, i, out)
            seq = buf[i:end]
            if seq == _PASTE_ON:
                self._paste, self._paste_bytes, self._paste_at = True, 0, self._now()
                out.append(("raw", seq))
            elif seq in _CSI_KEYS:
                out.append(("keys", [_CSI_KEYS[seq]]))
            else:
                out += _raw(seq)
            return end - i
        if nx == 0x4f:  # SS3
            if i + 2 >= n:
                return self._incomplete(buf, i, out)
            seq = buf[i:i + 3]
            out.append(("keys", [_SS3_KEYS[seq]]) if seq in _SS3_KEYS else ("raw", seq))
            return 3
        if nx == 0x1b:
            out.append(("keys", ["esc"]))
            return 1
        if 0x61 <= nx <= 0x7a:
            out.append(("keys", ["alt+" + chr(nx)]))
            return 2
        if nx < 0x80:
            out.append(("raw", buf[i:i + 2]))
            return 2
        out.append(("keys", ["esc"]))
        return 1

    @staticmethod
    def _incomplete(buf: bytes, i: int, out: list[Action]) -> int:
        if len(buf) - i < _MAX_SEQ:
            return 0
        out += _raw(buf[i:])  # not a key: forward it rather than hold input back forever
        return len(buf) - i

    def flush(self) -> list[Action]:
        """Emit whatever is held: a lone escape is the Esc key (the relay calls this after 50 ms of
        silence); any other partial sequence goes raw."""
        self._paste_expired()
        buf, self._buf = self._buf, b""
        if not buf:
            return []
        if self._paste:
            if _PASTE_OFF.startswith(buf):  # a possible end marker keeps waiting for the rest
                self._buf = buf
                return []
            return _text_actions(buf, False)[0]
        if buf == b"\x1b":
            return [("keys", ["esc"])]
        return _raw(buf)


def translate_input(data: bytes) -> list[Action]:
    t = InputTranslator()
    return t.feed(data) + t.flush()


def apply(client, pane_id: str, actions: list[Action]) -> int:
    """Perform actions in order; consecutive key actions go out in one send_keys call. Raw bytes
    that are not valid UTF-8 cannot ride in the JSON API and are dropped; returns how many bytes
    were dropped so the relay can log it."""
    dropped = 0
    keys: list[str] = []

    def flush_keys():
        for i in range(0, len(keys), MAX_KEYS):
            client.send_keys(pane_id, keys[i:i + MAX_KEYS])
        keys.clear()

    for kind, value in actions:
        if kind == "keys":
            keys.extend(value)
            continue
        flush_keys()
        if kind == "text":
            client.send_text(pane_id, value)
        elif kind == "raw":
            try:
                value.decode("utf-8")
            except UnicodeDecodeError:
                dropped += len(value)
                continue
            client.send_input(pane_id, value)
    flush_keys()
    return dropped
