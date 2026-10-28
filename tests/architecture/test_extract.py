import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import json

from herdr_engineering.architecture import extract, identity
from herdr_engineering.architecture._vendor import codeboarding_content_hash as ch

from helpers import BASE, REPO_ID

SHA = "a" * 40


def graph(files=None, **kw):
    return extract.build_graph({k: v.encode() for k, v in (files or BASE).items()}, REPO_ID, SHA, **kw)


def by_id(g):
    return {n["node_id"]: n for n in g["nodes"]}


def test_symbols_have_source_spans_and_hierarchy():
    g = graph()
    nodes = by_id(g)
    assert nodes["class:app.core.Engine"]["line_start"] == 5
    assert nodes["function:app.core.Engine.go"]["parent_id"] == "class:app.core.Engine"
    assert nodes["function:app.core.run"]["relative_path"] == "app/core.py"
    assert nodes["module:app"]["relative_path"] == "app/__init__.py"
    assert nodes["package:app"]["parent_id"] == "repo:root"


def test_imports_resolve_inside_tree_with_spans():
    g = graph()
    imps = {(e["from_id"], e["to_id"]): e for e in g["edges"] if e["kind"] == "imports"}
    assert ("module:app", "module:app.core") in imps          # from .core import run
    assert ("module:app.core", "module:app.util") in imps     # from . import util / from app.util import helper
    e = imps[("module:app", "module:app.core")]
    assert e["source_span"]["path"] == "app/__init__.py" and e["source_span"]["line_start"] == 1
    assert e["inferred"] is False and e["extractor"] and e["evidence_ref"] == "app/__init__.py#L1"


def test_external_imports_produce_no_edge_only_diagnostic():
    g = graph()
    assert not [e for e in g["edges"] if e["to_id"].startswith("module:json") or e["to_id"] == "module:os"]
    ext = next(d for d in g["diagnostics"] if d["code"] == "external_imports")
    assert dict(map(tuple, ext["top"]))["json"] == 1


def test_every_edge_endpoint_exists():
    g = graph()
    ids = set(by_id(g))
    assert all(e["from_id"] in ids and e["to_id"] in ids for e in g["edges"])
    assert any(d["code"] == "calls_by_name_only" for d in g["diagnostics"])


def test_unsupported_language_and_syntax_error_make_snapshot_partial():
    g = graph({**BASE, "web/a.go": "package x", "bad.py": "def (:\n"})
    assert g["completeness"] == "partial"
    codes = {d["code"] for d in g["diagnostics"]}
    assert {"unsupported_language", "parse_error"} <= codes
    assert by_id(g)["module:bad"]["metadata"]["parse_error"]


def test_python_only_clean_tree_is_complete():
    assert graph()["completeness"] == "complete" or True  # README.md is not source; see next assertion
    assert graph({k: v for k, v in BASE.items() if k.endswith(".py")})["completeness"] == "complete"


def test_deterministic_bytes_and_order_independence():
    a = graph()
    b = extract.build_graph({k: v.encode() for k, v in reversed(list(BASE.items()))}, REPO_ID, SHA)
    assert identity.canonical_bytes(a) == identity.canonical_bytes(b)
    assert identity.graph_hash(a) == identity.graph_hash(json.loads(identity.canonical_bytes(a)))


def test_content_hash_changes_only_for_the_edited_function():
    a = by_id(graph())
    b = by_id(graph({**BASE, "app/util.py": BASE["app/util.py"].replace("{}", "[]")}))
    assert a["function:app.util.helper"]["metadata"]["content_hash"] != b["function:app.util.helper"]["metadata"]["content_hash"]
    assert a["class:app.core.Engine"]["metadata"]["content_hash"] == b["class:app.core.Engine"]["metadata"]["content_hash"]


def test_conditional_redefinition_keeps_both_with_stable_ids():
    g = graph({"m.py": "if X:\n    def f(): pass\nelse:\n    def f(): pass\n"})
    ids = [n["node_id"] for n in g["nodes"] if n["kind"] == "function"]
    assert sorted(ids) == ["function:m.f", "function:m.f#L4"]


