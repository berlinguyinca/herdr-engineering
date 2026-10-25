"""Python static extractor: files of one commit -> normalized graph.

Pure and deterministic: the output depends only on the file bytes, the
repository identity and the config. It parses with ``ast`` and never imports or
executes analysed code. Every edge is grounded in a source span. Imports that
do not resolve inside the analysed tree (stdlib, third party, other
repositories) produce no edge; they are counted in diagnostics instead, so no
edge is ever invented.

Not supported here (reported as diagnostics, snapshot is then ``partial``):
``calls`` edges (needs a language server) and every language except Python.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass

from ._vendor.codeboarding_content_hash import (
    SOURCE_DECODE_ERRORS, SOURCE_ENCODING, MethodSpan, hash_file_residual, hash_method_body,
    hash_whole_file, tree_hash_from_file_hashes)
from .identity import GRAPH_SCHEMA

EXTRACTOR_IMPORTS = "python-ast-import/1"
EXTRACTOR_SYMBOLS = "python-ast-symbols/1"
EXTRACTOR_JS = "js-import-regex/1"      # module-level imports only; regex, so confidence < 1
NODE_KINDS = ("repo", "service", "package", "module", "class", "function", "file", "endpoint")
EDGE_KINDS = ("contains", "imports", "calls", "implements", "depends_on", "exposes", "consumes")
JS_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")
UNSUPPORTED_SUFFIXES = {".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".cs": "csharp",
                        ".php": "php", ".rb": "ruby"}
JS_CONFIDENCE = 0.9
MAX_EXTERNAL_PER_MODULE = 200


@dataclass(frozen=True)
class Config:
    max_file_bytes: int = 1_000_000
    max_files: int = 20_000
    max_nodes: int = 200_000

    def as_dict(self) -> dict:
        return {"languages": ["python", "javascript-imports"], "max_file_bytes": self.max_file_bytes,
                "max_files": self.max_files, "max_nodes": self.max_nodes,
                "extractors": [EXTRACTOR_SYMBOLS, EXTRACTOR_IMPORTS]}


def module_name(path: str) -> str:
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_package_init(path: str) -> bool:
    return path.endswith("/__init__.py") or path == "__init__.py"


class _Builder:
    def __init__(self, repository_id: str, cfg: Config):
        self.repo = repository_id
        self.cfg = cfg
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.diag: list[dict] = []

    def node(self, kind, qname, parent, path, ls, le, extractor, meta=None, nid=None):
        base = nid or f"{kind}:{qname}"
        nid = base
        if nid in self.nodes:  # conditional redefinition: keep both, deterministic suffix
            nid = f"{base}#L{ls}" if ls else f"{base}#{hashlib.sha256((path or '').encode()).hexdigest()[:6]}"
        n = {"node_id": nid, "kind": kind, "qualified_name": qname, "parent_id": parent,
             "repository_id": self.repo, "relative_path": path, "line_start": ls, "line_end": le,
             "extractor": extractor, "confidence": 1.0, "metadata": meta or {}}
        self.nodes[nid] = n
        return nid

    def edge(self, kind, a, b, span, extractor, evidence, meta=None, confidence=1.0):
        key = f"{kind}|{a}|{b}|{span.get('path')}|{span.get('line_start')}|{span.get('col')}"
        self.edges.append({"edge_id": "e_" + hashlib.sha256(key.encode()).hexdigest()[:20],
                           "kind": kind, "from_id": a, "to_id": b, "source_span": span,
                           "evidence_ref": evidence, "extractor": extractor, "inferred": False,
                           "confidence": confidence, "metadata": meta or {}})


def _resolve(b_modules: set[str], name: str) -> str | None:
    """Longest existing module prefix of a dotted name (import a.b.c -> a.b if a.b.c is absent)."""
    parts = name.split(".")
    while parts:
        cand = ".".join(parts)
        if cand in b_modules:
            return cand
        parts.pop()
    return None


_JS_IMPORT = re.compile(
    r"""(?:^|[;{}\s])(?:import|export)\s+(?:type\s+)?(?:[\w*${}\s,]+?\s+from\s+)?(['"])([^'"\n]+)\1"""
    r"""|\b(?:require|import)\s*\(\s*(['"])([^'"\n]+)\3\s*\)""", re.M)


def is_js(path: str) -> bool:
    return path.endswith(JS_SUFFIXES)


def extract_file_facts(path: str, raw: bytes) -> dict:
    """Parse ONE file into path-local, JSON-serialisable facts. No cross-file knowledge."""
    text = raw.decode(SOURCE_ENCODING, errors=SOURCE_DECODE_ERRORS)
    lines = text.splitlines()
    facts = {"path": path, "lang": "py" if path.endswith(".py") else "js", "lines": len(lines),
             "content_hash": hash_whole_file(lines)}
    if facts["lang"] == "js":
        imps = []
        for m in _JS_IMPORT.finditer(text):
            spec = m.group(2) or m.group(4)
            pos = m.start(2) if m.group(2) else m.start(4)
            line = text.count("\n", 0, pos) + 1
            imps.append({"line": line, "col": pos - (text.rfind("\n", 0, pos) + 1) + 1, "spec": spec})
        facts["imports"] = imps
        return facts
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        facts["parse_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return facts
    syms: list[dict] = []

    def visit(body, parent_idx, prefix, in_class):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_cls = isinstance(st, ast.ClassDef)
                q = f"{prefix}.{st.name}" if prefix else st.name
                start = min([st.lineno] + [d.lineno for d in st.decorator_list])
                end = st.end_lineno or st.lineno
                meta = {"content_hash": hash_method_body(lines, start, end)}
                if is_cls:
                    meta["bases"] = [ast.unparse(x)[:120] for x in st.bases]
                else:
                    meta["async"] = isinstance(st, ast.AsyncFunctionDef)
                    meta["method"] = in_class
                syms.append({"kind": "class" if is_cls else "function", "name": st.name, "qname": q, "parent": parent_idx,
                             "start": start, "end": end, "col": st.col_offset + 1, "meta": meta})
                if is_cls:
                    visit(st.body, len(syms) - 1, q, True)
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for attr in ("body", "orelse", "finalbody"):
                    visit(getattr(st, attr, []) or [], parent_idx, prefix, in_class)
                for h in getattr(st, "handlers", []) or []:
                    visit(h.body, parent_idx, prefix, in_class)

    mq = module_name(path)
    visit(tree.body, -1, mq, False)
    imps = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imps.append({"line": node.lineno, "end": node.end_lineno or node.lineno, "col": node.col_offset + 1,
                         "kind": "import", "level": 0, "module": None, "names": [a.name for a in node.names]})
        elif isinstance(node, ast.ImportFrom):
            imps.append({"line": node.lineno, "end": node.end_lineno or node.lineno, "col": node.col_offset + 1,
                         "kind": "from", "level": node.level, "module": node.module, "names": [a.name for a in node.names]})
    imps.sort(key=lambda i: (i["line"], i["col"]))
    top = [s for s in syms if s["parent"] == -1]
    spans = [MethodSpan(s["start"], s["end"]) for s in top]
    facts.update(symbols=syms, imports=imps, residual_hash=hash_file_residual(lines, spans))
    return facts


def _js_resolve(files: set[str], base_dir: str, spec: str) -> str | None:
    parts = (base_dir.split("/") if base_dir else []) + spec.split("/")
    out: list[str] = []
    for p in parts:
        if p in ("", "."):
            continue
        if p == "..":
            if not out:
                return None            # escapes the repository root: never resolve
            out.pop()
        else:
            out.append(p)
    stem = "/".join(out)
    cands = [stem]
    root, ext = (stem.rsplit(".", 1) + [""])[:2] if "." in stem.rsplit("/", 1)[-1] else (stem, "")
    if ext in ("js", "mjs", "cjs", "jsx"):   # TS projects import './x.js' for x.ts
        cands += [f"{root}.ts", f"{root}.tsx", f"{root}.mts"]
    cands += [stem + e for e in JS_SUFFIXES] + [f"{stem}/index{e}" for e in JS_SUFFIXES]
    return next((c for c in cands if c in files), None)


def assemble(facts: list[dict], repository_id: str, cfg: Config = Config(), skipped: dict | None = None,
             other_files: dict[str, int] | None = None) -> dict:
    """All per-file facts -> graph. Always global, so incremental reuse of facts cannot change the result."""
    b = _Builder(repository_id, cfg)
    skipped = skipped or {}
    facts = sorted(facts, key=lambda f: f["path"])
    unsupported: dict[str, int] = dict(other_files or {})
    truncated = False
    if len(facts) > cfg.max_files:
        facts, truncated = facts[:cfg.max_files], True
        b.diag.append({"severity": "warning", "code": "max_files",
                       "message": f"only the first {cfg.max_files} source files were analysed"})
    root = b.node("repo", repository_id, None, None, None, None, EXTRACTOR_SYMBOLS, nid="repo:root")
    py_modules = {module_name(f["path"]): f["path"] for f in facts if f["lang"] == "py" and module_name(f["path"])}
    js_files = {f["path"] for f in facts if f["lang"] == "js"}
    file_hashes = {f["path"]: f["content_hash"] for f in facts}
    pkg_ids: dict[str, str] = {}
    ext_counts: dict[str, int] = {}
    parse_errors = 0
    node_cap = False

    def ensure_pkg(dirpath: str) -> str:
        if not dirpath:
            return root
        if dirpath in pkg_ids:
            return pkg_ids[dirpath]
        parent = ensure_pkg(dirpath.rpartition("/")[0])
        pid = b.node("package", dirpath.replace("/", "."), parent, dirpath, None, None, EXTRACTOR_SYMBOLS)
        b.edge("contains", parent, pid, {"path": dirpath}, EXTRACTOR_SYMBOLS, dirpath)
        pkg_ids[dirpath] = pid
        return pid

    for f in facts:
        path = f["path"]
        parent = ensure_pkg(path.rpartition("/")[0])
        if f["lang"] == "js":
            mid = b.node("module", path, parent, path, 1, max(f["lines"], 1), EXTRACTOR_JS,
                         {"content_hash": f["content_hash"], "lines": f["lines"], "language": "javascript"}, nid=f"module:{path}")
            b.nodes[mid]["confidence"] = JS_CONFIDENCE
            b.edge("contains", parent, mid, {"path": path, "line_start": 1}, EXTRACTOR_JS, path)
            continue
        mq = module_name(path)
        if not mq:
            continue
        meta = {"content_hash": f["content_hash"], "lines": f["lines"]}
        if f.get("parse_error"):
            meta["parse_error"] = f["parse_error"]
            parse_errors += 1
            b.diag.append({"severity": "warning", "code": "parse_error", "path": path, "message": f["parse_error"]})
        else:
            meta["residual_hash"] = f["residual_hash"]
        mid = b.node("module", mq, parent, path, 1, max(f["lines"], 1), EXTRACTOR_SYMBOLS, meta)
        b.edge("contains", parent, mid, {"path": path, "line_start": 1}, EXTRACTOR_SYMBOLS, path)
        idx_to_id: dict[int, str] = {-1: mid}
        for i, s in enumerate(f.get("symbols", [])):
            nid = b.node(s["kind"], s["qname"], idx_to_id[s["parent"]], path, s["start"], s["end"], EXTRACTOR_SYMBOLS,
                         dict(s["meta"]))
            idx_to_id[i] = nid
            b.edge("contains", idx_to_id[s["parent"]], nid,
                   {"path": path, "line_start": s["start"], "line_end": s["end"], "col": s["col"]},
                   EXTRACTOR_SYMBOLS, f"{path}#L{s['start']}")
        if len(b.nodes) > cfg.max_nodes:
            node_cap = True
            truncated = True
            b.diag.append({"severity": "warning", "code": "max_nodes",
                           "message": f"node limit {cfg.max_nodes} reached; graph truncated"})
            break

    ext_by_module: dict[str, list] = {}
    if not node_cap:
        for f in facts:
            path = f["path"]
            if f["lang"] == "js":
                mid = f"module:{path}"
                base_dir = path.rpartition("/")[0]
                for imp in f["imports"]:
                    spec = imp["spec"]
                    span = {"path": path, "line_start": imp["line"], "line_end": imp["line"], "col": imp["col"]}
                    if spec.startswith("."):
                        tgt = _js_resolve(js_files, base_dir, spec)
                        if tgt and tgt != path:
                            b.edge("imports", mid, f"module:{tgt}", span, EXTRACTOR_JS, f"{path}#L{imp['line']}",
                                   {"how": "js-relative"}, confidence=JS_CONFIDENCE)
                        elif not tgt:
                            ext_counts["(unresolved relative)"] = ext_counts.get("(unresolved relative)", 0) + 1
                    else:
                        root_name = spec.split("/")[0] if not spec.startswith("@") else "/".join(spec.split("/")[:2])
                        ext_counts[root_name] = ext_counts.get(root_name, 0) + 1
                continue
            mq = module_name(path)
            if not mq or f.get("parse_error"):
                continue
            mid = f"module:{mq}"
            pkg = mq if (path.endswith("/__init__.py") or path == "__init__.py") else mq.rpartition(".")[0]
            ext: list[dict] = []

            def add(target_q, imp, how):
                tid = f"module:{target_q}"
                if tid != mid:
                    b.edge("imports", mid, tid, {"path": path, "line_start": imp["line"], "line_end": imp["end"], "col": imp["col"]},
                           EXTRACTOR_IMPORTS, f"{path}#L{imp['line']}", {"how": how})

            for imp in f["imports"]:
                if imp["kind"] == "import":
                    for name in imp["names"]:
                        tgt = _resolve(set(py_modules), name)
                        if tgt:
                            add(tgt, imp, "import")
                        else:
                            ext_counts[name.split(".")[0]] = ext_counts.get(name.split(".")[0], 0) + 1
                            ext.append({"name": name, "line": imp["line"]})
                    continue
                if imp["level"]:
                    base_parts = pkg.split(".") if pkg else []
                    up = imp["level"] - 1
                    if up > len(base_parts):
                        ext_counts["(unresolved relative)"] = ext_counts.get("(unresolved relative)", 0) + 1
                        continue
                    base_parts = base_parts[:len(base_parts) - up] if up else base_parts
                    base = ".".join(base_parts + ([imp["module"]] if imp["module"] else []))
                else:
                    base = imp["module"] or ""
                tgt = _resolve(set(py_modules), base) if base else None
                subs = [f"{base}.{n}" if base else n for n in imp["names"]]
                hits = [sub for sub in subs if sub in py_modules and sub != tgt]
                if tgt and not (hits and len(hits) == len(subs)):
                    add(tgt, imp, "from")
                elif not tgt and base:
                    ext_counts[base.split(".")[0]] = ext_counts.get(base.split(".")[0], 0) + 1
                    if not imp["level"]:
                        ext.append({"name": base, "line": imp["line"]})
                for sub in hits:
                    add(sub, imp, "from-submodule")
            if ext:
                uniq = {}
                for e in ext:
                    uniq.setdefault(e["name"], e["line"])
                b.nodes[mid]["metadata"]["external_imports"] = [{"name": n, "line": l} for n, l in sorted(uniq.items())][:MAX_EXTERNAL_PER_MODULE]

    for lang, n in sorted(unsupported.items()):
        b.diag.append({"severity": "info", "code": "unsupported_language", "language": lang, "files": n,
                       "message": f"{n} {lang} file(s) are not analysed"})
    if js_files:
        b.diag.append({"severity": "info", "code": "js_imports_only", "files": len(js_files),
                       "message": "JavaScript/TypeScript: module-level imports only (regex, confidence "
                                  f"{JS_CONFIDENCE}); no symbols, no call edges"})
    b.diag.append({"severity": "info", "code": "unsupported_edge_kind", "edge_kind": "calls",
                   "message": "call edges need a language server; none were produced"})
    for key, label in (("skipped_symlinks", "symlink"), ("skipped_submodules", "submodule"),
                       ("skipped_oversize", "oversize file")):
        if skipped.get(key):
            b.diag.append({"severity": "info", "code": key, "count": skipped[key],
                           "message": f"{skipped[key]} {label}(s) skipped"})
    if ext_counts:
        b.diag.append({"severity": "info", "code": "external_imports", "count": sum(ext_counts.values()),
                       "top": sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20],
                       "message": "imports outside the analysed tree produce no edge"})
    b.diag.sort(key=lambda d: (d["code"], d.get("path", ""), d.get("language", ""), d.get("edge_kind", "")))
    nodes = sorted(b.nodes.values(), key=lambda n: n["node_id"])
    edges = sorted(b.edges, key=lambda e: (e["kind"], e["from_id"], e["to_id"], e["edge_id"]))
    complete = not (unsupported or truncated or parse_errors or skipped.get("skipped_oversize") or js_files)
    return {"schema": GRAPH_SCHEMA, "nodes": nodes, "edges": edges, "diagnostics": b.diag,
            "completeness": "complete" if complete else "partial",
            "source_tree_hash": tree_hash_from_file_hashes(file_hashes),
            "counts": {"nodes": len(nodes), "edges": len(edges), "source_files": len(facts),
                       "python_files": sum(1 for f in facts if f["lang"] == "py"), "parse_errors": parse_errors}}


def build_graph(files: dict[str, bytes], repository_id: str, commit_sha: str,
                cfg: Config = Config(), skipped: dict | None = None) -> dict:
    """``files``: repo-relative posix path -> bytes. Non-source paths only feed the unsupported-language report."""
    facts, other = [], {}
    for p in sorted(files):
        if p.endswith(".py") or is_js(p):
            facts.append(extract_file_facts(p, files[p]))
        else:
            lang = UNSUPPORTED_SUFFIXES.get("." + p.rsplit(".", 1)[-1]) if "." in p.rsplit("/", 1)[-1] else None
            if lang:
                other[lang] = other.get(lang, 0) + 1
    return assemble(facts, repository_id, cfg, skipped, other)
