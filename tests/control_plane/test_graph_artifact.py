"""P5 artifact tests: deterministic snapshot + malicious HTML/security.

Gate (07-phases-tests.md P5): deterministic snapshot and malicious
HTML/security tests.
"""
from herdr_engineering.control_plane.graph_artifact import GraphArtifactRenderer


def snapshot_with_evil(evil="<script>alert(1)</script>"):
    return {
        "nodes": {"repository:r1": {"node_type": "repository", "state": None,
                                    "owner": "k", "attrs": {"name": evil}}},
        "edges": {},
    }


class TestArtifactSlice:
    def test_deterministic_snapshot(self):
        s = snapshot_with_evil()
        r1 = GraphArtifactRenderer().render(s)
        r2 = GraphArtifactRenderer().render(s)
        assert r1.html == r2.html
        assert r1.digest == r2.digest
        assert r1.node_count == 1 and r1.edge_count == 0

    def test_malicious_html_is_escaped(self):
        evil = "<script>alert('x')</script>"
        result = GraphArtifactRenderer().render(snapshot_with_evil(evil))
        assert "<script>alert" not in result.html
        assert "&lt;script&gt;" in result.html

    def test_csp_header_present(self):
        result = GraphArtifactRenderer().render(snapshot_with_evil())
        assert "Content-Security-Policy" in result.html
        assert "default-src 'none'" in result.html

    def test_snapshot_includes_kinds_and_provenance(self):
        result = GraphArtifactRenderer().render(snapshot_with_evil())
        assert "repository" in result.html
        assert "owner" in result.html