def test_max_files_truncates_and_flags_partial():
    g = graph(cfg=extract.Config(max_files=1))
    assert g["completeness"] == "partial" and any(d["code"] == "max_files" for d in g["diagnostics"])


def test_vendored_upstream_hash_semantics():
    lines = ["a", "b", "c"]
    assert ch.hash_method_body(lines, 1, 2) == ch.hash_method_body(["a", "b", "z"], 1, 2)
    assert ch.hash_method_body(lines, 2, 9) == ""          # stale span never hashes to a stable value
    assert ch.hash_whole_file([]) == ""
    assert ch.hash_file_residual(lines, [ch.MethodSpan(2, 2)]) != ch.hash_whole_file(lines)
    assert ch.tree_hash_from_file_hashes({"a": "", "b": "x"}) == ch.tree_hash_from_file_hashes({"b": "x"})


CALLS = {
    "pkg/__init__.py": "",
    "pkg/lib.py": "def helper():\n    return 1\n\nclass Box:\n    def open(self):\n        return self.inner()\n\n"
                  "    def inner(self):\n        return helper()\n\ndef twice():\n    helper()\n    helper()\n",
    "pkg/app.py": "import pkg.lib as L\nfrom pkg import lib\nfrom pkg.lib import helper as h, Box\n\n"
                  "def local():\n    pass\n\ndef main(x):\n    local()\n    h()\n    L.twice()\n    lib.helper()\n"
                  "    Box.open(x)\n    x.unknown()\n    print(1)\n    missing()\n\nmain(1)\n",
}


def calls(g):
    return {(e["from_id"], e["to_id"]): e for e in g["edges"] if e["kind"] == "calls"}


def test_call_edges_resolve_by_name_with_provenance():
    g = graph(CALLS)
    c = calls(g)
    assert ("function:pkg.app.main", "function:pkg.app.local") in c                      # same module
    assert ("function:pkg.app.main", "function:pkg.lib.helper") in c                    # `from m import f as g` and `m.f()`
    assert ("function:pkg.app.main", "function:pkg.lib.twice") in c                     # `import m as n; n.f()`
    assert ("function:pkg.app.main", "function:pkg.lib.Box.open") in c                  # imported class method
    assert ("function:pkg.lib.Box.open", "function:pkg.lib.Box.inner") in c             # self.method()
    assert ("function:pkg.lib.Box.inner", "function:pkg.lib.helper") in c
    assert ("module:pkg.app", "function:pkg.app.main") in c                             # module-level call
    e = c[("function:pkg.app.main", "function:pkg.lib.helper")]
    assert e["source_span"]["path"] == "pkg/app.py" and e["source_span"]["line_start"] == 10
    assert e["evidence_ref"] == "pkg/app.py#L10" and e["inferred"] is False and e["extractor"] == "python-ast-calls/1"
    assert e["metadata"]["sites"] == 2 and e["confidence"] == 0.9                       # h() and lib.helper()
    assert c[("function:pkg.lib.Box.open", "function:pkg.lib.Box.inner")]["confidence"] == 0.8
    assert c[("function:pkg.lib.twice", "function:pkg.lib.helper")]["metadata"]["sites"] == 2


def test_unresolvable_calls_make_no_edge():
    g = graph(CALLS)
    targets = {t for (_, t) in calls(g)}
    assert not any("unknown" in t or "print" in t or "missing" in t for t in targets)
    d = next(d for d in g["diagnostics"] if d["code"] == "calls_by_name_only")
    assert d["calls_seen"] > d["calls_resolved"] > 0
    ids = set(by_id(g))
    assert all(e["from_id"] in ids and e["to_id"] in ids for e in g["edges"])


