"""Collect static dependencies of a selected HTML preview inside its task root."""
from __future__ import annotations

import base64
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ASSETS = {'.css', '.js', '.mjs', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp',
          '.avif', '.ico', '.woff', '.woff2', '.ttf', '.otf'}
CSS_URL = re.compile(r'''url\(\s*["']?([^\s"')]+)|@import\s+["']([^"']+)''', re.I)


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag in {'a', 'area', 'base'}:
            return
        for key, value in attrs:
            if value and (key in {'src', 'poster'} or (key == 'href' and tag == 'link')):
                self.urls.append(value)
            elif value and key == 'srcset':
                self.urls.extend(part.strip().split()[0] for part in value.split(',') if part.strip())


def collect(root: Path, entry: str, content: bytes, read) -> list[dict]:
    """Bound transfer to 100 static dependencies and 32 MiB including entry."""
    queue = [(entry, content)]
    visited = {entry}
    result = []
    total = len(content)
    while queue:
        name, data = queue.pop()
        text = data.decode('utf-8', errors='replace')
        urls = [first or second for first, second in CSS_URL.findall(text)]
        if Path(name).suffix.lower() in {'.html', '.htm'}:
            parser = References()
            parser.feed(text)
            urls.extend(parser.urls)
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme or parts.netloc or not parts.path or parts.path.startswith('/'):
                continue
            path = root / Path(name).parent / unquote(parts.path)
            if path.suffix.lower() not in ASSETS:
                continue
            relative = path.resolve().relative_to(root).as_posix()
            if relative in visited:
                continue
            visited.add(relative)
            # Use unresolved path in read: do not hide a symlink by resolving it first.
            asset = read(root, str(Path(name).parent / unquote(parts.path)))
            total += len(asset)
            if len(result) >= 100 or total > 32 * 1024 * 1024:
                raise ValueError('HTML dependencies exceed the bounded preview limit')
            result.append({'name': relative, 'data': base64.b64encode(asset).decode(),
                           'sha256': hashlib.sha256(asset).hexdigest()})
            if path.suffix.lower() == '.css':
                queue.append((relative, asset))
    return result
