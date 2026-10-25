"""The public collab page's module closure is served by BOTH the service allowlist (`_PUBLIC_STATIC`) and the TLS
edge's nginx allowlist, and neither serves anything outside that closure's own files list plus the fixed extras."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

from herdr_engineering.control_plane.api import _PUBLIC_STATIC

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
NGINX = ROOT / "deploy/control-plane/collab-edge/nginx.conf"
_STATIC_IMPORT = re.compile(r"""^\s*(?:import|export)\b[^'"]*?\bfrom\s*['"](\.{1,2}/[^'"]+)['"]|^\s*import\s*['"](\.{1,2}/[^'"]+)['"]""",
                            re.M)
_URL_REF = re.compile(r"""new URL\(\s*['"](\.{1,2}/[^'"]+)['"]\s*,\s*import\.meta\.url""")   # e.g. a stylesheet
_HTML_REF = re.compile(r"""<(?:script[^>]*\bsrc|link[^>]*\bhref)=["']([^"':]+)["']""")


def closure() -> set[str]:
    """Every file collab.html loads, following static ES module imports (paths relative to packages/)."""
    todo = list(_HTML_REF.findall((PACKAGES / "collab.html").read_text()))
    seen: set[str] = set()
    while todo:
        rel = posixpath.normpath(todo.pop())
        if rel in seen:
            continue
        seen.add(rel)
        if rel.endswith(".js"):
            text = (PACKAGES / rel).read_text()
            for a, b in _STATIC_IMPORT.findall(text):
                todo.append(posixpath.join(posixpath.dirname(rel), a or b))
            todo += [posixpath.join(posixpath.dirname(rel), u) for u in _URL_REF.findall(text)]
    return seen


def edge_patterns() -> list[re.Pattern]:
    return [re.compile(m) for m in re.findall(r"location ~ (\^/\(collab-app[^ ]*\$) \{", NGINX.read_text())]


def test_the_closure_is_what_the_page_needs():
    files = closure()
    assert {"collab-app.js", "collab-view.js", "collab-cards.js", "conversation-composer.js", "conversation.css"} <= files
    assert all((PACKAGES / f).is_file() for f in files)


def test_every_module_the_page_loads_is_in_both_allowlists():
    files = closure()
    missing_service = sorted(f for f in files if "/" + f not in _PUBLIC_STATIC)
    assert missing_service == [], missing_service
    pats = edge_patterns()
    assert pats, "no static location in the collab edge"
    for pat in pats:
        missing_edge = sorted(f for f in files if not pat.fullmatch("/" + f))
        assert missing_edge == [], missing_edge