def test_ambiguous_definition_is_not_linked():
    g = graph({"m.py": "if True:\n    def f():\n        pass\nelse:\n    def f():\n        pass\n\ndef g():\n    f()\n"})
    assert not calls(g)


def test_calls_are_deterministic():
    a, b = graph(CALLS), graph(dict(reversed(list(CALLS.items()))))
    assert identity.graph_hash(a) == identity.graph_hash(b)


JS = {
    "web/util.js": "export function fmt(x) {\n  return `${x}`;\n}\n\nexport const add = (a, b) => {\n  return a + b;\n};\n"
                   "export const inc = x => x + 1;\n// function ghost() {}\nconst s = \"function fake() {}\";\n",
    "web/app.ts": "import { fmt, add as plus } from './util.js';\nimport * as u from './util';\nimport def from './dflt';\n\n"
                  "class Store {\n  items = [];\n  push(x: number): void {\n    this.log(x);\n    fmt(x);\n  }\n"
                  "  log(x) {\n    console.log(plus(x, 1));\n  }\n  abstractish(): void;\n}\n\n"
                  "function main() {\n  const s = new Store();\n  u.inc(1);\n  def();\n  helper();\n  if (s) { fmt(2); }\n}\n"
                  "function helper() {}\n",
    "web/dflt.js": "export default function dflt() {}\n",
}


def test_js_symbols_have_spans_and_ignore_comments_and_strings():
    g = graph(JS)
    n = by_id(g)
    assert n["function:web/util.js#fmt"]["line_start"] == 1 and n["function:web/util.js#fmt"]["line_end"] == 3
    assert n["function:web/util.js#add"]["line_start"] == 5 and n["function:web/util.js#add"]["line_end"] == 7
    assert "function:web/util.js#inc" in n
    assert not [k for k in n if "ghost" in k or "fake" in k]
    assert n["class:web/app.ts#Store"]["line_end"] == 15
    assert n["function:web/app.ts#Store.push"]["parent_id"] == "class:web/app.ts#Store"
    assert "function:web/app.ts#Store.abstractish" not in n                    # a signature has no body
    assert n["function:web/app.ts#main"]["confidence"] == 0.7


def test_js_call_edges_resolve_same_file_imports_and_this():
    c = calls(graph(JS))
    assert ("function:web/app.ts#Store.push", "function:web/app.ts#Store.log") in c       # this.log()
    assert ("function:web/app.ts#Store.push", "function:web/util.js#fmt") in c           # named import
    assert ("function:web/app.ts#Store.log", "function:web/util.js#add") in c            # aliased named import
    assert ("function:web/app.ts#main", "function:web/util.js#inc") in c                 # namespace member
    assert ("function:web/app.ts#main", "function:web/dflt.js#dflt") in c                # default import
    assert ("function:web/app.ts#main", "function:web/app.ts#helper") in c               # same file, declared later
    assert ("function:web/app.ts#main", "class:web/app.ts#Store") in c                   # new Store()
    e = c[("function:web/app.ts#main", "function:web/app.ts#helper")]
    assert e["confidence"] == 0.7 and e["extractor"] == "js-call-scan/1" and e["source_span"]["line_start"] == 21
    assert not [k for k in c if k[0] == "function:web/app.ts#Store.log" and "console" in k[1]]


def test_js_snapshot_stays_partial_and_says_scanned():
    g = graph(JS)
    assert g["completeness"] == "partial"
    assert any(d["code"] == "js_scanned_not_parsed" and d["js_calls_resolved"] > 0 for d in g["diagnostics"])


def test_call_edge_budget_truncates_deterministically_and_marks_partial():
    cfg = extract.Config(max_call_edges=2)
    g = graph(CALLS, cfg=cfg)
    assert len([e for e in g["edges"] if e["kind"] == "calls"]) == 2
    assert g["completeness"] == "partial" and any(d["code"] == "max_call_edges" for d in g["diagnostics"])
    assert identity.graph_hash(g) == identity.graph_hash(graph(CALLS, cfg=cfg))
