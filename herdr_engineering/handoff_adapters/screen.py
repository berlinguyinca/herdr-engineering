"""Reading a pane screen as Herdr returns it (`pane read --format ansi`): colour codes in every row."""
import re

_CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*[@-~])")
_BRAILLE = range(0x2800, 0x2900)  # Codex animates the empty composer with braille cells outside the placeholder


def strip_ansi(text: str) -> str:
    return _CSI.sub("", text)


def screen_rows(screen: str) -> list[tuple[str, str]]:
    """(raw row, row without colour codes or edge spaces) for each row, trailing blank padding rows dropped."""
    rows = [(raw, strip_ansi(raw).strip()) for raw in screen.splitlines()]
    while rows and not rows[-1][1]:
        rows.pop()
    return rows


def _dim_after(dim: bool, params: str) -> bool:
    """The dim (faint) state after one SGR sequence; 38/48 colour arguments are skipped, not read as codes."""
    codes = [int(p) if p else 0 for p in params.split(";")]
    index = 0
    while index < len(codes):
        code = codes[index]
        if code in (38, 48):
            index += 1 + (4 if index + 1 < len(codes) and codes[index + 1] == 2 else 2)
            continue
        if code == 0 or code == 22:
            dim = False
        elif code == 2:
            dim = True
        index += 1
    return dim


def typed_text(row: str, marker: str) -> str | None:
    """The text typed after ``marker`` in a raw (coloured) row, or None when the row has no such marker.

    The empty prompt of Claude Code and Codex shows a faint suggestion after the marker; that is not
    typed text, and neither are Codex's braille animation cells. A row without colour codes
    (plain text) cannot tell the two apart, so any text after the marker counts as typed.
    """
    dim, text = False, []
    position = 0
    for match in _CSI.finditer(row):
        text.extend((char, dim) for char in row[position:match.start()])
        if match.group(2) == "m":
            dim = _dim_after(dim, match.group(1))
        position = match.end()
    text.extend((char, dim) for char in row[position:])
    plain = "".join(char for char, _ in text).strip()
    if not plain.startswith(marker):
        return None
    start = "".join(char for char, _ in text).index(marker) + len(marker)
    return "".join(char for char, faint in text[start:]
                   if not faint and ord(char) not in _BRAILLE).strip()
