"""Automatic workspace / worktree / branch / artifact / dev-service naming.

``derive_workspace_identity`` is a required early foundational capability
(spec 0010 / master program). It derives a short, human-readable, kebab-case,
collision-safe name from a priority list of structured inputs and records the
provenance of the name source.

Rules:
  - Priority: explicit user name -> AutoSpec spec id/title -> GitHub issue
    number/title -> Pi Engineering mission title -> initial prompt ->
    <repo>-<short-task> -> <repo>-<short-id>.
  - Only auto-rename workspaces still marked ``automatic``; never overwrite a
    user rename.
  - Preserve spec/issue identity (e.g. "0110-dev-fabric", "421-test-tree").
  - At most one early refinement when stronger structured metadata arrives.
  - Never block workspace creation if naming fails (fallback to safe slug).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SOURCE_PRIORITY = ["auto_prompt", "auto_spec", "auto_issue", "auto_mission", "user"]
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_LEN = 64
_COLLISION_SUFFIX = "-{}"


def slugify(text: str, max_len: int = _MAX_LEN) -> str:
    """Produce a kebab-case slug safe for filesystem/branch/URL use."""
    if not text:
        return "untitled"
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    return s[:max_len].rstrip("-") or "untitled"


_LEADING_FILLER = [
    "implement", "add", "create", "build", "make", "setup", "set up",
    "work on", "please", "can you", "could you", "refactor", "update",
    "fix", "enable", "support", "improve", "enhance", "write", "develop",
    "modify", "change", "do", "provide", "allow", "let", "ensure",
    "need", "want", "help", "give", "produce", "generate", "set up",
]
_STOPWORDS = {
    "the", "a", "an", "and", "or", "so", "to", "for", "of", "that",
    "this", "these", "those", "with", "without", "every", "through",
    "via", "into", "from", "on", "in", "at", "by", "as", "is", "are",
    "be", "been", "was", "were", "it", "its", "our", "your", "my",
    "we", "you", "so", "then", "all", "any", "some", "just", "like",
    "about", "after", "before", "between", "over", "under", "using",
    "can", "could", "will", "would", "should", "might", "must", "may",
    "work", "please", "want", "need", "help", "do", "make", "get",
    "have", "has", "had", "been", "being", "would", "going",
}


def _concise(text: str, max_words: int = 4) -> str:
    """Extract a concise semantic slug, stripping filler verbs/stopwords.

    Applied to full prompts and titles so names stay short and meaningful
    (spec 0010: "free of meaningless prompt filler... approximately 2-6
    semantic words"). User-supplied names and explicit short_tasks bypass
    this so they are never mangled.
    """
    if not text:
        return ""
    norm = re.sub(r"[^a-z0-9\s-]", " ", text.lower())
    tokens = re.split(r"[\s-]+", norm.strip())
    tokens = [t for t in tokens if t]
    # strip one leading filler verb phrase
    if tokens and tokens[0] in _LEADING_FILLER:
        tokens = tokens[1:]
    words = [t for t in tokens if t not in _STOPWORDS]
    words = [t for t in words if t not in _LEADING_FILLER]
    # de-duplicate while preserving order
    seen = set()
    kept: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            kept.append(w)
    return "-".join(kept[:max_words])


def _short_id(value: Any) -> str:
    """A short collision-safe hash suffix for provenance when no title exists."""
    import hashlib
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:8]


@dataclass
class WorkspaceIdentity:
    name: str
    name_source: str          # one of SOURCE_PRIORITY
    refined: bool = False     # True if a stronger source refined the name
    base_input: str = ""      # the input that produced this name
    collision_suffix: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "name_source": self.name_source,
            "refined": self.refined,
            "base_input": self.base_input,
            "collision_suffix": self.collision_suffix,
        }


def _spec_slug(spec_id: str, title: str) -> str:
    """Preserve spec identity like '0110-dev-fabric'."""
    sid = slugify(spec_id, 12)
    t = _concise(title, 4)
    if t:
        return f"{sid}-{t}".rstrip("-") if sid else t
    return sid or "spec"


def _issue_slug(issue_number: Any, title: str) -> str:
    """Preserve issue identity like '421-test-tree'."""
    t = _concise(title, 4)
    if issue_number not in (None, ""):
        return f"{issue_number}-{t}".rstrip("-") if t else str(issue_number)
    return t


def derive_workspace_identity(
    *,
    user_name: str | None = None,
    spec_id: str | None = None,
    spec_title: str | None = None,
    issue_number: str | None = None,
    issue_title: str | None = None,
    mission_title: str | None = None,
    prompt: str | None = None,
    repository: str | None = None,
    short_task: str | None = None,
) -> WorkspaceIdentity:
    """Derive a canonical semantic slug with provenance, by priority."""
    # 1. Explicit user name wins and is never auto-renamed.
    if user_name and user_name.strip():
        return WorkspaceIdentity(name=slugify(user_name), name_source="user",
                                 base_input=user_name)
    # 2. AutoSpec spec id/title.
    if spec_id or spec_title:
        return WorkspaceIdentity(
            name=_spec_slug(spec_id or "", spec_title or ""),
            name_source="auto_spec",
            base_input=(spec_id or "") + (spec_title or ""))
    # 3. GitHub issue number/title.
    if issue_number or issue_title:
        return WorkspaceIdentity(
            name=_issue_slug(issue_number, issue_title or ""),
            name_source="auto_issue",
            base_input=(str(issue_number or "")) + (issue_title or ""))
    # 4. Pi Engineering mission title.
    if mission_title and mission_title.strip():
        return WorkspaceIdentity(name=_concise(mission_title) or slugify(mission_title),
                                 name_source="auto_mission", base_input=mission_title)
    # 5. Initial prompt.
    if prompt and prompt.strip():
        return WorkspaceIdentity(name=_concise(prompt) or slugify(prompt),
                                 name_source="auto_prompt", base_input=prompt)
    # 6/7. <repo>-<short-task> -> <repo>-<short-id>.
    repo = slugify(repository or "", 20) if repository else ""
    if short_task and short_task.strip():
        name = f"{repo}-{slugify(short_task, 20)}".strip("-")
        return WorkspaceIdentity(name=name or "task", name_source="auto_prompt",
                                 base_input=short_task)
    sid = _short_id(repository or prompt or "workspace")
    return WorkspaceIdentity(name=f"{repo}-{sid}".strip("-"), name_source="auto_prompt",
                             base_input="auto")


def ensure_collision_free(name: str, taken: set) -> str:
    """Append a numeric suffix until the name is unique. Never blocks creation."""
    candidate = name
    n = 2
    while candidate in taken:
        candidate = f"{name}-{n}"
        n += 1
    return candidate


class WorkspaceNamer:
    """Stateful namer that tracks user renames and allows one refinement."""

    def __init__(self) -> None:
        self._renamed_by_user: dict[str, str] = {}   # original -> user name
        self._refined: dict[str, str] = {}           # original -> refined name

    def register_user_rename(self, original: str, user_name: str) -> None:
        self._renamed_by_user[original] = user_name

    def apply(self, identity: WorkspaceIdentity) -> WorkspaceIdentity:
        """Return the final name honoring user renames and at-most-one refinement."""
        if identity.name in self._renamed_by_user:
            return WorkspaceIdentity(
                name=self._renamed_by_user[identity.name], name_source="user",
                refined=identity.refined, base_input=identity.base_input)
        if identity.name in self._refined:
            return WorkspaceIdentity(
                name=self._refined[identity.name], name_source=identity.name_source,
                refined=True, base_input=identity.base_input)
        return identity

    def refine(self, original: str, stronger: WorkspaceIdentity) -> WorkspaceIdentity:
        """Apply at most one early refinement when stronger metadata arrives."""
        if original in self._refined:
            return WorkspaceIdentity(
                name=self._refined[original], name_source=stronger.name_source,
                refined=True, base_input=stronger.base_input)
        if original in self._renamed_by_user:
            # never overwrite a user rename: keep the user's chosen name
            return WorkspaceIdentity(
                name=self._renamed_by_user[original], name_source="user",
                refined=True, base_input=stronger.base_input)
        if stronger.name_source in ("user",):
            self._renamed_by_user[original] = stronger.name
        else:
            self._refined[original] = stronger.name
        return WorkspaceIdentity(name=stronger.name, name_source=stronger.name_source,
                                 refined=True, base_input=stronger.base_input)
