import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import BASE, REPO_ID, git, write  # noqa: E402

from herdr_engineering.architecture import analyze, extract, identity  # noqa: E402

SHA = "a" * 40


def full(repo, **kw):
    return analyze.analyze_commit(str(repo), "HEAD", "h", REPO_ID, **kw)


def commit(repo, msg="c"):
    git(repo, "add", "-A"); git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def same(a, b):
    assert identity.canonical_bytes(a["graph"]) == identity.canonical_bytes(b["graph"])
    assert a["snapshot"]["graph_hash"] == b["snapshot"]["graph_hash"]
    assert a["snapshot"]["snapshot_id"] == b["snapshot"]["snapshot_id"]


def check(repo, tmp_path, mutate):
    """Warm the cache on the base commit, mutate, then incremental must equal a from-scratch full run."""
    cache = str(tmp_path / "cache")
    base = full(repo, cache_dir=cache)
    assert base["snapshot"]["reuse"]["reused"] == 0
    mutate(repo); commit(repo)
    inc = full(repo, cache_dir=cache)
    scratch = full(repo)                                      # no cache at all
    same(inc, scratch)
    return base, inc


def test_unchanged_commit_is_fully_reused_and_identical(repo, tmp_path):
    cache = str(tmp_path / "c")
    a = full(repo, cache_dir=cache); b = full(repo, cache_dir=cache)
    same(a, b)
    assert b["snapshot"]["reuse"] == {"files": 3, "reused": 3, "parsed": 0}


def test_parity_file_edit(repo, tmp_path):
    _, inc = check(repo, tmp_path, lambda r: write(r, {"app/util.py": "import json\n\ndef helper():\n    return 3\n"}))
    assert inc["snapshot"]["reuse"]["parsed"] == 1 and inc["snapshot"]["reuse"]["reused"] == 2


def test_parity_delete(repo, tmp_path):
    _, inc = check(repo, tmp_path, lambda r: git(r, "rm", "-q", "app/core.py"))
    assert "module:app.core" not in {n["node_id"] for n in inc["graph"]["nodes"]}
    assert not [e for e in inc["graph"]["edges"] if "app.core" in e["to_id"]]       # no dangling edge


def test_parity_move(repo, tmp_path):
    def mv(r):
        (r / "lib").mkdir()
        git(r, "mv", "app/util.py", "lib/util.py")
    _, inc = check(repo, tmp_path, mv)
    ids = {n["node_id"] for n in inc["graph"]["nodes"]}
    assert "module:lib.util" in ids and "module:app.util" not in ids


def test_parity_dependency_change_rewires_edges_without_reparsing_unchanged_importer(repo, tmp_path):
    # app/core.py is unchanged, but the module it imports disappears: its edge must go too.
    base, inc = check(repo, tmp_path, lambda r: git(r, "rm", "-q", "app/util.py"))
    assert inc["snapshot"]["reuse"]["parsed"] == 0
    assert any(e["to_id"] == "module:app.util" for e in base["graph"]["edges"])
    assert not any(e["to_id"] == "module:app.util" for e in inc["graph"]["edges"])


def test_parity_new_module_adds_edge_from_unchanged_file(repo, tmp_path):
    # `from . import extra` in an UNCHANGED file starts resolving once extra.py exists.
    write(repo, {"app/uses.py": "from . import extra\n"}); commit(repo)
    _, inc = check(repo, tmp_path, lambda r: write(r, {"app/extra.py": "X = 1\n"}))
    assert any(e["from_id"] == "module:app.uses" and e["to_id"] == "module:app.extra" for e in inc["graph"]["edges"])


def test_config_change_invalidates_cache(repo, tmp_path):
    cache = str(tmp_path / "c")
    full(repo, cache_dir=cache)
    other = full(repo, cache_dir=cache, max_files=19_999)
    assert other["snapshot"]["reuse"]["reused"] == 0          # different config hash -> different cache scope


def test_analyzer_or_toolchain_change_invalidates_cache(repo, tmp_path, monkeypatch):
    cache = str(tmp_path / "c")
    full(repo, cache_dir=cache)
    monkeypatch.setattr(identity, "ANALYZER_VERSION", "herdr-arch-python-ast/999")
    assert full(repo, cache_dir=cache)["snapshot"]["reuse"]["reused"] == 0


def test_corrupt_or_foreign_cache_entry_is_a_miss_not_a_crash(repo, tmp_path):
    cache = tmp_path / "c"
    a = full(repo, cache_dir=str(cache))
    for p in cache.rglob("*.json"):
        p.write_text("{not json")
    b = full(repo, cache_dir=str(cache))
    same(a, b)
    assert b["snapshot"]["reuse"]["reused"] == 0


def test_cache_is_never_written_inside_the_analysed_repo(repo, tmp_path):
    full(repo, cache_dir=str(tmp_path / "c"))
    assert git(repo, "status", "--porcelain") == ""


def test_facts_are_json_only_no_code_loaded_from_cache(repo, tmp_path):
    cache = tmp_path / "c"
    full(repo, cache_dir=str(cache))
    import json
    for p in cache.rglob("*.json"):
        assert isinstance(json.loads(p.read_text()), dict)


# ----------------------------------------------------------------------- JS / TS imports
JS = {"web/app.js": "import {a} from './lib/a.js';\nimport b from \"./b\";\nconst c = require('../shared/c.js');\nimport 'react';\n",
      "web/lib/a.js": "export const a = 1;\n", "web/b.ts": "import {a} from './lib/a.js'\n", "shared/c.js": "module.exports = {}\n",
      "web/escape.js": "import x from '../../../outside.js';\n"}


def test_js_relative_imports_resolve_with_spans_and_confidence():
    g = extract.build_graph({k: v.encode() for k, v in JS.items()}, REPO_ID, SHA)
    imps = {(e["from_id"], e["to_id"]): e for e in g["edges"] if e["kind"] == "imports"}
    assert ("module:web/app.js", "module:web/lib/a.js") in imps
    assert ("module:web/app.js", "module:web/b.ts") in imps                 # extensionless -> .ts
    assert ("module:web/app.js", "module:shared/c.js") in imps              # require('../x')
    assert ("module:web/b.ts", "module:web/lib/a.js") in imps
    e = imps[("module:web/app.js", "module:web/lib/a.js")]
    assert e["source_span"]["line_start"] == 1 and e["confidence"] == extract.JS_CONFIDENCE and e["extractor"] == extract.EXTRACTOR_JS
    assert not [e for e in g["edges"] if "outside" in e["to_id"]]            # never resolves outside the repo
    assert g["completeness"] == "partial" and any(d["code"] == "js_imports_only" for d in g["diagnostics"])
    ext = next(d for d in g["diagnostics"] if d["code"] == "external_imports")
    assert "react" in dict(map(tuple, ext["top"]))


def test_js_ts_dot_js_specifier_maps_to_ts_source():
    g = extract.build_graph({"a/x.ts": b"export const x=1", "a/y.ts": b"import {x} from './x.js'"}, REPO_ID, SHA)
    assert any(e["from_id"] == "module:a/y.ts" and e["to_id"] == "module:a/x.ts" for e in g["edges"])
