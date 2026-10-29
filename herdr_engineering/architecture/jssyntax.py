"""JavaScript / TypeScript symbols and call sites, found without a parser.

There is no JS parser in the isolated Python child, and the analysed code is never run, so this is a scanner: comments, string
literals and template literals are blanked first (same length, newlines kept), then top-level declarations and class methods are
matched and their bodies delimited by brace matching. It is deliberately modest and says so: every symbol and edge here carries
confidence 0.7, and the snapshot stays ``partial``.

Found: top-level ``function`` / ``class`` / ``const f = (...) =>`` / ``const f = function`` declarations, and class methods.
Not found: object-literal methods, nested functions, class fields holding arrows, anything inside template literals, regex
literals containing quotes (they can desynchronise the scanner for the rest of the file), re-exports, dynamic dispatch.
Calls resolve only to the same file, to a named / namespace / default import of a relative module, or to ``this.m()`` in the same
class.
"""
from __future__ import annotations

import bisect
import re

EXTRACTOR_JS_SYMBOLS = "js-symbol-scan/1"
EXTRACTOR_JS_CALLS = "js-call-scan/1"
JS_SYMBOL_CONFIDENCE = 0.7
MAX_JS_CALLS = 5000
_ID = r"[A-Za-z_$][\w$]*"
_KEYWORDS = frozenset("""if for while switch catch return typeof void delete await yield throw new function class import export
else do try finally in of instanceof super this with case default var let const async static get set constructor require""".split())

_FUNC = re.compile(rf"^[ \t]*(?:export\s+(default\s+)?)?(?:async\s+)?function\s*\*?\s*({_ID})\s*[(<]", re.M)
_CLASS = re.compile(rf"^[ \t]*(?:export\s+(default\s+)?)?(?:abstract\s+)?class\s+({_ID})", re.M)
_ARROW = re.compile(rf"^[ \t]*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*(?::[^=\n]+)?=\s*(?:async\s+)?(?:function\b|\([^)\n]*\)\s*(?::[^=\n]+)?=>|{_ID}\s*=>)", re.M)
_METHOD = re.compile(rf"^[ \t]*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*\*?\s*(#?{_ID})\s*(?:<[^>\n]*>)?\s*\(", re.M)
_IMPORT = re.compile(r"""^[ \t]*import\s+(?:type\s+)?([^;'"]+?)\s+from\s+(['"])([^'"\n]+)\2""", re.M)
_REQUIRE = re.compile(rf"""^[ \t]*(?:const|let|var)\s+(\{{[^}}]*\}}|{_ID})\s*=\s*require\s*\(\s*(['"])([^'"\n]+)\2\s*\)""", re.M)
_CALL_BARE = re.compile(rf"(?<![\w$.#])(new\s+)?({_ID})\s*\(")
_CALL_THIS = re.compile(rf"\bthis\.(#?{_ID})\s*\(")
_CALL_MEMBER = re.compile(rf"(?<![\w$.])({_ID})\.({_ID})\s*\(")


def scrub(text: str) -> str:
    """Blank comments, strings and templates; same length, newlines kept so offsets and lines still match the source."""
    out = list(text)
    i, n = 0, len(text)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            blank(i, min(j + 1, n))
            i = j + 1
        elif c == "`":
            depth, j = 0, i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if depth == 0 and text[j] == "`":
                    break
                if text[j:j + 2] == "${":
                    depth += 1
                    j += 2
                    continue
                if depth and text[j] == "}":
                    depth -= 1
                j += 1
            blank(i, min(j + 1, n))
            i = j + 1
        else:
            i += 1
    return "".join(out)


class _Text:
    def __init__(self, text: str):
        self.text = text
        self.clean = scrub(text)
        self.brace = [i for i, c in enumerate(self.clean) if c in "{}"]
        self.nl = [i for i, c in enumerate(text) if c == "\n"]
        d, self.depths = 0, []
        for i in self.brace:
            d += 1 if self.clean[i] == "{" else -1
            self.depths.append(d)

    def line(self, pos: int) -> int:
        return bisect.bisect_left(self.nl, pos) + 1

    def col(self, pos: int) -> int:
        return pos - (self.text.rfind("\n", 0, pos) + 1) + 1

    def depth(self, pos: int) -> int:
        k = bisect.bisect_left(self.brace, pos)
        return self.depths[k - 1] if k else 0

    def match_brace(self, open_pos: int) -> int | None:
        k = bisect.bisect_left(self.brace, open_pos)
        want = self.depths[k] - 1
        for j in range(k + 1, len(self.brace)):
            if self.depths[j] == want:
                return self.brace[j]
        return None

    def body_open(self, pos: int) -> int | None:
        """The '{' opening a body after a parameter list starting at/after ``pos``; None for signatures without a body."""
        c = self.clean
        p = c.find("(", pos)
        if p < 0:
            return None
        depth = 0
        for j in range(p, len(c)):
            if c[j] == "(":
                depth += 1
            elif c[j] == ")":
                depth -= 1
                if depth == 0:
                    p = j
                    break
        else:
            return None
        for j in range(p + 1, min(len(c), p + 400)):
            if c[j] == "{":
                return j
            if c[j] == ";":
                return None
        return None


