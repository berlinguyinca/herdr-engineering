# Vendored from CodeBoarding/CodeBoarding @ 647fe6a728169ac442ae24a5feeba50784ea2231
# (agents/content_hash.py). Licensed MIT; the full upstream notice follows.
#
# Modifications for Herdr: removed the working-tree walkers
# (hash_repo_source_files, compute_source_tree_hash) and the RepoIgnoreManager
# import. They follow symlinks and read the live worktree; Herdr hashes git
# blobs of a fixed commit instead. The pure hashing functions are unchanged.
#
# MIT License
#
# Copyright (c) 2025 CodeBoarding
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Content fingerprinting helpers — a leaf module with no analysis imports.

Standalone (no heavy analysis imports) so clustering, static enrichment, and the
JSON layer can import it without cycles. ``splitlines()`` folds line-ending-only
edits. Decode/encode use ``surrogateescape`` so invalid UTF-8 bytes stay distinct.
"""

import hashlib
from pathlib import Path
from typing import NamedTuple


SOURCE_ENCODING = "utf-8"
SOURCE_DECODE_ERRORS = "surrogateescape"

# Repo-relative path -> cached lines (empty list when unreadable). Shared line
# cache threaded through the hashing helpers to avoid re-reading the same file.
SourceCache = dict[str, list[str]]


class MethodRef(NamedTuple):
    """A method's identity across files: keyed by path so a qualified name that
    collides across files can't borrow the wrong file's hash/span."""

    file_path: str
    qualified_name: str


class MethodSpan(NamedTuple):
    """A method's 1-based inclusive line range in its file."""

    start_line: int
    end_line: int


def read_source_lines(repo_dir: Path, rel_path: str, cache: SourceCache) -> list[str]:
    """Read and cache a file's lines (repo-relative path). Empty list if unreadable."""
    if rel_path not in cache:
        try:
            cache[rel_path] = (
                (repo_dir / rel_path).read_text(encoding=SOURCE_ENCODING, errors=SOURCE_DECODE_ERRORS).splitlines()
            )
        except OSError:
            cache[rel_path] = []
    return cache[rel_path]


def hash_method_body(lines: list[str], start_line: int, end_line: int) -> str:
    """Truncated SHA-256 of source lines [start_line-1:end_line]. '' when unavailable.

    Why '' on an out-of-range span: a stale line range must not hash to a stable
    but meaningless value that compares equal across unrelated code.
    """
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        return ""
    body = "\n".join(lines[start_line - 1 : end_line])
    return hashlib.sha256(body.encode(SOURCE_ENCODING, errors=SOURCE_DECODE_ERRORS)).hexdigest()[:16]


def hash_whole_file(lines: list[str]) -> str:
    """Truncated SHA-256 of the entire file's lines. '' when the file has no lines."""
    if not lines:
        return ""
    return hashlib.sha256("\n".join(lines).encode(SOURCE_ENCODING, errors=SOURCE_DECODE_ERRORS)).hexdigest()[:16]


def hash_file_residual(lines: list[str], spans: list[MethodSpan]) -> str:
    """Truncated SHA-256 of a file's *module-level* lines — everything outside ``spans``.

    Why: the whole-file hash also moves when a method body changes, so it cannot
    tell a pure method edit from a module-level (constant/import/decorator) edit. The
    residual excises every indexed method's line range and hashes only what is left,
    so a change here means module-level content moved even when a sibling method also
    changed. '' when the file is empty (matches ``hash_whole_file`` so a missing
    baseline residual degrades to "no module-level signal" rather than a false hit).
    """
    if not lines:
        return ""
    excised = [True] * len(lines)
    for start_line, end_line in spans:
        if start_line < 1 or end_line < start_line:
            continue
        for idx in range(start_line - 1, min(end_line, len(lines))):
            excised[idx] = False
    residual = "\n".join(line for line, keep in zip(lines, excised) if keep)
    return hashlib.sha256(residual.encode(SOURCE_ENCODING, errors=SOURCE_DECODE_ERRORS)).hexdigest()[:16]


def tree_hash_from_file_hashes(file_hashes: dict[str, str]) -> str:
    """SHA-256 over sorted ``path:hash`` lines. '' when no files carry a hash.

    Files with an unknown ('') hash are skipped so a partial read can't silently
    collide two different trees. Public so the wrapper can reproduce
    ``source_tree_hash`` from a fingerprint map.
    """
    parts = [f"{path}:{digest}" for path, digest in sorted(file_hashes.items()) if digest]
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode(SOURCE_ENCODING, errors=SOURCE_DECODE_ERRORS)).hexdigest()
