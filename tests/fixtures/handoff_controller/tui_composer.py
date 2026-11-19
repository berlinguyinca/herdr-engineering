#!/usr/bin/env python3
"""A real, tiny terminal program shaped like the Claude Code composer, for the drain tests.

Installed under the name `claude` so Herdr's own agent detection reports it. It draws a transcript, a rule, the composer
row (`❯` then a faint placeholder while empty), a closing rule and a status row; echoes typed characters into the
composer; on Enter moves the line into the transcript and appends it to the file named by FAKE_TUI_SUBMITTED.
Bracketed paste (what `herdr agent prompt` sends) is understood. FAKE_TUI_PREFILL puts text in the composer at start;
FAKE_TUI_BLOCKED starts it showing a permission dialog instead of the composer.
"""
import os
import sys
import tty

RULE = "─" * 60
PLACEHOLDER = 'Try "fix lint errors"'
SUBMITTED = os.environ.get("FAKE_TUI_SUBMITTED", "")
transcript: list[str] = []
buffer = os.environ.get("FAKE_TUI_PREFILL", "")
blocked = bool(os.environ.get("FAKE_TUI_BLOCKED"))


def draw() -> None:
    out = ["\x1b[2J\x1b[H"]
    if blocked:
        out.append("Do you want to proceed?\r\n❯ 1. Yes\r\n  2. No\r\n")
    else:
        out.extend(f"● {line}\r\n" for line in transcript)
        out.append(f"\x1b[0m\x1b[38;2;136;136;136m{RULE}\x1b[0m\r\n")
        shown = buffer if buffer else f"\x1b[0m\x1b[2m{PLACEHOLDER}\x1b[0m"
        out.append(f"❯ {shown}\r\n")
        out.append(f"\x1b[0m\x1b[38;2;136;136;136m{RULE}\x1b[0m\r\n")
        out.append("  \x1b[38;2;255;193;7m⏵⏵ auto mode on\x1b[0m\x1b[38;2;153;153;153m (shift+tab to cycle)\x1b[0m")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def submit() -> None:
    global buffer
    if buffer and SUBMITTED:
        with open(SUBMITTED, "a", encoding="utf-8") as handle:
            handle.write(buffer + "\n")
    if buffer:
        transcript.append(buffer)
    buffer = ""


def main() -> None:
    global buffer
    fd = sys.stdin.fileno()
    tty.setraw(fd)
    sys.stdout.write("\x1b[?2004h")
    draw()
    pasting = False
    pending = ""
    while True:
        data = os.read(fd, 4096).decode("utf-8", "replace")
        if not data:
            return
        pending += data
        while pending:
            if pending.startswith("\x1b[200~"):
                pasting, pending = True, pending[6:]
            elif pending.startswith("\x1b[201~"):
                pasting, pending = False, pending[6:]
            elif pending.startswith("\x1b"):
                pending = pending[1:]
            else:
                char, pending = pending[0], pending[1:]
                if blocked:
                    continue
                if char in "\r\n" and not pasting:
                    submit()
                elif char in "\x7f\b":
                    buffer = buffer[:-1]
                elif char >= " ":
                    buffer += char
        draw()


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.stdout.write("\x1b[?2004l")