def _bindings(t: _Text) -> list[dict]:
    out = []
    for m in _IMPORT.finditer(t.text):
        clause, spec = m.group(1).strip(), m.group(3)
        line = t.line(m.start())
        default = re.match(rf"({_ID})\s*(?:,|$)", clause)
        if default:
            out.append({"spec": spec, "line": line, "how": "default", "local": default.group(1)})
        ns = re.search(rf"\*\s*as\s+({_ID})", clause)
        if ns:
            out.append({"spec": spec, "line": line, "how": "ns", "local": ns.group(1)})
        named = re.search(r"\{([^}]*)\}", clause)
        if named:
            for part in named.group(1).split(","):
                bits = re.match(rf"\s*(?:type\s+)?({_ID})(?:\s+as\s+({_ID}))?\s*$", part)
                if bits:
                    out.append({"spec": spec, "line": line, "how": "named", "name": bits.group(1), "local": bits.group(2) or bits.group(1)})
    for m in _REQUIRE.finditer(t.text):
        target, spec, line = m.group(1), m.group(3), t.line(m.start())
        if target.startswith("{"):
            for part in target.strip("{} ").split(","):
                bits = re.match(rf"\s*({_ID})(?:\s*:\s*({_ID}))?\s*$", part)
                if bits:
                    out.append({"spec": spec, "line": line, "how": "named", "name": bits.group(1), "local": bits.group(2) or bits.group(1)})
        else:
            out.append({"spec": spec, "line": line, "how": "ns", "local": target})
    return out


def scan(text: str, path: str, hash_method_body, lines: list[str]) -> dict:
    """Symbols, import bindings and call sites of one file. Path-local facts only."""
    t = _Text(text)
    syms: list[dict] = []
    name_pos: set[int] = set()

    def add(kind, name, qname, parent, a, b, default=False, method=False):
        start, end = t.line(a), t.line(b)
        meta = {"content_hash": hash_method_body(lines, start, end), "language": "javascript", "method": method}
        if default:
            meta["default_export"] = True
        syms.append({"kind": kind, "name": name, "qname": qname, "parent": parent, "start": start, "end": end,
                     "col": t.col(a), "meta": meta, "a": a, "b": b})
        return len(syms) - 1

    found: list[tuple[int, str, str, bool, int]] = []     # (pos, kind, name, default, name_pos)
    for m in _FUNC.finditer(t.clean):
        found.append((m.start(), "function", m.group(2), bool(m.group(1)), m.start(2)))
    for m in _CLASS.finditer(t.clean):
        found.append((m.start(), "class", m.group(2), bool(m.group(1)), m.start(2)))
    for m in _ARROW.finditer(t.clean):
        found.append((m.start(), "arrow", m.group(1), False, m.start(1)))
    for pos, kind, name, default, npos in sorted(found):
        if t.depth(pos) != 0:
            continue
        if kind == "arrow":
            op = _arrow_body(t.clean, npos)
            kind = "function"
        else:
            op = t.body_open(npos) if kind == "function" else t.clean.find("{", npos)
        end = None
        if op is not None and op >= 0:
            end = t.match_brace(op)
        if end is None:                                    # expression-bodied arrow: ends at the first ';' (or the line)
            semi = t.clean.find(";", npos)
            nl = t.clean.find("\n", npos)
            end = min(x for x in (semi, nl, len(t.clean) - 1) if x >= 0)
        idx = add(kind, name, f"{path}#{name}", -1, pos, end, default)
        name_pos.add(npos)
        if kind == "class" and op is not None and op >= 0:
            cend = t.match_brace(op)
            if cend is None:
                continue
            body_depth = t.depth(op + 1)
            for m in _METHOD.finditer(t.clean, op + 1, cend):
                mname = m.group(1)
                if mname in _KEYWORDS or t.depth(m.start(1)) != body_depth:
                    continue
                mop = t.body_open(m.start(1))
                if mop is None or mop >= cend:
                    continue
                mend = t.match_brace(mop)
                if mend is None:
                    continue
                add("function", mname, f"{path}#{name}.{mname}", idx, m.start(1), mend, method=True)
                name_pos.add(m.start(1))
    calls = _calls(t, syms, name_pos)
    for s in syms:
        del s["a"], s["b"]
    return {"symbols": syms, "bindings": _bindings(t), "calls": calls[0], **({"calls_truncated": True} if calls[1] else {})}


def _arrow_body(clean: str, pos: int) -> int | None:
    """'{' of a block-bodied arrow/function expression assigned at ``pos``; None for an expression body."""
    m = re.compile(r"=\s*(?:async\s+)?function\b").match(clean, clean.find("=", pos))
    if m:
        return _FuncExprOpen(clean, m.end())
    a = clean.find("=>", pos)
    if a < 0:
        return None
    j = a + 2
    while j < len(clean) and clean[j] in " \t\r\n":
        j += 1
    return j if j < len(clean) and clean[j] == "{" else None


