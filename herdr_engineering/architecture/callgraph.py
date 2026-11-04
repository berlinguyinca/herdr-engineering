"""Static ``calls`` edges for Python, resolved by name only.

No type inference and no execution. A call becomes an edge only when its callee is a unique definition inside the analysed tree
that the call site's own file can name through one of:

* a top-level definition of the same module (``helper()``),
* a ``from m import f [as g]`` / ``import m [as n]`` binding (``f()``, ``n.f()``, ``Cls.method()`` via an imported class),
* ``self.m()`` / ``cls.m()`` inside a method, looked up in the same class only (bases are not followed).

Everything else (attribute calls on values, dynamic dispatch, inherited methods, shadowed names) produces no edge and is counted
in diagnostics, so no edge is invented. A call to a local variable that shadows a module-level name is the known false positive;
edges carry ``confidence`` 0.9 (name) / 0.8 (self) for that reason. One edge per (caller, callee); ``metadata.sites`` counts the
call sites and the span is the first one.
"""
from __future__ import annotations

import ast

MAX_CALLS_PER_FILE = 5000
EXTRACTOR_CALLS = "python-ast-calls/1"
CONF_NAME, CONF_SELF = 0.9, 0.8


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def collect_calls(tree: ast.AST, sym_of: dict[int, int]) -> tuple[list[dict], bool]:
    """Call sites of one file: ``owner`` is the index of the enclosing registered symbol (-1 = module level).

    Calls inside a function nested in a function belong to the outer registered function. Iterative, so a deeply nested tree cannot
    exhaust the recursion limit.
    """
    calls: list[dict] = []
    truncated = False
    stack = [(tree, -1)]
    while stack:
        node, owner = stack.pop()
        for child in ast.iter_child_nodes(node):
            stack.append((child, sym_of.get(id(child), owner)))
            if not isinstance(child, ast.Call):
                continue
            if len(calls) >= MAX_CALLS_PER_FILE:
                truncated = True
                continue
            f = child.func
            rec = {"owner": owner, "line": child.lineno, "end": child.end_lineno or child.lineno, "col": child.col_offset + 1}
            if isinstance(f, ast.Name):
                rec.update(k="name", n=f.id)
            elif isinstance(f, ast.Attribute):
                base = _dotted(f.value)
                if base is None:
                    continue
                rec.update(k="self" if base in ("self", "cls") else "attr", b=base, n=f.attr)
            else:
                continue
            calls.append(rec)
    calls.sort(key=lambda c: (c["line"], c["col"], c["owner"]))
    return calls, truncated


def resolve_calls(py_facts: list[dict], ids: dict[str, dict[int, str]], py_modules: set[str], pkg_of, add_edge) -> dict:
    """Resolve every file's call sites. ``ids[path][i]`` is the graph node id of symbol i; ``ids[path][-1]`` is the module node."""
    defs: dict[str, list[str]] = {}
    for f in py_facts:
        for i, s in enumerate(f.get("symbols", [])):
            defs.setdefault(s["qname"], []).append(ids[f["path"]][i])

    def unique(qname: str) -> str | None:
        c = defs.get(qname)
        return c[0] if c and len(c) == 1 else None

    stats = {"calls_seen": 0, "calls_resolved": 0}
    for f in py_facts:
        if not f.get("calls") or f.get("parse_error"):
            continue
        path = f["path"]
        mq = _mq(path)
        binds = _bindings(f, mq, py_modules, pkg_of(path))
        seen: dict[tuple[str, str], dict] = {}
        for c in f["calls"]:
            stats["calls_seen"] += 1
            owner = c["owner"]
            caller = ids[path][owner]
            target, how, conf = None, None, CONF_NAME
            if c["k"] == "name":
                target, how = _name_target(c["n"], mq, binds, unique)
            elif c["k"] == "self":
                if owner >= 0:
                    cls = _enclosing_class(f["symbols"], owner)
                    if cls:
                        target, how, conf = unique(f"{cls}.{c['n']}"), "self-method", CONF_SELF
            else:
                target, how = _attr_target(c["b"], c["n"], binds, unique)
            if not target or target == caller:
                continue
            stats["calls_resolved"] += 1
            key = (caller, target)
            if key in seen:
                seen[key]["sites"] += 1
            else:
                seen[key] = {"span": {"path": path, "line_start": c["line"], "line_end": c["end"], "col": c["col"]},
                             "how": how, "conf": conf, "sites": 1}
        for (caller, target), e in seen.items():
            add_edge(caller, target, e["span"], f"{path}#L{e['span']['line_start']}",
                     {"how": e["how"], "sites": e["sites"]}, e["conf"])
    return stats


def _mq(path: str) -> str:
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _enclosing_class(symbols: list[dict], owner: int) -> str | None:
    s = symbols[owner]
    p = s["parent"]
    return symbols[p]["qname"] if p >= 0 and symbols[p]["kind"] == "class" and s["kind"] == "function" else None


def _bindings(f: dict, mq: str, py_modules: set[str], pkg: str) -> dict[str, tuple]:
    """Local name -> ("mod", module) or ("sym", module, name), only for targets inside the analysed tree."""
    out: dict[str, tuple] = {}
    for imp in f.get("imports", []):
        as_names = imp.get("asnames") or [None] * len(imp["names"])
        if imp["kind"] == "import":
            for name, alias in zip(imp["names"], as_names):
                if alias and name in py_modules:
                    out[alias] = ("mod", name)
                elif not alias:
                    for i in range(1, len(name.split(".")) + 1):      # `import a.b.c` binds `a`; a.b.c.f() is matched by dotted base
                        mod = ".".join(name.split(".")[:i])
                        if mod in py_modules:
                            out.setdefault(mod, ("mod", mod))
            continue
        if imp["level"]:
            parts = pkg.split(".") if pkg else []
            up = imp["level"] - 1
            if up > len(parts):
                continue
            parts = parts[:len(parts) - up] if up else parts
            base = ".".join(parts + ([imp["module"]] if imp["module"] else []))
        else:
            base = imp["module"] or ""
        for name, alias in zip(imp["names"], as_names):
            if name == "*":
                continue
            local = alias or name
            sub = f"{base}.{name}" if base else name
            if sub in py_modules:
                out[local] = ("mod", sub)
            elif base in py_modules:
                out[local] = ("sym", base, name)
    return out


def _name_target(name: str, mq: str, binds: dict, unique):
    if t := unique(f"{mq}.{name}"):
        return t, "same-module"
    b = binds.get(name)
    if b and b[0] == "sym":
        return unique(f"{b[1]}.{b[2]}"), "imported-name"
    return None, None


def _attr_target(base: str, name: str, binds: dict, unique):
    if base in binds:
        b = binds[base]
        if b[0] == "mod":
            return unique(f"{b[1]}.{name}"), "module-attribute"
        return unique(f"{b[1]}.{b[2]}.{name}"), "imported-class-method"
    return None, None
