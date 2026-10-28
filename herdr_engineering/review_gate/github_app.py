"""GitHub App client for the review gate.

This module holds the pipeline's GitHub credential. The App private key is read
from disk only while minting a JWT; installation tokens live in memory only.
Error messages are fixed strings (plus a status code) passed through
``redact_text``: no response body, token, JWT or key material ever reaches them.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from herdr_engineering.control_plane.project_links import canonical_repository
from herdr_engineering.control_plane.redact import redact_text
from herdr_engineering.github import _NoRedirect

from .config import AppConfig

API = "https://api.github.com"
MAX_RESPONSE = 4 * 1024 * 1024
TIMEOUT = 20
MAX_PR_PAGES = 20
COMPARE_FILE_CAP = 300
REFRESH_MARGIN = 60
STATUS_DESCRIPTION_MAX = 140
REVIEW_TITLE_MAX = 500
REVIEW_BODY_MAX = 20000
# Files read for a review (3 pages of 100). GitHub lists up to 3000 files per PR;
# a PR with 300 or more files is marked truncated, so it cannot PASS.
PR_FILES_CAP = 300
_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_BLOB = re.compile(r"[0-9a-fA-F]{40}")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$", re.MULTILINE)
SUBJECT_MAX = 200
_SQUASH_PR = re.compile(r"\(#([0-9]{1,9})\)\s*$")
_MERGE_PR = re.compile(r"Merge pull request #([0-9]{1,9})(?![0-9])")
_STATES = ("pending", "success", "failure", "error")
_MESSAGES = {
    "authentication": "GitHub rejected the App credential.",
    "permission": "GitHub denied access to this resource.",
    "not_found": "The GitHub repository or pull request is unavailable.",
    "rate_limited": "GitHub requests are paused until the rate limit resets.",
    "conflict": "GitHub reports a conflict with the current state.",
    "unavailable": "GitHub is temporarily unavailable.",
    "invalid_response": "GitHub returned an incomplete or invalid response.",
}


class GitHubAppError(RuntimeError):
    def __init__(self, reason, *, status=None, retry_at=None):
        self.reason = reason
        self.status = status
        self.retry_at = retry_at
        text = _MESSAGES.get(reason, _MESSAGES["unavailable"])
        if status:
            text += f" (HTTP {status})"
        super().__init__(redact_text(text))
        self.__context__ = None
        self.__suppress_context__ = True


@dataclass(frozen=True)
class PrInfo:
    number: int
    head_sha: str
    head_ref: str
    base_ref: str
    author_login: str
    draft: bool


@dataclass(frozen=True)
class ReviewFile:
    path: str
    status: str
    patch: str | None


@dataclass(frozen=True)
class ReviewMaterial:
    title: str
    body: str
    base_ref: str
    head_sha: str
    files: tuple[ReviewFile, ...]
    truncated: bool


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    subject: str
    pr_number: int | None


@dataclass(frozen=True)
class CommitRange:
    commits: tuple[CommitInfo, ...]
    total: int
    truncated: bool


def pr_number_from_subject(subject: str) -> int | None:
    """The PR a commit subject names: a trailing ``(#N)`` (squash) or ``Merge pull request #N``."""
    match = _MERGE_PR.match(subject) or _SQUASH_PR.search(subject)
    return int(match.group(1)) if match else None


def _repo_path(repo):
    canonical = canonical_repository(repo)
    parts = canonical.split("/") if canonical else []
    if (
        len(parts) != 3
        or parts[0] != "github.com"
        or not all(_NAME.fullmatch(p) and p not in (".", "..") for p in parts[1:])
    ):
        raise ValueError("repo must be a canonical github.com/<owner>/<name>")
    return f"{parts[1]}/{parts[2]}"


def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("sha must be a full 40 (or 64) hex commit id")
    return value.lower()


def _number(value):
    if type(value) is not int or value < 1:
        raise ValueError("pull request number must be a positive integer")
    return value


class GitHubAppClient:
    def __init__(self, config: AppConfig, *, client=None, now=time.time):
        self._config = config
        self._client = client or urllib.request.build_opener(_NoRedirect())
        self._now = now
        self._token = None
        self._token_expiry = 0.0

    # -- credentials -------------------------------------------------------
    def _mint_jwt(self):
        pem = None
        encoded = None
        failed = False
        try:
            import jwt  # lazy: importing this module must not require PyJWT

            with open(self._config.private_key_file, "rb") as handle:
                pem = handle.read(65536)
            now = int(self._now())
            encoded = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": str(self._config.app_id)},
                pem,
                algorithm="RS256",
            )
        except Exception:  # noqa: BLE001 - never chain: the cause could echo key material
            failed = True
        finally:
            pem = None  # drop the key before any exception can carry this frame
        if failed:
            raise GitHubAppError("authentication")
        return encoded

    @staticmethod
    def _parse_token(data):
        try:
            token = data["token"]
            expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return (token, expiry) if isinstance(token, str) and token else None

    def installation_token(self) -> str:
        if self._token and self._now() < self._token_expiry - REFRESH_MARGIN:
            return self._token
        data = self._send(
            "POST",
            f"/app/installations/{int(self._config.installation_id)}/access_tokens",
            bearer=self._mint_jwt(),
        )
        parsed = self._parse_token(data)
        data = None
        if parsed is None:
            raise GitHubAppError("invalid_response")
        self._token, self._token_expiry = parsed
        return parsed[0]

    # -- transport ---------------------------------------------------------
    def _send(self, method, path, *, body=None, bearer=None):
        app_auth = bearer is not None
        if not app_auth:
            bearer = self.installation_token()
        ok, payload = self._transport(method, path, body, bearer)
        bearer = None  # keep credentials out of this frame before any raise
        if not ok:
            if payload.status == 401 and not app_auth:
                self._token, self._token_expiry = None, 0.0
            raise payload
        return payload

    def _transport(self, method, path, body, bearer):
        """Return (True, data) or (False, error); never raises, so no traceback holds the credential."""
        request = urllib.request.Request(
            API + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "herdr-engineering",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._client.open(request, timeout=TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                return False, GitHubAppError("invalid_response")
            return True, (json.loads(raw.decode()) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            exc.close()
            return False, self._http_error(status, headers)
        except (ValueError, TypeError, UnicodeError):
            return False, GitHubAppError("invalid_response")
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            # HTTPException: IncompleteRead, BadStatusLine, LineTooLong, ...
            return False, GitHubAppError("unavailable")

    def _http_error(self, status, headers):
        get = headers.get if headers is not None else (lambda *_: None)
        if status == 429 or (
            status == 403 and (get("Retry-After") or get("X-RateLimit-Remaining") == "0")
        ):
            now = self._now()
            try:
                delay = get("Retry-After")
                retry = (
                    now + max(1, min(float(delay), 86400))
                    if delay is not None
                    else max(now + 60, float(get("X-RateLimit-Reset") or 0))
                )
            except (TypeError, ValueError):
                retry = now + 60
            return GitHubAppError("rate_limited", status=status, retry_at=retry)
        reason = {
            401: "authentication",
            403: "permission",
            404: "not_found",
            409: "conflict",
            422: "conflict",
        }.get(status, "unavailable")
        return GitHubAppError(reason, status=status)

    # -- operations --------------------------------------------------------
    def open_prs(self, repo: str) -> list[PrInfo]:
        slug = _repo_path(repo)
        out = []
        for page in range(1, MAX_PR_PAGES + 1):
            data = self._send("GET", f"/repos/{slug}/pulls?state=open&per_page=100&page={page}")
            if not isinstance(data, list):
                raise GitHubAppError("invalid_response")
            bad_item = False
            for item in data:
                try:
                    out.append(
                        PrInfo(
                            number=int(item["number"]),
                            head_sha=str(item["head"]["sha"]),
                            head_ref=str(item["head"]["ref"]),
                            base_ref=str(item["base"]["ref"]),
                            author_login=str((item.get("user") or {}).get("login", "")),
                            draft=bool(item.get("draft", False)),
                        )
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    bad_item = True
                if bad_item:
                    raise GitHubAppError("invalid_response")
            if len(data) < 100:
                return out
        raise GitHubAppError(
            "invalid_response"
        )  # cap reached with a full page: never truncate silently

    def pr_state(self, repo: str, number: int) -> PrInfo | None:
        """One fresh read of a PR: its PrInfo while open, None otherwise.

        Used right before a merge so a head move or retarget after the tick's
        listing is seen. Malformed data raises GitHubAppError("invalid_response").
        """
        slug, number = _repo_path(repo), _number(number)
        data = self._send("GET", f"/repos/{slug}/pulls/{number}")
        try:
            state = data["state"]
            info = PrInfo(
                number=number,
                head_sha=data["head"]["sha"],
                head_ref=data["head"]["ref"],
                base_ref=data["base"]["ref"],
                author_login=str((data.get("user") or {}).get("login", "")),
                draft=data.get("draft", False),
            )
        except (KeyError, TypeError, AttributeError):
            raise GitHubAppError("invalid_response") from None
        if not all(
            isinstance(v, str) for v in (state, info.head_sha, info.head_ref, info.base_ref)
        ):
            raise GitHubAppError("invalid_response")
        if not isinstance(info.draft, bool):
            raise GitHubAppError("invalid_response")
        if state != "open":
            return None
        return info

    def _review_pr(self, slug, number, sha):
        """(title, body, base_ref, base_sha) while the PR is open at ``sha``; None otherwise."""
        data = self._send("GET", f"/repos/{slug}/pulls/{number}")
        try:
            state, head = data["state"], data["head"]["sha"]
            base_ref, title, body = data["base"]["ref"], data["title"], data.get("body")
            raw_base_sha = data["base"]["sha"]
        except (KeyError, TypeError, AttributeError):
            raise GitHubAppError("invalid_response") from None
        base_sha = None
        try:
            base_sha = _sha(raw_base_sha)
        except ValueError:
            pass
        if base_sha is None:  # raised outside the except block: no chained context
            raise GitHubAppError("invalid_response")
        title = "" if title is None else title
        body = "" if body is None else body
        if not all(isinstance(v, str) for v in (state, head, base_ref, title, body)):
            raise GitHubAppError("invalid_response")
        if state != "open" or head.lower() != sha:
            return None
        return title[:REVIEW_TITLE_MAX], body[:REVIEW_BODY_MAX], base_ref, base_sha

    def review_material(
        self, repo: str, number: int, expected_head_sha: str, *, max_bytes: int
    ) -> ReviewMaterial | None:
        """Bounded title/body/patches of an open PR, or None if it moved off ``expected_head_sha``.

        Patches are included until their running UTF-8 size would exceed ``max_bytes``; after
        that files are only listed. Anything left out sets ``truncated``. The PR is re-read after
        the files so a push, close or retarget (base change) in between yields None.
        """
        slug, number, sha = _repo_path(repo), _number(number), _sha(expected_head_sha)
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        first = self._review_pr(slug, number, sha)
        if first is None:
            return None
        files, truncated, used, full = [], False, 0, False
        for page in range(1, PR_FILES_CAP // 100 + 1):
            data = self._send("GET", f"/repos/{slug}/pulls/{number}/files?per_page=100&page={page}")
            if not isinstance(data, list) or len(data) > 100:
                raise GitHubAppError("invalid_response")
            for item in data:
                try:
                    path, status, patch = item["filename"], item["status"], item.get("patch")
                except (KeyError, TypeError, AttributeError):
                    raise GitHubAppError("invalid_response") from None
                if not isinstance(path, str) or not isinstance(status, str):
                    raise GitHubAppError("invalid_response")
                if patch is not None and not isinstance(patch, str):
                    raise GitHubAppError("invalid_response")
                if patch is None or full:
                    truncated = True
                    patch = None
                else:
                    size = len(patch.encode("utf-8", "replace"))
                    if used + size > max_bytes:
                        full, truncated, patch = True, True, None
                    else:
                        used += size
                files.append(ReviewFile(path, status, patch))
            if len(data) < 100:
                break
        else:
            truncated = True  # read PR_FILES_CAP files: there may be more
        second = self._review_pr(slug, number, sha)
        # Both the base name AND the base commit must match the first read: a
        # name-only check misses a retarget main -> X -> main during the file
        # pages. A normal forward move of the base branch during the fetch also
        # voids it; the job is then skipped and retried after its lease expires.
        if second is None or second[2:] != first[2:]:
            return None  # head moved, closed, retargeted, or base moved during the fetch
        title, body, base_ref, _base_sha = first
        return ReviewMaterial(title, body, base_ref, sha, tuple(files), truncated)

    def set_status(
        self,
        repo: str,
        sha: str,
        state: Literal["pending", "success", "failure", "error"],
        description: str,
        *,
        context: str = "herdr/review-gate",
        target_url: str | None = None,
    ) -> None:
        slug, sha = _repo_path(repo), _sha(sha)
        if state not in _STATES:
            raise ValueError("invalid commit status state")
        if target_url and redact_text(target_url) != target_url:
            raise ValueError("target_url must not carry secrets")
        body = {
            "state": state,
            "context": context,
            "description": redact_text(str(description))[:STATUS_DESCRIPTION_MAX],
        }
        if target_url:
            body["target_url"] = target_url
        self._send("POST", f"/repos/{slug}/statuses/{sha}", body=body)

    def merge_pr(self, repo: str, number: int, sha: str, *, method: str = "squash") -> bool:
        return self.merge_pr_sha(repo, number, sha, method=method) is not None

    def merge_pr_sha(self, repo: str, number: int, sha: str, *, method: str = "squash") -> str | None:
        """Merge, returning the merge commit id, ``""`` when merged but GitHub gave no valid id, or None when refused."""
        slug, number, sha = _repo_path(repo), _number(number), _sha(sha)
        if method not in ("squash", "merge", "rebase"):
            raise ValueError("invalid merge method")
        try:
            data = self._send(
                "PUT",
                f"/repos/{slug}/pulls/{number}/merge",
                body={"sha": sha, "merge_method": method},
            )
        except GitHubAppError as exc:
            if exc.status in (405, 409):
                return None
            raise
        try:
            return _sha(data.get("sha")) if isinstance(data, dict) else ""
        except ValueError:
            return ""

    def update_branch(self, repo: str, number: int, expected_head_sha: str) -> None:
        slug, number, sha = _repo_path(repo), _number(number), _sha(expected_head_sha)
        self._send(
            "PUT", f"/repos/{slug}/pulls/{number}/update-branch", body={"expected_head_sha": sha}
        )

    def default_branch_head(self, repo: str) -> tuple[str, str]:
        """(default branch name, its head sha): two reads, malformed data is invalid_response."""
        slug = _repo_path(repo)
        info = self._send("GET", f"/repos/{slug}")
        branch = info.get("default_branch") if isinstance(info, dict) else None
        if (
            not isinstance(branch, str)
            or not branch
            or len(branch) > 255
            or any(ord(c) < 33 or ord(c) == 127 for c in branch)
        ):
            raise GitHubAppError("invalid_response")
        data = self._send("GET", f"/repos/{slug}/branches/{urllib.parse.quote(branch, safe='')}")
        raw = (data.get("commit") or {}).get("sha") if isinstance(data, dict) else None
        head = None
        try:
            head = _sha(raw)
        except ValueError:
            pass
        if head is None:  # raised outside the except block: no chained context
            raise GitHubAppError("invalid_response")
        return branch, head

    def pr_review_summary(self, repo: str, number: int, head_sha: str) -> tuple[int, bool]:
        """(human approvals of THIS head, whether any reviewer's latest review requests changes).

        Only a person (``user.type == "User"``) who is an owner, member or collaborator counts, only on
        ``head_sha`` (an approval of an older commit is stale), and only the reviewer's latest review counts.
        """
        slug, number, sha = _repo_path(repo), _number(number), _sha(head_sha)
        latest: dict[str, dict] = {}
        for page in range(1, 6):
            data = self._send("GET", f"/repos/{slug}/pulls/{number}/reviews?per_page=100&page={page}")
            if not isinstance(data, list):
                raise GitHubAppError("invalid_response")
            for item in data:
                user = item.get("user") if isinstance(item, dict) else None
                if not isinstance(user, dict) or not isinstance(user.get("login"), str):
                    continue
                if item.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                    latest[user["login"]] = item
            if len(data) < 100:
                break
        approvals, changes = 0, False
        for item in latest.values():
            state = item.get("state")
            if state == "CHANGES_REQUESTED":
                changes = True
            elif (state == "APPROVED" and str(item.get("commit_id", "")).lower() == sha
                  and (item.get("user") or {}).get("type") == "User"
                  and item.get("author_association") in ("OWNER", "MEMBER", "COLLABORATOR")):
                approvals += 1
        return approvals, changes

    def pr_commits_verified(self, repo: str, number: int, head_sha: str) -> bool:
        """True only if the PR has at most 100 commits, ends at ``head_sha`` and GitHub verified every signature."""
        slug, number, sha = _repo_path(repo), _number(number), _sha(head_sha)
        data = self._send("GET", f"/repos/{slug}/pulls/{number}/commits?per_page=100")
        if not isinstance(data, list) or not data or len(data) >= 100:
            return False
        try:
            if str(data[-1]["sha"]).lower() != sha:
                return False
            return all(((c.get("commit") or {}).get("verification") or {}).get("verified") is True for c in data)
        except (KeyError, TypeError, AttributeError):
            return False

    def head_commit(self, repo: str, sha: str) -> CommitInfo:
        """One commit's subject and PR number (the range when no green sha is known)."""
        slug, sha = _repo_path(repo), _sha(sha)
        data = self._send("GET", f"/repos/{slug}/commits/{sha}")
        message = (data.get("commit") or {}).get("message") if isinstance(data, dict) else None
        if not isinstance(message, str):
            raise GitHubAppError("invalid_response")
        first = message.split("\n", 1)[0].strip()
        return CommitInfo(sha, first[:SUBJECT_MAX], pr_number_from_subject(first))

    def commits_between(self, repo: str, base_sha: str, head_sha: str) -> CommitRange:
        """Commits after ``base_sha`` up to ``head_sha``, oldest first, one compare read.

        GitHub returns at most 250 commits there; ``truncated`` is set when it reports more.
        """
        slug, base, head = _repo_path(repo), _sha(base_sha), _sha(head_sha)
        data = self._send("GET", f"/repos/{slug}/compare/{base}...{head}")
        raw = data.get("commits") if isinstance(data, dict) else None
        total = data.get("total_commits") if isinstance(data, dict) else None
        if not isinstance(raw, list) or type(total) is not int or total < 0 or total < len(raw):
            raise GitHubAppError("invalid_response")
        out = []
        failed = False
        try:
            for item in raw:
                sha = _sha(item["sha"])
                message = item["commit"]["message"]
                if not isinstance(message, str):
                    raise ValueError
                first = message.split("\n", 1)[0].strip()
                out.append(CommitInfo(sha, first[:SUBJECT_MAX], pr_number_from_subject(first)))
        except (KeyError, TypeError, AttributeError, ValueError):
            failed = True
        if failed:
            raise GitHubAppError("invalid_response")
        return CommitRange(tuple(out), total, total > len(out))

    def patch_id(self, repo: str, base_sha: str, head_sha: str) -> str:
        """Hash of the per-file patches between base and head, hunk line offsets ignored.

        Fails closed: raises GitHubAppError("invalid_response") whenever the diff cannot be
        fingerprinted completely (300+ files, a patch-less file without a blob sha). Callers
        (Task 8's GateService) treat a raising patch_id as "no carry-over, re-review".
        """
        slug, base, head = _repo_path(repo), _sha(base_sha), _sha(head_sha)
        data = self._send("GET", f"/repos/{slug}/compare/{base}...{head}")
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            raise GitHubAppError("invalid_response")
        if len(files) >= COMPARE_FILE_CAP:
            # GitHub caps compare `files` at 300 and does not page them: patch too large to fingerprint.
            raise GitHubAppError("invalid_response")
        digest = hashlib.sha256()
        failed = False
        try:
            for item in sorted(files, key=lambda f: f["filename"]):
                patch = item.get("patch")
                if isinstance(patch, str):
                    text = _HUNK.sub("@@", patch)
                else:
                    blob = item.get("sha")
                    if not isinstance(blob, str) or not _BLOB.fullmatch(blob):
                        raise ValueError
                    text = "blob:" + blob.lower()
                for part in (
                    item["filename"],
                    str(item.get("previous_filename") or ""),
                    str(item.get("status", "")),
                    text,
                ):
                    raw = part.encode()
                    digest.update(len(raw).to_bytes(8, "big"))
                    digest.update(raw)
        except (KeyError, TypeError, AttributeError, ValueError):
            failed = True
        if failed:
            raise GitHubAppError("invalid_response")
        return digest.hexdigest()
