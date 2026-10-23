"""Bounded task delegation and artifact proposals (T11): constants and pure validators.

A *delegation* is an ordinary HCN task (same store, same events) that carries a ``bounds`` block and is
addressed to an external coworker agent that was invited with ``proposals: true``. The delegate never
receives ``execute`` (so it never appears in ``tasks/inbox``) and never a terminal. Its ONLY output is an
*artifact proposal*: untrusted text held in quarantine until a room owner accepts it, and even then it is
recorded, never applied.

Everything here is import-light and side-effect free so the service, the MCP adapter and the A2A adapter
share one set of checks. Nothing in this module reads a file, resolves a name or fetches a URL: a proposal
is data, and a link proposal is a string that the server never dereferences.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

from . import collab_external as ext
from .redact import redact_text

OUTPUT_KINDS = ("patch", "doc", "analysis", "link")

# Hard ceilings: a delegation may ask for less, never more.
MAX_ARTIFACT_BYTES = 32 * 1024              # one proposal's content (the log row must stay far under MAX_BODY)
DEFAULT_ARTIFACT_BYTES = 16 * 1024
MAX_PROPOSALS_PER_TASK = 5
MAX_STEPS = 200                             # progress notes + proposals the delegate may send for one task
MAX_TOKENS = 10_000_000                     # declarative: the server cannot measure an agent's token use
MIN_SECONDS, MAX_SECONDS = 60.0, 24 * 3600.0
MAX_SCOPE_PATHS = 20
MAX_SCOPE_NODES = 20
MAX_SCOPE_QUESTIONS = 10
MAX_PATCH_FILES = 20
MAX_PATCH_LINES = 2000
MAX_HEADER_LINE = 1100                     # two 255-byte paths plus slack; also keeps the header regexes linear
MAX_TITLE = 120
MAX_NOTE = 140
MAX_REASON = 200
PROPOSAL_TTL = 3 * 24 * 3600.0              # a proposal nobody decided in three days expires

MAX_OPEN_BOUNDED_PER_AGENT = 3
MAX_QUARANTINED_PER_ROOM = 40
MAX_PROPOSALS_PER_AGENT = 30                # lifetime, per delegate credential
MAX_PROPOSALS_PER_SPONSOR = 150             # lifetime, across every delegate a room owner invited
PROPOSALS_PER_10_MIN = 6                    # per delegate
DELEGATIONS_PER_10_MIN = 20                 # per room owner
RESPONSES_PER_MINUTE = 60                   # per delegate: progress / accept / result calls
DECISIONS_PER_MINUTE = 60                   # per room owner

HCN_NODE_PREFIXES = ("task:hcn-delegation.", "artifact:hcn-proposal.")   # written only by the HCN service

_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_NODE = re.compile(r"[a-z]+:[A-Za-z0-9._:/@+=-]{1,190}\Z")
_DRIVE = re.compile(r"[A-Za-z]:")
# Hard-coded and not overridable by a room owner or a delegate: these name files a checkout executes.
_DENIED_SEGMENTS = frozenset({".git", ".gitmodules", ".gitattributes", ".gitconfig", ".husky", ".githooks", ".circleci",
                              ".gitlab-ci.yml", "jenkinsfile", ".pre-commit-config.yaml"})
_DENIED_PREFIXES = (".github/workflows/", ".github/actions/")
_SHORT_NAME = re.compile(r"[^.~]{1,6}~[0-9]+(\.[^.]{0,3})?\Z")     # NTFS 8.3 alias such as GIT~1 of .git
_INSTRUCTION_LIKE = re.compile(
    r"(ignore (all |any |the )?(previous|prior|above) (instructions|messages|prompts)|"
    r"disregard (the |your )?(system|previous)|you are now |system prompt|"
    r"(run|execute) (the following|this) (command|script)|curl [^\n|]{0,200}\|[ \t]{0,20}(ba)?sh|"
    r"reveal (your|the) (secrets?|credentials?|tokens?)|do not tell the (user|owner))", re.I)


class DelegationError(ValueError):
    """A refusal with an HTTP status (400 malformed, 403 outside the delegated bounds)."""

    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


# ---------------------------------------------------------------------------------- paths
def norm_path(value: object, *, allow_dir: bool = False) -> str:
    """A repository-relative path that cannot escape its root or name git internals. ``allow_dir`` keeps one
    trailing ``/`` (a directory prefix; ``dir/**`` is the same thing). Raises DelegationError(400)."""
    if not isinstance(value, str) or not value or len(value) > 255:
        raise DelegationError(400, "paths are 1-255 characters")
    path = value
    if allow_dir and path.endswith("/**"):
        path = path[:-2]
    if path != ext.clean_text(path, 255) or "\\" in path or _DRIVE.match(path) or "\x00" in path:
        raise DelegationError(400, "path contains characters that are not allowed")
    if path.startswith(("/", "~")) or ":" in path:
        raise DelegationError(400, "absolute paths and drive or stream names are not allowed")
    is_dir = path.endswith("/")
    segments = path.rstrip("/").split("/")
    for seg in segments:
        low = seg.casefold()
        if (not seg or seg in (".", "..") or seg != seg.strip() or seg.endswith(".")
                or low in _DENIED_SEGMENTS or _SHORT_NAME.match(low)):
            raise DelegationError(400, "path escapes the repository or names a protected file")
    clean = "/".join(segments) + ("/" if is_dir else "")
    if any((clean + ("" if is_dir else "/")).casefold().startswith(p) for p in _DENIED_PREFIXES):
        raise DelegationError(400, "proposals cannot touch CI definitions")
    if is_dir and not allow_dir:
        raise DelegationError(400, "a file path must not end with /")
    return clean


def in_scope(path: str, scope_paths: list[str]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in scope_paths)


# ---------------------------------------------------------------------------------- bounds
def _span(body: dict, name: str, default: int, lo: int, hi: int) -> int:
    v = body.get(name, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise DelegationError(400, f"{name} must be an integer {lo}-{hi}")
    return v


def parse_bounds(body: dict, now: float, hard_expiry: float, task_ttl: float) -> dict:
    """Validate the owner's scope, budget, expiry and allowed output kinds. Server-side and strict: unknown
    keys are refused, every list is capped, the expiry cannot outlive the delegate's credential."""
    if set(body) - {"scope", "budget", "expires_in_seconds", "output_kinds"}:
        raise DelegationError(400, "unknown field")
    scope_in, budget_in = body.get("scope"), body.get("budget", {})
    if not isinstance(scope_in, dict) or not isinstance(budget_in, dict):
        raise DelegationError(400, "scope and budget must be objects")
    if set(scope_in) - {"paths", "nodes", "questions"}:
        raise DelegationError(400, "unknown scope field")
    if set(budget_in) - {"max_proposals", "max_steps", "max_artifact_bytes", "max_tokens"}:
        raise DelegationError(400, "unknown budget field")

    def listing(name: str, cap: int) -> list:
        v = scope_in.get(name, [])
        if not isinstance(v, list) or len(v) > cap:
            raise DelegationError(400, f"scope.{name} must be a list of at most {cap}")
        return v
    paths = sorted({norm_path(p, allow_dir=True) for p in listing("paths", MAX_SCOPE_PATHS)})
    nodes = []
    for n in listing("nodes", MAX_SCOPE_NODES):
        if not isinstance(n, str) or not _NODE.match(n) or n.split(":", 1)[0] not in _node_kinds():
            raise DelegationError(400, "scope.nodes entries are graph node ids like commit:abc")
        nodes.append(n)
    questions = []
    for q in listing("questions", MAX_SCOPE_QUESTIONS):
        qid = q.get("id") if isinstance(q, dict) else None
        text = ext.clean_text(q.get("text"), 500) if isinstance(q, dict) else ""
        if not isinstance(qid, str) or not _ID.match(qid) or not text or set(q) - {"id", "text"}:
            raise DelegationError(400, "scope.questions entries are {id, text}")
        questions.append({"id": qid, "text": text})
    if not (paths or nodes or questions):
        raise DelegationError(400, "scope must name at least one path, node or question")
    if len({q["id"] for q in questions}) != len(questions):
        raise DelegationError(400, "question ids must be unique")
    kinds = body.get("output_kinds")
    if (not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) and k in OUTPUT_KINDS for k in kinds)):
        raise DelegationError(400, "output_kinds must be a non-empty subset of " + ", ".join(OUTPUT_KINDS))
    kinds = [k for k in OUTPUT_KINDS if k in kinds]
    if "patch" in kinds and not paths:
        raise DelegationError(400, "a patch needs scope.paths naming where it may write")
    if "analysis" in kinds and not (questions or nodes):
        raise DelegationError(400, "an analysis needs scope.questions or scope.nodes to answer")
    seconds = body.get("expires_in_seconds", min(task_ttl, MAX_SECONDS))
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise DelegationError(400, f"expires_in_seconds must be {int(MIN_SECONDS)}-{int(MAX_SECONDS)}")
    expires = min(now + float(seconds), hard_expiry)
    if expires - now < MIN_SECONDS:
        raise DelegationError(400, "the delegate's credential expires too soon for a task")
    budget = {"max_proposals": _span(budget_in, "max_proposals", 1, 1, MAX_PROPOSALS_PER_TASK),
              "max_steps": _span(budget_in, "max_steps", 20, 1, MAX_STEPS),
              "max_artifact_bytes": _span(budget_in, "max_artifact_bytes", DEFAULT_ARTIFACT_BYTES, 256, MAX_ARTIFACT_BYTES),
              "max_tokens": _span(budget_in, "max_tokens", 100_000, 0, MAX_TOKENS)}
    return {"scope": {"paths": paths, "nodes": nodes, "questions": questions}, "budget": budget,
            "expires_at": expires, "output_kinds": kinds}


def _node_kinds() -> frozenset:
    from .graph import NODE_TYPES
    return NODE_TYPES


# ---------------------------------------------------------------------------------- content
def _text(value: object, limit: int, what: str) -> str:
    """Untrusted text is refused, not repaired, when it hides anything: control, bidirectional or invisible
    characters could make a reviewer read something other than what an applier would act on."""
    if not isinstance(value, str) or not value.strip():
        raise DelegationError(400, f"{what} is required")
    text = value
    if len(text.encode("utf-8", "surrogatepass")) > limit:
        raise DelegationError(400, f"{what} is larger than the {limit} byte limit", "too_large")
    if "\r" in text:
        raise DelegationError(400, f"{what} has CR or CRLF line endings; send LF-only text (nothing is converted)", "crlf")
    if "\x00" in text or ext._strip(text) != text:
        raise DelegationError(400, f"{what} contains control, invisible or direction-changing characters", "hidden_characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise DelegationError(400, f"{what} is not valid text", "binary") from None
    if redact_text(text) != text:
        raise DelegationError(400, f"{what} contains secret-like material", "secret_like")
    return text


_HUNK = re.compile(r"@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@")
_GIT = re.compile(r"diff --git a/(\S.*) b/(\S.*)\Z")
_MODE = re.compile(r"(new|deleted) file mode (\d{6})\Z")


def _diff_path(raw: str) -> str | None:
    """A ---/+++ name exactly as git would read it: up to the first tab, nothing stripped. A name with leading,
    trailing or any other whitespace or format character is refused rather than normalised, so the path we check
    is the path an applier opens."""
    raw = raw.split("\t", 1)[0]
    if raw == "/dev/null":
        return None
    if any(c.isspace() or unicodedata.category(c) in ("Cf", "Cc", "Zs", "Zl", "Zp") for c in raw):
        raise DelegationError(400, "patch paths must not contain whitespace or invisible characters", "bad_patch")
    if not raw.startswith(("a/", "b/")):
        raise DelegationError(400, "patch paths must be a/<path> or b/<path>", "bad_patch")
    return norm_path(raw[2:])


def parse_patch(text: str, scope_paths: list[str]) -> list[str]:
    """Strictly parse a unified diff and return the repository paths it touches. Refuses (never repairs):
    absolute or ``..`` paths, git internals and CI definitions, paths outside ``scope_paths``, renames and
    copies, mode changes, symlinks (120000), submodules (160000), executable files (100755), binary
    patches, and anything the hunk counts do not account for."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > MAX_PATCH_LINES:
        raise DelegationError(400, f"a patch is at most {MAX_PATCH_LINES} lines", "too_large")
    touched: list[str] = []
    i = 0
    cur_old = cur_new = None        # paths of the file block being read (None = /dev/null)
    have_header = False

    def bad(msg: str):
        raise DelegationError(400, "malformed patch: " + msg, "bad_patch")

    def finish_pair(old: str | None, new: str | None) -> None:
        if old is not None and new is not None and old != new:
            raise DelegationError(400, "renames and copies are not accepted", "bad_patch")
        path = new if new is not None else old
        if path is None:
            bad("a file block names no file")
        if not in_scope(path, scope_paths):
            raise DelegationError(403, f"{path} is outside the delegated scope", "out_of_scope")
        if path not in touched:
            touched.append(path)
        if len(touched) > MAX_PATCH_FILES:
            raise DelegationError(400, f"a patch touches at most {MAX_PATCH_FILES} files", "too_large")
    git_pair: tuple[str, str] | None = None
    git_kind: str | None = None                          # "new" / "deleted" from the extended header
    while i < len(lines):
        line = lines[i]
        if len(line) > MAX_HEADER_LINE and line.startswith(("diff ", "--- ", "+++ ", "@@")):
            bad("header line is too long")
        if (m := _GIT.match(line)):
            for nm in (m.group(1), m.group(2)):
                _diff_path("a/" + nm)                    # the same whitespace / invisible-character refusal
            a, b = norm_path(m.group(1)), norm_path(m.group(2))
            if a != b:
                raise DelegationError(400, "renames and copies are not accepted", "bad_patch")
            git_pair, have_header, git_kind = (a, b), True, None
            i += 1
            while i < len(lines) and not lines[i].startswith(("--- ", "diff --git ", "@@")):
                h = lines[i]
                if (mm := _MODE.match(h)):
                    git_kind = mm.group(1)
                    if mm.group(2) != "100644":
                        raise DelegationError(400, f"file mode {mm.group(2)} is not accepted (regular, non-executable files only)", "bad_mode")
                elif h.startswith(("old mode", "new mode")):
                    raise DelegationError(400, "mode changes are not accepted", "bad_mode")
                elif h.startswith(("rename ", "copy ", "similarity ", "dissimilarity ")):
                    raise DelegationError(400, "renames and copies are not accepted", "bad_patch")
                elif h.startswith(("GIT binary patch", "Binary files", "literal ", "delta ")):
                    raise DelegationError(400, "binary patches are not accepted", "binary")
                elif not (h.startswith("index ") and re.fullmatch(r"index [0-9a-f]{4,64}\.\.[0-9a-f]{4,64}( 100644)?", h)):
                    bad("unknown header line")
                i += 1
            if i >= len(lines) or lines[i].startswith("diff --git "):
                bad("a file block has no hunks")   # an empty add/delete or mode-only block carries nothing reviewable
            continue
        if line.startswith("--- "):
            if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                bad("--- without +++")
            cur_old, cur_new = _diff_path(line[4:]), _diff_path(lines[i + 1][4:])
            if (git_kind == "new" and cur_old is not None) or (git_kind == "deleted" and cur_new is not None):
                bad("the file mode line and the --- / +++ names disagree")
            if git_pair is not None:
                for p in (cur_old, cur_new):
                    if p is not None and p != git_pair[0]:
                        bad("header paths disagree")
            finish_pair(cur_old, cur_new)
            git_pair, have_header, git_kind = None, True, None
            i += 2
            nh = 0
            while i < len(lines) and (hm := _HUNK.match(lines[i])):
                nh += 1
                old_n = int(hm.group(2)) if hm.group(2) is not None else 1
                new_n = int(hm.group(4)) if hm.group(4) is not None else 1
                # git decides creation / deletion from /dev/null AND from a 0,0 side; both must say the same thing
                creates = hm.group(1) == "0" and old_n == 0
                deletes = hm.group(3) == "0" and new_n == 0
                if (cur_old is None) != creates or (cur_new is None) != deletes:
                    bad("/dev/null and the hunk ranges disagree (creation needs -0,0, deletion needs +0,0)")
                if (cur_old is None or cur_new is None) and nh > 1:
                    bad("a created or deleted file has exactly one hunk")
                i += 1
                while old_n > 0 or new_n > 0:
                    if i >= len(lines):
                        bad("hunk is shorter than its header says")
                    c = lines[i][:1]
                    if c == "\\":                      # "\ No newline at end of file"
                        i += 1
                        continue
                    if c == " " or lines[i] == "":
                        old_n, new_n = old_n - 1, new_n - 1
                    elif c == "-":
                        old_n -= 1
                    elif c == "+":
                        new_n -= 1
                    else:
                        bad("unexpected line in hunk")
                    i += 1
                while i < len(lines) and lines[i].startswith("\\"):
                    i += 1
                if old_n < 0 or new_n < 0:
                    bad("hunk is longer than its header says")
            if nh == 0:
                bad("a file block has no hunks")
            continue
        bad("unexpected line outside a hunk")
    if not have_header or not touched:
        bad("no file changes found")
    return touched


def check_link(url: object) -> str:
    """An https URL with a public-looking host, kept as a string. It is never fetched by the server."""
    if not isinstance(url, str) or not url or len(url) > 500 or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in url):
        raise DelegationError(400, "url must be a plain ASCII https URL of at most 500 characters")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port                    # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise DelegationError(400, "url is not a valid https URL") from None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or not host:
        raise DelegationError(400, "only https URLs without credentials are accepted")
    if (port not in (None, 443) or "." not in host or host.endswith((".local", ".internal", ".localhost", ".lan"))
            or re.fullmatch(r"[0-9.]+|\[.*\]|0x[0-9a-f]+", host) or host == "localhost"):
        raise DelegationError(400, "the host must be a public DNS name on port 443")
    if redact_text(url) != url:
        raise DelegationError(400, "url contains secret-like material", "secret_like")
    return url


def check_refs(refs: object, scope: dict) -> dict:
    """What the proposal says it concerns. Everything must be inside the delegated scope."""
    refs = {} if refs is None else refs
    if not isinstance(refs, dict) or set(refs) - {"paths", "nodes", "questions"}:
        raise DelegationError(400, "refs may hold paths, nodes and questions")
    out = {}
    for name, allowed, cap in (("paths", scope["paths"], MAX_SCOPE_PATHS), ("nodes", scope["nodes"], MAX_SCOPE_NODES),
                               ("questions", [q["id"] for q in scope["questions"]], MAX_SCOPE_QUESTIONS)):
        vals = refs.get(name, [])
        if not isinstance(vals, list) or len(vals) > cap or not all(isinstance(v, str) for v in vals):
            raise DelegationError(400, f"refs.{name} must be a short list of strings")
        if name == "paths":
            vals = [norm_path(v) for v in vals]
            if any(not in_scope(v, allowed) for v in vals):
                raise DelegationError(403, "a referenced path is outside the delegated scope", "out_of_scope")
        elif any(v not in allowed for v in vals):
            raise DelegationError(403, f"a referenced {name[:-1]} is outside the delegated scope", "out_of_scope")
        out[name] = sorted(set(vals))
    return out


def build_proposal(task_id: str, bounds: dict, body: dict) -> dict:
    """Validate one proposal against its delegation and return the normalised record (no I/O)."""
    allowed = {"kind", "title", "content", "url", "refs", "supersedes", "expected_version", "idempotency_key"}
    if set(body) - allowed:
        raise DelegationError(400, "unknown field")
    kind = body.get("kind")
    if not isinstance(kind, str) or kind not in OUTPUT_KINDS:
        raise DelegationError(400, "kind must be one of " + ", ".join(OUTPUT_KINDS))
    if kind not in bounds["output_kinds"]:
        raise DelegationError(403, f"this task does not allow a {kind} proposal", "kind_not_allowed")
    title = ext.clean_text(body.get("title"), MAX_TITLE)
    if not title:
        raise DelegationError(400, "title is required")
    cap = bounds["budget"]["max_artifact_bytes"]
    scope = bounds["scope"]
    refs = check_refs(body.get("refs"), scope)
    content = url = None
    if kind == "link":
        if body.get("content") is not None:
            raise DelegationError(400, "a link proposal carries url, not content")
        url = check_link(body.get("url"))
        size = len(url)
    else:
        if body.get("url") is not None:
            raise DelegationError(400, "only a link proposal carries a url")
        content = _text(body.get("content"), cap, "content")
        size = len(content.encode())
        if kind == "patch":
            touched = parse_patch(content, scope["paths"])
            refs["paths"] = sorted(set(refs["paths"]) | set(touched))
        elif kind == "analysis" and not (refs["questions"] or refs["nodes"]):
            raise DelegationError(400, "an analysis must name the scope.questions or scope.nodes it answers")
    flags = ["instruction_like_text"] if _INSTRUCTION_LIKE.search(content or title) else []
    digest = hashlib.sha256(json.dumps([task_id, kind, title, content, url, refs], sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return {"proposal_id": "prp_" + digest[:32], "digest": digest, "kind": kind, "title": title,
            "content": content, "url": url, "refs": refs, "size": size, "flags": flags}
