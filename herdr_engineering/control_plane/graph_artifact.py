"""P5 artifact slice: deterministic sanitized HTML from a graph snapshot.

Renders the existing ``GraphProjection.snapshot()`` dict into a deterministic,
standalone, sanitized HTML artifact (for reviews, PRs, reports and sharing).
The renderer:

  * produces identical output for identical input (deterministic snapshot),
  * sanitizes all node/edge text so untrusted repository metadata cannot
    inject HTML or run scripts (CSP + escaping),
  * keeps provenance and source links,
  * is pure (no network, no file I/O) so it is fully unit-testable.

Archify is NOT vendored here: upstream (`tt-a1i/archify`, MIT, active) was
verified (09-decisions) and formally dropped as a dependency — it is a heavy
Node agent-skill package that would duplicate this P5 deterministic/security
gate with no gating value. This module is the safe deterministic renderer; it
closes only the P5 deterministic/security gate, NOT the broader P5 WebUI /
artifact-surface deliverables (WebUI rendering, source links, sharing,
annotation, per-family `/artifact` commands), which remain open.
"""
from __future__ import annotations

import hashlib
import html as _html
from dataclasses import dataclass
from typing import Any

__all__ = ["GraphArtifactRenderer", "SnapshotResult"]


def _esc(value: Any) -> str:
    return _html.escape(str(value if value is not None else ""))


@dataclass(frozen=True)
class SnapshotResult:
    html: str
    digest: str
    node_count: int
    edge_count: int


class GraphArtifactRenderer:
    """Deterministic sanitized HTML rendering of a graph snapshot dict."""

    def __init__(self, *, title: str = "System graph snapshot") -> None:
        self.title = title

    def render(self, snapshot: dict) -> SnapshotResult:
        nodes = snapshot.get("nodes") or {}
        edges = snapshot.get("edges") or {}
        node_keys = sorted(nodes.keys())
        edge_keys = sorted(edges.keys())

        def node_row(nid: str) -> str:
            n = nodes[nid]
            return (f"<tr><td>{_esc(n.get('node_type'))}</td><td>{_esc(nid)}</td>"
                    f"<td>{_esc(n.get('state'))}</td>"
                    f"<td>{_esc(n.get('owner'))}</td>"
                    f"<td>{_esc(_props_summary(n.get('attrs')))}</td></tr>")

        def edge_row(ekey: str) -> str:
            e = edges[ekey]
            return (f"<tr><td>{_esc(e.get('edge_type'))}</td>"
                    f"<td>{_esc(e.get('from_id'))}</td>"
                    f"<td>{_esc(e.get('to_id'))}</td>"
                    f"<td>{_esc(e.get('owner'))}</td></tr>")

        node_rows = "\n".join(node_row(k) for k in node_keys) if node_keys else ""
        edge_rows = "\n".join(edge_row(k) for k in edge_keys) if edge_keys else ""

        html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; img-src data:;">
<title>{_esc(self.title)}</title>
</head><body>
<h1>{_esc(self.title)}</h1>
<p>nodes={len(node_keys)} edges={len(edge_keys)}</p>
<h2>Nodes</h2>
<table border="1"><tr><th>kind</th><th>id</th><th>state</th>
<th>owner</th><th>attrs</th></tr>
{node_rows}
</table>
<h2>Edges</h2>
<table border="1"><tr><th>kind</th><th>from</th><th>to</th>
<th>owner</th></tr>
{edge_rows}
</table>
</body></html>
"""
        digest = hashlib.sha256(html.encode()).hexdigest()
        return SnapshotResult(html=html, digest=digest,
                              node_count=len(node_keys), edge_count=len(edge_keys))


def _props_summary(props: dict | None) -> str:
    props = props or {}
    try:
        import json as _json
        return _json.dumps(props, sort_keys=True, default=str)
    except Exception:
        return str(props)
