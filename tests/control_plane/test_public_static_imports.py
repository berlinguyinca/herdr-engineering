"""Every module the public (collab-only) listener serves must be able to load: each relative
import of each served JS module, and each script/stylesheet an served HTML page references,
must itself be in _PUBLIC_STATIC. A missing one 404s on the public listener and the page dies
(collab-view.js importing ./motion.js was such a break)."""
import posixpath
import re
from pathlib import Path

from herdr_engineering.control_plane.api import _PUBLIC_STATIC

PACKAGES = Path(__file__).resolve().parents[2] / "packages"

# import x from './a.js' · import {a,\n b} from './a.js' · import './a.js' · export {a} from './a.js' · import('./a.js')
_IMPORT = re.compile(
    r"""(?:\bimport\s*(?:[\w*{}\s,$]*?\s*from\s*)?|\bexport\s+[\w*{}\s,$]*?from\s*|\bimport\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]""")
_HTML_REF = re.compile(r"""<(?:script|link)\b[^>]*?\b(?:src|href)=["']([^"':]+\.(?:js|css))["']""")


def _refs(name: str) -> list[str]:
    if not name.endswith((".js", ".html")):         # fonts and vendored .mjs bundles are served, not walked
        return []
    text = (PACKAGES / name).read_text(encoding="utf-8")
    if name.endswith(".js"):
        return _IMPORT.findall(text)
    return _HTML_REF.findall(text)


def _resolve(name: str, ref: str) -> str:
    if ref.startswith("/"):
        return posixpath.normpath(ref)
    return posixpath.normpath(posixpath.join("/" + posixpath.dirname(name), ref))


def unserved_imports(table=_PUBLIC_STATIC):
    missing = []
    for _url, (name, _ctype) in table.items():
        for ref in _refs(name):
            target = _resolve(name, ref)
            if target not in table:
                missing.append(f"{name} -> {target}")
    return sorted(missing)


def test_every_public_static_file_exists():
    assert [name for name, _ in _PUBLIC_STATIC.values() if not (PACKAGES / name).is_file()] == []


def test_every_import_of_every_public_module_is_served():
    assert unserved_imports() == []


def test_the_walk_catches_an_unserved_import():
    # Positive control: drop motion.js from the table and the walk must report collab-view's import.
    table = {k: v for k, v in _PUBLIC_STATIC.items() if k != "/motion.js"}
    assert "collab-view.js -> /motion.js" in unserved_imports(table)
