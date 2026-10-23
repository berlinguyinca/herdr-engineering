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
from dataclasses import dataclass

from ._vendor.codeboarding_content_hash import (
    SOURCE_DECODE_ERRORS, SOURCE_ENCODING, MethodSpan, hash_file_residual, hash_method_body,
    hash_whole_file, tree_hash_from_file_hashes)
from .identity import GRAPH_SCHEMA

EXTRACTOR_IMPORTS = "python-ast-import/1"
EXTRACTOR_SYMBOLS = "python-ast-symbols/1"
NODE_KINDS = ("repo", "service", "package", "module", "class", "function", "file", "endpoint")
EDGE_KINDS = ("contains", "imports", "calls", "implements", "depends_on", "exposes", "consumes")
UNSUPPORTED_SUFFIXES = {".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
                        ".mjs": "javascript", ".go": "go", ".rs": "rust", ".java": "java",
                        ".kt": "kotlin", ".cs": "csharp", ".php": "php", ".rb": "ruby"}


@dataclass(frozen=True)
class Config:
    max_file_bytes: int = 1_000_000
    max_files: int = 20_000
    max_nodes: int = 200_000

    def as_dict(self) -> dict:
        return {"languages": ["python"], "max_file_bytes": self.max_file_bytes,
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
            nid = f"{base}#L{ls}"
        n = {"node_id": nid, "kind": kind, "qualified_name": qname, "parent_id": parent,
             "repository_id": self.repo, "relative_path": path, "line_start": ls, "line_end": le,
             "extractor": extractor, "confidence": 1.0, "metadata": meta or {}}
        self.nodes[nid] = n
        return nid

    def edge(self, kind, a, b, span, extractor, evidence, meta=None):
        key = f"{kind}|{a}|{b}|{span.get('path')}|{span.get('line_start')}|{span.get('col')}"
        self.edges.append({"edge_id": "e_" + hashlib.sha256(key.encode()).hexdigest()[:20],
                           "kind": kind, "from_id": a, "to_id": b, "source_span": span,
                           "evidence_ref": evidence, "extractor": extractor, "inferred": False,
                           "confidence": 1.0, "metadata": meta or {}})


def _span(path, node):
    return {"path": path, "line_start": node.lineno, "line_end": getattr(node, "end_lineno", node.lineno),
            "col": node.col_offset + 1}


def _symbols(b: _Builder, path: str, module_id: str, mod_q: str, tree: ast.Module, lines: list[str]):
    def visit(body, parent_id, prefix, in_class):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_cls = isinstance(st, ast.ClassDef)
                q = f"{prefix}.{st.name}" if prefix else st.name
                start = min([st.lineno] + [d.lineno for d in st.decorator_list])
                end = st.end_lineno or st.lineno
                meta = {"content_hash": hash_method_body(lines, start, end)}
                if not is_cls:
                    meta["async"] = isinstance(st, ast.AsyncFunctionDef)
                    meta["method"] = in_class
                else:
                    meta["bases"] = [ast.unparse(x)[:120] for x in st.bases]
                nid = b.node("class" if is_cls else "function", q, parent_id, path, start, end,
                             EXTRACTOR_SYMBOLS, meta)
                b.edge("contains", parent_id, nid, {"path": path, "line_start": start, "line_end": end, "col": st.col_offset + 1},
                       EXTRACTOR_SYMBOLS, f"{path}#L{start}")
                if is_cls:
                    visit(st.body, nid, q, True)
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for attr in ("body", "orelse", "finalbody"):
                    visit(getattr(st, attr, []) or [], parent_id, prefix, in_class)
                for h in getattr(st, "handlers", []) or []:
                    visit(h.body, parent_id, prefix, in_class)
    visit(tree.body, module_id, mod_q, False)


def _resolve(b_modules: set[str], name: str) -> str | None:
    """Longest existing module prefix of a dotted name (import a.b.c -> a.b if a.b.c is absent)."""
    parts = name.split(".")
    while parts:
        cand = ".".join(parts)
        if cand in b_modules:
            return cand
        parts.pop()
    return None


def _imports(b: _Builder, path: str, module_id: str, mod_q: str, tree: ast.Module, modules: set[str],
             counters: dict):
    pkg = mod_q if _is_package_init(path) else mod_q.rpartition(".")[0]
    ext = counters.setdefault("external", {})

    def add(target_q, node, how):
        tid = f"module:{target_q}"
        if tid == module_id:
            return
        b.edge("imports", module_id, tid, _span(path, node), EXTRACTOR_IMPORTS, f"{path}#L{node.lineno}",
               {"how": how})

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                tgt = _resolve(modules, al.name)
                if tgt:
                    add(tgt, node, "import")
                else:
                    ext[al.name.split(".")[0]] = ext.get(al.name.split(".")[0], 0) + 1
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = pkg.split(".") if pkg else []
                up = node.level - 1
                if up > len(base_parts):
                    counters["unresolved_relative"] = counters.get("unresolved_relative", 0) + 1
                    continue
                base_parts = base_parts[:len(base_parts) - up] if up else base_parts
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            tgt = _resolve(modules, base) if base else None
            subs = [f"{base}.{al.name}" if base else al.name for al in node.names]
            hits = [sub for sub in subs if sub in modules and sub != tgt]
            # `from pkg import a, b` where every name is a submodule depends on those
            # submodules, not on pkg's __init__ (which would only create false cycles).
            if tgt and not (hits and len(hits) == len(subs)):
                add(tgt, node, "from")
            elif not tgt and base:
                root = base.split(".")[0]
                ext[root] = ext.get(root, 0) + 1
            for sub in hits:
                add(sub, node, "from-submodule")


def build_graph(files: dict[str, bytes], repository_id: str, commit_sha: str,
                cfg: Config = Config(), skipped: dict | None = None) -> dict:
    """``files``: repo-relative posix path -> bytes, every file of the commit's tree."""
    b = _Builder(repository_id, cfg)
    skipped = skipped or {}
    py = sorted(p for p in files if p.endswith(".py"))
    unsupported: dict[str, int] = {}
    for p in files:
        lang = UNSUPPORTED_SUFFIXES.get("." + p.rsplit(".", 1)[-1]) if "." in p.rsplit("/", 1)[-1] else None
        if lang:
            unsupported[lang] = unsupported.get(lang, 0) + 1
    truncated = False
    if len(py) > cfg.max_files:
        py, truncated = py[:cfg.max_files], True
        b.diag.append({"severity": "warning", "code": "max_files",
                       "message": f"only the first {cfg.max_files} Python files were analysed"})
    root = b.node("repo", repository_id, None, None, None, None, EXTRACTOR_SYMBOLS, nid="repo:root")
    modules = {module_name(p): p for p in py if module_name(p)}
    module_set = set(modules)
    file_hashes: dict[str, str] = {}
    counters: dict = {}
    parse_errors = 0
    pkg_ids: dict[str, str] = {}

    def ensure_pkg(dirpath: str) -> str:
        if not dirpath:
            return root
        if dirpath in pkg_ids:
            return pkg_ids[dirpath]
        parent = ensure_pkg(dirpath.rpartition("/")[0])
        q = dirpath.replace("/", ".")
        pid = b.node("package", q, parent, dirpath, None, None, EXTRACTOR_SYMBOLS)
        b.edge("contains", parent, pid, {"path": dirpath}, EXTRACTOR_SYMBOLS, dirpath)
        pkg_ids[dirpath] = pid
        return pid

    for path in py:
        raw = files[path]
        text = raw.decode(SOURCE_ENCODING, errors=SOURCE_DECODE_ERRORS)
        lines = text.splitlines()
        file_hashes[path] = hash_whole_file(lines)
        mq = module_name(path)
        if not mq:
            continue
        parent = ensure_pkg(path.rpartition("/")[0])
        try:
            tree = ast.parse(text, filename=path)
            err = None
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            tree, err = None, f"{type(exc).__name__}: {str(exc)[:120]}"
        spans: list[MethodSpan] = []
        meta = {"content_hash": file_hashes[path], "lines": len(lines)}
        if err:
            meta["parse_error"] = err
            parse_errors += 1
            b.diag.append({"severity": "warning", "code": "parse_error", "path": path, "message": err})
        mid = b.node("module", mq, parent, path, 1, max(len(lines), 1), EXTRACTOR_SYMBOLS, meta)
        b.edge("contains", parent, mid, {"path": path, "line_start": 1}, EXTRACTOR_SYMBOLS, path)
        if tree is not None:
            before = len(b.nodes)
            _symbols(b, path, mid, mq, tree, lines)
            for n in list(b.nodes.values())[before:]:
                if n["kind"] in ("class", "function") and n["parent_id"] == mid:
                    spans.append(MethodSpan(n["line_start"], n["line_end"]))
            b.nodes[mid]["metadata"]["residual_hash"] = hash_file_residual(lines, spans)
            _imports(b, path, mid, mq, tree, module_set, counters)
        if len(b.nodes) > cfg.max_nodes:
            truncated = True
            b.diag.append({"severity": "warning", "code": "max_nodes",
                           "message": f"node limit {cfg.max_nodes} reached; graph truncated"})
            break

    for lang, n in sorted(unsupported.items()):
        b.diag.append({"severity": "info", "code": "unsupported_language", "language": lang, "files": n,
                       "message": f"{n} {lang} file(s) are not analysed (Python only)"})
    b.diag.append({"severity": "info", "code": "unsupported_edge_kind", "edge_kind": "calls",
                   "message": "call edges need a language server; none were produced"})
    for key, label in (("skipped_symlinks", "symlink"), ("skipped_submodules", "submodule"),
                       ("skipped_oversize", "oversize file")):
        if skipped.get(key):
            b.diag.append({"severity": "info", "code": key, "count": skipped[key],
                           "message": f"{skipped[key]} {label}(s) skipped"})
    ext = counters.get("external", {})
    if ext:
        b.diag.append({"severity": "info", "code": "external_imports", "count": sum(ext.values()),
                       "top": sorted(ext.items(), key=lambda kv: (-kv[1], kv[0]))[:20],
                       "message": "imports outside the analysed tree produce no edge"})
    b.diag.sort(key=lambda d: (d["code"], d.get("path", ""), d.get("language", ""), d.get("edge_kind", "")))
    nodes = sorted(b.nodes.values(), key=lambda n: n["node_id"])
    edges = sorted(b.edges, key=lambda e: (e["kind"], e["from_id"], e["to_id"], e["edge_id"]))
    complete = not (unsupported or truncated or parse_errors or skipped.get("skipped_oversize"))
    return {"schema": GRAPH_SCHEMA, "nodes": nodes, "edges": edges, "diagnostics": b.diag,
            "completeness": "complete" if complete else "partial",
            "source_tree_hash": tree_hash_from_file_hashes(file_hashes),
            "counts": {"nodes": len(nodes), "edges": len(edges), "python_files": len(py),
                       "parse_errors": parse_errors}}
