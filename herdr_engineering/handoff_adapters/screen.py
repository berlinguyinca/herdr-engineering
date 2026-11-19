"""Reading a pane screen as Herdr returns it (`pane read --format ansi`): colour codes in every row."""
import re

_CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*[@-~])")  # ECMA-48: parameter bytes are 0x30-0x3F, so `:` and `?` too
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
    """The dim (faint) state after one SGR sequence; never raises.

    A sequence with private parameters (`?`, `>`, `<`, `=`) is not SGR and changes nothing. Colour codes 38/48/58 swallow
    their arguments: `38;5;n` and `38;2;r;g;b` as separate parameters, `38:2::r:g:b` as one colon-joined parameter.
    """
    if params[:1] in ("?", ">", "<", "="):
        return dim
    parts = params.split(";")
    index = 0
    while index < len(parts):
        head = parts[index].split(":")[0]  # a colon-joined parameter is one code with sub-parameters
        code = int(head) if head.isascii() and head.isdigit() and len(head) <= 9 else 0 if head == "" else -1  # longer is no SGR code, and would pass Python's int-string limit
        if code in (38, 48, 58):
            if ":" not in parts[index]:
                following = parts[index + 1] if index + 1 < len(parts) else ""
                index += 1 + (4 if following == "2" else 2)
                continue
        elif code in (0, 22):
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
