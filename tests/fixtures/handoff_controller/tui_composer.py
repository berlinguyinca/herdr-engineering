#!/usr/bin/env python3
"""A real, tiny terminal program shaped like the Claude Code composer, for the drain tests.

Installed under the name `claude` so Herdr's own agent detection reports it. It draws a transcript, a rule, the composer
row (`❯` then a faint placeholder while empty), a closing rule and a status row; echoes typed characters into the
composer; on Enter moves the line into the transcript and appends it to the file named by FAKE_TUI_SUBMITTED.
Bracketed paste (what `herdr agent prompt` sends) is understood. FAKE_TUI_PREFILL puts text in the composer at start;
FAKE_TUI_BLOCKED starts it showing a permission dialog instead of the composer; FAKE_TUI_IGNORE_EXIT makes `/exit` a no-op.
Like the real harnesses: arguments are an initial prompt, answered in the transcript in the program's own words
(`Acknowledged handoff <id>, resuming now`, so a pane that merely echoes its launch command is told apart); a
submitted `/exit` ends the program; a submitted `/compact` shows a working screen ("esc to interrupt") for
FAKE_TUI_COMPACT_S seconds (default 1.0) and then a "Compacted" line, ignoring input meanwhile.
"""
import os
import re
import select
import sys
import time
import tty

RULE = "─" * 60
PLACEHOLDER = 'Try "fix lint errors"'
SUBMITTED = os.environ.get("FAKE_TUI_SUBMITTED", "")
transcript: list[str] = []
buffer = os.environ.get("FAKE_TUI_PREFILL", "")
blocked = bool(os.environ.get("FAKE_TUI_BLOCKED"))
busy_until = 0.0  # monotonic time at which a running /compact ends; 0 when idle


def draw() -> None:
    out = ["\x1b[2J\x1b[H"]
    if blocked:
        out.append("Do you want to proceed?\r\n❯ 1. Yes\r\n  2. No\r\n")
    elif busy_until:
        out.append("✻ Compacting conversation… (1s · esc to interrupt)\r\n")
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
    global buffer, busy_until
    if buffer and SUBMITTED:
        with open(SUBMITTED, "a", encoding="utf-8") as handle:
            handle.write(buffer + "\n")
    line, buffer = buffer, ""
    if line == "/exit" and not os.environ.get("FAKE_TUI_IGNORE_EXIT"):
        sys.stdout.write("\x1b[?2004l\x1b[2J\x1b[H")
        sys.stdout.flush()
        raise SystemExit(0)
    if line == "/compact":
        busy_until = time.monotonic() + float(os.environ.get("FAKE_TUI_COMPACT_S", "1.0"))
    elif line:
        transcript.append(line)


def main() -> None:
    global buffer, busy_until
    fd = sys.stdin.fileno()
    tty.setraw(fd)
    sys.stdout.write("\x1b[?2004h")
    transcript.extend(f"Acknowledged handoff {found}, resuming now"
                      for found in re.findall(r"handoff_[a-f0-9]{32}", " ".join(sys.argv[1:]))[:1])
    draw()
    pasting = False
    pending = ""
    while True:
        wait = max(0.0, busy_until - time.monotonic()) if busy_until else None
        if not select.select([fd], [], [], wait)[0]:
            busy_until = 0.0
            transcript.append("Compacted")
            draw()
            continue
        data = os.read(fd, 4096).decode("utf-8", "replace")
        if not data:
            return
        if busy_until:
            continue  # a working harness drops typed input
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
