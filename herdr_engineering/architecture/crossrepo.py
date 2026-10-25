"""Cross-repository dependency resolution between published snapshots.

An import that stays outside its own repository becomes a cross-repo edge only with evidence:
* ``mapping``: the operator explicitly said "package P of importer R is provided by repository Q"
  -> ``inferred: false``.
* ``name-match``: the dotted import name resolves to a module of exactly one other snapshot
  -> ``inferred: true`` (a name is not proof: two projects can ship the same package name).
Standard-library names are never candidates. A name provided by several snapshots is ``ambiguous`` and
produces no edge. Everything is bounded by snapshot count, edge count and a time budget.
"""
from __future__ import annotations

import sys
import time

MAX_SNAPSHOTS = 8
MAX_EDGES = 5000
TIME_BUDGET_S = 5.0
STDLIB = frozenset(sys.stdlib_module_names)


def _roots(loaded) -> dict[str, list[str]]:
    """top-level python package/module name -> module node ids of that snapshot."""
    out: dict[str, list[str]] = {}
    for nid, n in loaded.nodes.items():
        if n["kind"] == "module" and n["extractor"].startswith("python"):
            out.setdefault(n["qualified_name"].split(".")[0], []).append(nid)
    return out


def _longest(loaded, name: str):
    parts = name.split(".")
    while parts:
        nid = f"module:{'.'.join(parts)}"
        if nid in loaded.nodes:
            return nid
        parts.pop()
    return None


def resolve(snapshots: list[tuple[dict, object]], mappings: list[dict] | None = None) -> dict:
    """``snapshots``: [(meta, loaded graph)]; at most one snapshot per repository."""
    started = time.monotonic()
    if len(snapshots) > MAX_SNAPSHOTS:
        raise ValueError(f"at most {MAX_SNAPSHOTS} snapshots can be resolved together")
    repos = [m["repository_id"] for m, _ in snapshots]
    if len(set(repos)) != len(repos):
        raise ValueError("pass at most one snapshot per repository")
    by_repo = {m["repository_id"]: (m, g) for m, g in snapshots}
    roots = {rid: _roots(g) for rid, (_, g) in by_repo.items()}
    maps = {}
    for mp in mappings or []:
        if not all(isinstance(mp.get(k), str) for k in ("importer", "package", "provider")):
            raise ValueError("a mapping needs importer, package and provider")
        if mp["importer"] not in by_repo or mp["provider"] not in by_repo:
            raise ValueError("a mapping names a repository that is not among the snapshots")
        if mp["package"] in STDLIB:
            raise ValueError("a mapping cannot claim a standard-library package")
        maps[(mp["importer"], mp["package"])] = mp["provider"]
    edges, ambiguous, no_provider = [], [], {}
    incomplete = False
    for rid, (meta, g) in sorted(by_repo.items()):
        for nid, n in sorted(g.nodes.items()):
            for imp in n["metadata"].get("external_imports", ()):
                root = imp["name"].split(".")[0]
                if root in STDLIB:
                    continue
                if len(edges) >= MAX_EDGES or time.monotonic() - started > TIME_BUDGET_S:
                    incomplete = True
                    break
                provider = maps.get((rid, root))
                resolution = "mapping"
                if provider is None:
                    cands = sorted(p for p in by_repo if p != rid and root in roots[p])
                    if len(cands) > 1:
                        ambiguous.append({"snapshot_id": meta["snapshot_id"], "from_id": nid, "name": imp["name"],
                                          "line": imp["line"], "candidates": cands})
                        continue
                    if not cands:
                        no_provider[root] = no_provider.get(root, 0) + 1
                        continue
                    provider, resolution = cands[0], "name-match"
                pm, pg = by_repo[provider]
                target = _longest(pg, imp["name"])
                if target is None:
                    no_provider[root] = no_provider.get(root, 0) + 1
                    continue
                edges.append({"kind": "depends_on", "from": {"snapshot_id": meta["snapshot_id"], "repository_id": rid, "node_id": nid},
                              "to": {"snapshot_id": pm["snapshot_id"], "repository_id": provider, "node_id": target},
                              "source_span": {"path": n["relative_path"], "line_start": imp["line"]},
                              "evidence_ref": f"{n['relative_path']}#L{imp['line']}", "extractor": "python-ast-import/1",
                              "resolution": resolution, "inferred": resolution != "mapping",
                              "confidence": 1.0 if resolution == "mapping" else 0.6})
            else:
                continue
            break
    edges.sort(key=lambda e: (e["from"]["repository_id"], e["from"]["node_id"], e["to"]["node_id"], e["evidence_ref"]))
    return {"edges": edges[:MAX_EDGES],
            "verified": [e for e in edges if not e["inferred"]][:MAX_EDGES],
            "inferred": [e for e in edges if e["inferred"]][:MAX_EDGES],
            "ambiguous": ambiguous[:200],
            "unresolved_external_roots": sorted(no_provider.items(), key=lambda kv: (-kv[1], kv[0]))[:50],
            "limits": {"max_snapshots": MAX_SNAPSHOTS, "max_edges": MAX_EDGES, "time_budget_seconds": TIME_BUDGET_S},
            "incomplete": incomplete,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