def _FuncExprOpen(clean: str, pos: int) -> int | None:
    p = clean.find("(", pos)
    depth = 0
    for j in range(p, len(clean)) if p >= 0 else ():
        depth += (clean[j] == "(") - (clean[j] == ")")
        if depth == 0:
            k = clean.find("{", j)
            semi = clean.find(";", j)
            return k if k >= 0 and (semi < 0 or k < semi) else None
    return None


def _calls(t: _Text, syms: list[dict], name_pos: set[int]) -> tuple[list[dict], bool]:
    def owner(pos: int) -> int:
        best, span = -1, None
        for i, s in enumerate(syms):
            if s["a"] <= pos <= s["b"] and (span is None or s["b"] - s["a"] < span):
                best, span = i, s["b"] - s["a"]
        return best

    out: list[dict] = []
    for rx, kind in ((_CALL_BARE, "name"), (_CALL_THIS, "this"), (_CALL_MEMBER, "member")):
        for m in rx.finditer(t.clean):
            if len(out) >= MAX_JS_CALLS:
                return _sorted(out), True
            if kind == "name":
                name, pos = m.group(2), m.start(2)
                before = t.clean[max(0, pos - 12):pos]
                if name in _KEYWORDS or pos in name_pos or re.search(r"(?:function|class)\s*\*?\s*$", before):
                    continue
                rec = {"k": "name", "n": name}
            elif kind == "this":
                pos = m.start(1)
                rec = {"k": "this", "n": m.group(1)}
            else:
                pos = m.start(1)
                if m.group(1) in _KEYWORDS or m.group(1) == "this":
                    continue
                rec = {"k": "member", "b": m.group(1), "n": m.group(2)}
            line = t.line(pos)
            out.append({**rec, "owner": owner(pos), "line": line, "end": line, "col": t.col(pos)})
    return _sorted(out), False


def _sorted(calls):
    return sorted(calls, key=lambda c: (c["line"], c["col"], c["k"], c["n"]))


def resolve_js(js_facts: list[dict], ids: dict[str, dict[int, str]], files: set[str], resolve_spec, add_edge) -> dict:
    """Resolve call sites of JS files; same-file, relative-import and this.method targets only."""
    default_of: dict[str, list[str]] = {}
    defs: dict[str, list[str]] = {}
    for f in js_facts:
        for i, s in enumerate(f.get("symbols", [])):
            defs.setdefault(s["qname"], []).append(ids[f["path"]][i])
            if s["meta"].get("default_export"):
                default_of.setdefault(f["path"], []).append(ids[f["path"]][i])

    def unique(q):
        c = defs.get(q)
        return c[0] if c and len(c) == 1 else None

    stats = {"calls_seen": 0, "calls_resolved": 0}
    for f in js_facts:
        path = f["path"]
        base_dir = path.rpartition("/")[0]
        binds: dict[str, tuple] = {}
        for b in f.get("bindings", []):
            if b["spec"].startswith("."):
                tgt = resolve_spec(files, base_dir, b["spec"])
                if tgt:
                    binds[b["local"]] = (b["how"], tgt, b.get("name"))
        seen: dict[tuple[str, str], dict] = {}
        for c in f.get("calls", []):
            stats["calls_seen"] += 1
            caller = ids[path][c["owner"]]
            target = how = None
            if c["k"] == "name":
                target, how = unique(f"{path}#{c['n']}"), "same-file"
                if not target and c["n"] in binds:
                    kind, tgt, imported = binds[c["n"]]
                    if kind == "named":
                        target, how = unique(f"{tgt}#{imported}"), "imported-name"
                    elif kind == "default" and len(default_of.get(tgt, [])) == 1:
                        target, how = default_of[tgt][0], "imported-default"
            elif c["k"] == "this" and c["owner"] >= 0:
                s = f["symbols"][c["owner"]]
                if s["meta"].get("method") and s["parent"] >= 0:
                    target, how = unique(f"{f['symbols'][s['parent']]['qname']}.{c['n']}"), "this-method"
            elif c["k"] == "member" and c["b"] in binds and binds[c["b"]][0] == "ns":
                target, how = unique(f"{binds[c['b']][1]}#{c['n']}"), "namespace-member"
            if not target or target == caller:
                continue
            stats["calls_resolved"] += 1
            key = (caller, target)
            if key in seen:
                seen[key]["sites"] += 1
            else:
                seen[key] = {"span": {"path": path, "line_start": c["line"], "line_end": c["end"], "col": c["col"]},
                             "how": how, "sites": 1}
        for (caller, target), e in seen.items():
            add_edge(caller, target, e["span"], f"{path}#L{e['span']['line_start']}", {"how": e["how"], "sites": e["sites"]})
    return stats
