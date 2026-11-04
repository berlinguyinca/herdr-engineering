"""A tiny full-screen viewer for Herdr side panes: draws a document, scrolls it, and reflows when the pane is resized.

``less`` cannot report where the reader is, so after a resize it could not put them back; this loop keeps the
position as a fraction of the document and asks ``render`` for a fresh layout at the new size. Standard library only.
"""
from __future__ import annotations

import os
import re
import select
import signal
import sys
import termios
import tty
from collections.abc import Callable

from .md_render import RESET, visible_width

_ALT_ON, _ALT_OFF = "\x1b[?1049h", "\x1b[?1049l"
_HIDE, _SHOW = "\x1b[?25l", "\x1b[?25h"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_KEYS = {"[A": "up", "OA": "up", "[B": "down", "OB": "down", "[5~": "pgup", "[6~": "pgdn",
         "[H": "top", "OH": "top", "[1~": "top", "[F": "bottom", "OF": "bottom", "[4~": "bottom"}
_CHARS = {"q": "quit", "Q": "quit", "\x03": "quit", "j": "down", "\r": "down", "\n": "down", "k": "up",
          " ": "pgdn", "f": "pgdn", "b": "pgup", "g": "top", "G": "bottom"}


def _parse_keys(data: bytes) -> list[str]:
    text, actions, i = data.decode("utf-8", "ignore"), [], 0
    while i < len(text):
        if text[i] == "\x1b":
            j = i + 2
            while j < len(text) and not (text[j].isalpha() or text[j] == "~") and j < i + 8:
                j += 1
            action = _KEYS.get(text[i + 1:j + 1])
            if action:
                actions.append(action)
            i = j + 1 if action or j > i + 1 else i + 1
            continue
        if text[i] in _CHARS:
            actions.append(_CHARS[text[i]])
        i += 1
    return actions


def _fit(line: str, width: int) -> str:
    """``line`` cut to ``width`` columns; a line that already fits is returned untouched, escape codes included."""
    if visible_width(line) <= width:
        return line
    out = ""
    for ch in _ANSI.sub("", line):
        if visible_width(out + ch) > width:
            break
        out += ch
    return out


def run_viewer(render: Callable[[int, int], list[str]], *, scroll: bool, title: str = "", fd_in: int = 0,
               out=None, size: Callable[[], tuple[int, int]] | None = None) -> int:
    """Show ``render(width, height)`` full screen until the reader quits; returns 0 on quit, 1 on error.

    ``height`` is the body height (the screen minus the footer line). A resize makes ``render`` run again at the new
    size, with the reader kept at the same fraction of the document. The terminal is restored on every exit path.
    """
    out = out or sys.stdout

    def terminal_size() -> tuple[int, int]:
        if size:
            return size()
        try:
            cols, rows = os.get_terminal_size(out.fileno())
            return max(cols, 1), max(rows, 2)
        except (OSError, ValueError):
            return 80, 24

    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)

    def on_resize(_signum, _frame):
        try:
            os.write(wake_w, b"w")
        except (BlockingIOError, OSError):
            pass

    old_handler = signal.signal(signal.SIGWINCH, on_resize)
    attrs = None
    try:
        attrs = termios.tcgetattr(fd_in)
        tty.setcbreak(fd_in)
        out.write(_ALT_ON + _HIDE)
        out.flush()
        lines: list[str] = []
        top, fraction, dirty, width, body = 0, 0.0, True, 80, 23
        while True:
            if dirty:
                width, rows = terminal_size()
                body = max(rows - 1, 1)
                lines = render(width, body)
                top = min(round(fraction * len(lines)), max(len(lines) - body, 0))
                _draw(out, lines, top, width, body, scroll, title)
                dirty = False
            ready, _, _ = select.select([fd_in, wake_r], [], [])
            if wake_r in ready:
                while True:
                    try:
                        if not os.read(wake_r, 4096):
                            break
                    except BlockingIOError:
                        break
                dirty = True
            if fd_in in ready:
                data = os.read(fd_in, 64)
                if not data:
                    return 0
                moved = False
                for action in _parse_keys(data):
                    if action == "quit":
                        return 0
                    if not scroll:
                        continue
                    longest = max(len(lines) - body, 0)
                    new = {"up": top - 1, "down": top + 1, "pgup": top - body, "pgdn": top + body,
                           "top": 0, "bottom": longest}[action]
                    new = min(max(new, 0), longest)
                    moved = moved or new != top
                    top = new
                if moved:
                    fraction = top / max(len(lines), 1)
                    _draw(out, lines, top, width, body, scroll, title)
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 1
    finally:
        try:
            out.write(_SHOW + _ALT_OFF)
            out.flush()
        except (OSError, ValueError):
            pass
        if attrs is not None:
            try:
                termios.tcsetattr(fd_in, termios.TCSADRAIN, attrs)
            except termios.error:
                pass
        signal.signal(signal.SIGWINCH, old_handler)
        os.close(wake_r)
        os.close(wake_w)


def _draw(out, lines, top, width, body, scroll, title):
    parts = []
    for row in range(body):
        index = top + row
        text = _fit(lines[index], width) + RESET if index < len(lines) else ""
        parts.append(f"\x1b[{row + 1};1H{text}\x1b[K")
    percent = 100 if len(lines) <= body else round(100 * min(top + body, len(lines)) / len(lines))
    help_text = f" {title + ' · ' if title else ''}q closes this pane" + (f" · space/arrows scroll · {percent}%" if scroll else "")
    parts.append(f"\x1b[{body + 1};1H\x1b[7m{_fit(help_text, width).ljust(width)}\x1b[0m")
    out.write("".join(parts))
    out.flush()
