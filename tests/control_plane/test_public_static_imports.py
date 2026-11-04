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
    text = (PACKAGES / name).read_text(encoding="utf-8")
    if name.endswith(".js"):
        return _IMPORT.findall(text)
    if name.endswith(".html"):
        return _HTML_REF.findall(text)
    return []


def _resolve(name: str, ref: str) -> str:
    if ref.startswith("/"):
        return posixpath.normpath(ref)
    return posixpath.normpath(posixpath.join("/" + posixpath.dirname(name), ref))


def public_imports(table=_PUBLIC_STATIC):
    # Follow package files even when a dependency is absent from the API allowlist.
    pending = {name for name, _ctype in table.values()}
    visited = set()
    imports = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        for ref in _refs(name):
            target = _resolve(name, ref)
            imports.add((name, target))
            pending.add(target.lstrip("/"))
    return sorted(imports)


def unserved_imports(table=_PUBLIC_STATIC):
    return [f"{name} -> {target}" for name, target in public_imports(table) if target not in table]


def blocked_by_edge(config, table=_PUBLIC_STATIC):
    pattern = re.search(r'location ~ (\S+) \{', config).group(1)
    paths = set(table) | {target for _name, target in public_imports(table)}
    return sorted(path for path in paths
                  if f'location = {path} ' not in config and not re.fullmatch(pattern, path))


def test_every_public_static_file_exists():
    assert [name for name, _ in _PUBLIC_STATIC.values() if not (PACKAGES / name).is_file()] == []


def test_public_edge_serves_the_complete_public_asset_graph():
    config = (PACKAGES.parent / 'deploy/control-plane/collab-edge/nginx.conf').read_text()
    pattern = re.search(r'location ~ (\S+) \{', config).group(1)
    blocked = blocked_by_edge(config)
    assert blocked == [], f'Public modules blocked by edge allowlist: {blocked}'
    assert not re.fullmatch(pattern, '/app.js')
    assert not re.fullmatch(pattern, '/herdr-web-client/client.js')
    assert not re.fullmatch(pattern, '/api/v1/missions')


def test_every_import_of_every_public_module_is_served():
    assert unserved_imports() == []


def test_the_walk_catches_an_unserved_import():
    # Positive control: drop motion.js from the table and the walk must report collab-view's import.
    table = {k: v for k, v in _PUBLIC_STATIC.items() if k != "/motion.js"}
    assert "collab-view.js -> /motion.js" in unserved_imports(table)


def test_the_walk_follows_modules_missing_from_both_allowlists():
    # A missing intermediate module must not hide its own missing dependencies.
    table = {k: v for k, v in _PUBLIC_STATIC.items() if k not in {"/collab-view.js", "/patch.js"}}
    config = (PACKAGES.parent / 'deploy/control-plane/collab-edge/nginx.conf').read_text()
    config = config.replace(r'|patch\.js', '')
    assert "collab-app.js -> /collab-view.js" in unserved_imports(table)
    assert "collab-view.js -> /patch.js" in unserved_imports(table)
    assert "/patch.js" in blocked_by_edge(config, table)
