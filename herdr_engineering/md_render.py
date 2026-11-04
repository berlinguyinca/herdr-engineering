"""Render a Markdown report as styled terminal text, with no dependencies.

The ``/summary`` report is read in a Herdr pane, where raw ``less`` showed the Markdown source (``#``, ``**``,
backticks). This turns the subset those reports use into ANSI text that wraps to the pane width:
headings with rules, bold/italic/inline code, bullet and numbered lists (nested), fenced code, block quotes,
horizontal rules and pipe tables. Anything it does not recognise is printed as a plain paragraph, so a report is
never lost to a rendering gap.
"""
from __future__ import annotations

import re
import unicodedata

RESET = "\x1b[0m"
_BOLD, _DIM, _ITALIC, _UNDERLINE = "1", "2", "3", "4"
_CODE_FG, _CODE_BG = "38;5;222", "48;5;236"
_H = {1: ("1;38;5;81", "═"), 2: ("1;38;5;75", "─"), 3: ("1;38;5;221", ""), 4: ("1;38;5;250", "")}
_ACCENT = "38;5;75"
_MUTED = "38;5;244"
_BG_CODE_BLOCK = "48;5;235"

_INLINE = re.compile(
    r"(?P<code>`+)(?P<codetext>.+?)(?P=code)"
    r"|\[(?P<label>[^\]]+)\]\((?P<url>[^)\s]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|(?<![\w*])\*(?P<ital>[^*\s][^*]*?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<ital2>[^_\s][^_]*?)_(?![\w_])")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+-]*)\s*$")
_BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _sgr(*codes):
    return "\x1b[" + ";".join(c for c in codes if c) + "m"


def visible_width(text: str) -> int:
    """Display columns of ``text`` (ANSI escapes are free, wide and combining characters counted)."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    width = 0
    for ch in plain:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _inline_tokens(text, base=()):
    """Split inline Markdown into ``(text, style codes)`` pieces."""
    tokens, pos = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            tokens.append((text[pos:m.start()], tuple(base)))
        if m.group("code"):
            tokens.append((m.group("codetext").strip() or " ", (_CODE_FG, _CODE_BG)))
        elif m.group("label"):
            tokens.extend(_inline_tokens(m.group("label"), (*base, _UNDERLINE, _ACCENT)))
            tokens.append((f" ({m.group('url')})", (_MUTED,)))
        elif m.group("bold") or m.group("bold2"):
            tokens.extend(_inline_tokens(m.group("bold") or m.group("bold2"), (*base, _BOLD)))
        else:
            tokens.extend(_inline_tokens(m.group("ital") or m.group("ital2"), (*base, _ITALIC)))
        pos = m.end()
    if pos < len(text):
        tokens.append((text[pos:], tuple(base)))
    return tokens


def _styled(text, style):
    return f"{_sgr(*style)}{text}{RESET}" if style and text else text


def _words(tokens):
    """``(styled word, plain width, glued_to_previous)`` for every word, keeping inline code intact."""
    out = []
    for text, style in tokens:
        if style and _CODE_BG in style:
            out.append((_styled(f" {text} ", style), visible_width(text) + 2, not text.startswith(" ") and bool(out)))
            continue
        for i, piece in enumerate(re.split(r"(\s+)", text)):
            if not piece:
                continue
            if piece.isspace():
                out.append(("", 0, False))                 # a break marker
                continue
            out.append((_styled(piece, style), visible_width(piece), i == 0 and bool(out) and out[-1][0] != ""))
    return out


def wrap_inline(text, width, first="", rest="", base=()):
    """Greedy-wrap ``text`` (inline Markdown) to ``width`` columns; ``first``/``rest`` are the line prefixes."""
    lines, line, used = [], first, visible_width(first)
    start = used
    pending_space = False
    for word, w, glued in _words(_inline_tokens(text, base)):
        if word == "":
            pending_space = True
            continue
        gap = 1 if (pending_space and used > start) else 0
        if used > start and used + gap + w > width and not (glued and not pending_space):
            lines.append(line)
            line, used = rest, visible_width(rest)
            start, gap = used, 0
        line += (" " * gap) + word
        used += gap + w
        pending_space = False
    lines.append(line)
    return lines


def _table(rows, width, color):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header, body = cells[0], [r for r in cells[2:]]
    ncol = len(header)
    body = [(r + [""] * ncol)[:ncol] for r in body]
    plain = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", "".join(_styled(t, st) for t, st in _inline_tokens(s)))   # noqa: E731
    widths = [max(visible_width(plain(row[i])) for row in [header, *body]) for i in range(ncol)]
    budget = max(width - (3 * ncol + 1), ncol * 6)
    while sum(widths) > budget:
        widths[widths.index(max(widths))] -= 1

    def cell_lines(text, w, bold=False):
        return wrap_inline(text, w, base=(_BOLD,) if bold else ())

    def fmt(row, bold=False):
        wrapped = [cell_lines(c, widths[i], bold) for i, c in enumerate(row)]
        height = max(len(c) for c in wrapped)
        for c in wrapped:
            c.extend([""] * (height - len(c)))
        return ["│ " + " │ ".join(c[k] + " " * (widths[i] - visible_width(c[k])) for i, c in enumerate(wrapped)) + " │"
                for k in range(height)]

    def rule(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    out = [rule("┌", "┬", "┐"), *fmt(header, True), rule("├", "┼", "┤")]
    for r in body:
        out.extend(fmt(r))
    out.append(rule("└", "┴", "┘"))
    return [_styled(line, (_MUTED,)) if line.startswith(("┌", "├", "└")) else line for line in out]


def render_markdown(text, width=100, color=True):
    """Return ``text`` rendered for a terminal of ``width`` columns (no trailing newline)."""
    width = max(width, 30)
    lines = text.replace("\r\n", "\n").split("\n")
    out, i = [], 0

    def blank():
        if out and out[-1] != "":
            out.append("")

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            marker, i, code = fence.group(1)[0] * 3, i + 1, []
            while i < len(lines) and not lines[i].lstrip().startswith(marker):
                code.append(lines[i])
                i += 1
            i += 1
            blank()
            for c in code or [""]:
                chunk = c.expandtabs(4) or " "
                for k in range(0, len(chunk), width - 4) or [0]:
                    piece = chunk[k:k + width - 4]
                    out.append(_sgr(_CODE_FG, _BG_CODE_BLOCK) + "  " + piece.ljust(width - 4) + "  " + RESET)
            out.append("")
            continue
        if not line.strip():
            blank()
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            style, rule_char = _H[min(level, 4)]
            blank()
            out.extend(wrap_inline(heading.group(2), width, base=(style,)))
            if rule_char:
                out.append(_styled(rule_char * min(width, max(visible_width(heading.group(2)), 12)), (_MUTED,)))
            i += 1
            continue
        if _RULE.match(line):
            blank()
            out.extend([_styled("─" * width, (_MUTED,)), ""])
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            rows = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blank()
            out.extend(_table(rows, width, color))
            out.append("")
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip()[1:].lstrip())
                i += 1
            blank()
            for q in wrap_inline(" ".join(quote), width - 2, base=(_ITALIC, _MUTED)):
                out.append(_styled("▎ ", (_ACCENT,)) + q)
            out.append("")
            continue
        item = _BULLET.match(line) or _NUMBERED.match(line)
        if item:
            indent = len(item.group(1).expandtabs(4))
            numbered = item.group(2).isdigit()
            marker = (item.group(2) + ".") if numbered else ("•" if indent < 2 else "◦" if indent < 4 else "▪")
            pad = " " * (indent + (1 if indent else 0))
            lead = pad + _styled(marker, (_ACCENT, _BOLD)) + " "
            hang = " " * visible_width(lead)
            body = item.group(3)
            i += 1
            while i < len(lines) and lines[i].strip() and not (
                    _BULLET.match(lines[i]) or _NUMBERED.match(lines[i]) or _HEADING.match(lines[i])
                    or _FENCE.match(lines[i]) or lines[i].lstrip().startswith(("|", ">"))):
                body += " " + lines[i].strip()
                i += 1
            out.extend(wrap_inline(body, width, lead, hang))
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not (
                _BULLET.match(lines[i]) or _NUMBERED.match(lines[i]) or _HEADING.match(lines[i])
                or _FENCE.match(lines[i]) or _RULE.match(lines[i]) or lines[i].lstrip().startswith(("|", ">"))):
            para.append(lines[i].strip())
            i += 1
        # Reports are line-oriented (a header block is one fact per line), so source line breaks are kept.
        for p in para:
            out.extend(wrap_inline(p.rstrip("\\"), width))
    while out and out[-1] == "":
        out.pop()
    rendered = "\n".join(out)
    return rendered if color else re.sub(r"\x1b\[[0-9;]*m", "", rendered)
