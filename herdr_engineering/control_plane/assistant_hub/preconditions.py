"""Hub-side deploy preconditions: the hub verifies, the worker's word counts for nothing.

``check_preconditions`` answers one question for a matched deploy: is every name in the scope's ``require`` list
VERIFIED?  Nothing but the 40-hex sha of the matcher reaches these checks, so a worker's claim ("CI is green") cannot
influence them.  Anything short of a verified yes is not ok: a failed check is listed in ``failed`` (the world says no),
a check that could not be completed (error, timeout, missing or odd data, no client) is listed in ``unchecked``.  No
exception ever leaves this module's entry points, and nothing here defaults to "allow".
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from herdr_engineering.github import _NoRedirect

from .deploy import DeployMatch

REQUIRE_ALL = ("on_main", "ci_green", "descends_from_deployed")
DEFAULT_TIMEOUT_SECONDS = 10
MAX_HEALTH_BYTES = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")
_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_COMPARE_STATUSES = frozenset({"ahead", "behind", "identical", "diverged"})
_CI_EVENTS = frozenset({"push", "manual"})       # only these can gate a deploy (not pull_request, cron, tag, ...)
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class PreconditionResult:
    ok: bool
    failed: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreconditionClients:
    """The hub's own read access.  Any member may be None: the checks that need it are then unverified."""
    gh: Any = None          # GitHubAppClient
    ci: Any = None          # WoodpeckerProvider
    health: Any = None      # HealthClient


class _Unchecked(Exception):
    """A check could not be completed.  Carries no text on purpose."""


class _Failed(Exception):
    """The check completed and the answer is no."""


class HealthClient:
    """``GET <health_url>?probe=1`` and read the deployed ``commit``.  No environment proxy, no redirect, bounded time
    and size; the URL was validated when the config loaded and is checked again here."""

    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout

    def deployed_commit(self, health_url: str) -> Any:
        parts = urllib.parse.urlsplit(health_url) if isinstance(health_url, str) else None
        if (parts is None or parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment
                or (parts.scheme == "http" and parts.hostname not in _LOOPBACK)):
            raise _Unchecked
        req = urllib.request.Request(health_url + "?probe=1", headers={"Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=self._timeout) as response:
                body = response.read(MAX_HEALTH_BYTES + 1)
            if len(body) > MAX_HEALTH_BYTES:
                raise _Unchecked
            doc = json.loads(body)
        except _Unchecked:
            raise
        except (OSError, ValueError, UnicodeError, urllib.error.URLError):
            raise _Unchecked from None
        except Exception:                     # noqa: BLE001 - http.client errors and the like: unverified, never raised on
            raise _Unchecked from None
        if not isinstance(doc, dict):
            raise _Unchecked
        return doc.get("commit")


class _Ctx:
    def __init__(self, match: DeployMatch, gh: Any, ci: Any, health: Any, deadline: float | None) -> None:
        self.match, self.gh, self.ci, self.health, self.deadline = match, gh, ci, health, deadline
        self.repo = f"github.com/{match.repo}"
        self._head: tuple[str, str] | BaseException | None = None

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise _Unchecked
        return fn(*args)

    def head(self) -> tuple[str, str]:
        """(default branch, tip sha), read once per evaluation."""
        if self._head is None:
            if self.gh is None:
                raise _Unchecked
            try:
                self._head = self.call(self.gh.default_branch_head, self.repo)
            except BaseException as exc:      # noqa: BLE001 - remembered so a failing read is not retried per check
                self._head = exc
        if isinstance(self._head, BaseException):
            raise _Unchecked
        branch, tip = self._head
        if not isinstance(branch, str) or not isinstance(tip, str):
            raise _Unchecked
        return branch, tip


def _on_main(ctx: _Ctx) -> None:
    _, tip = ctx.head()
    if tip != ctx.match.sha:
        raise _Failed


def _github_ci(ctx: _Ctx) -> None:
    if ctx.gh is None:
        raise _Unchecked
    sha = ctx.match.sha
    status = ctx.call(ctx.gh.commit_status, ctx.repo, sha)
    runs = ctx.call(ctx.gh.check_runs, ctx.repo, sha)
    if status.count + len(runs) == 0:
        raise _Failed                                  # no signal at all is not green
    if status.count > 0 and status.state != "success":
        raise _Failed
    for run in runs:
        if run.status != "completed" or run.conclusion not in _GREEN_CONCLUSIONS:
            raise _Failed


def _woodpecker_ci(ctx: _Ctx) -> None:
    if ctx.ci is None:
        raise _Unchecked
    branch, _ = ctx.head()
    pipelines = ctx.call(ctx.ci.public_pipelines, ctx.match.repo)
    if pipelines is None:
        raise _Unchecked                               # the configured CI address is not safe to read from
    mine = [p for p in pipelines if p.revision == ctx.match.sha and p.branch == branch and p.event in _CI_EVENTS]
    if not mine:
        raise _Failed
    latest = max(mine, key=lambda p: p.run_number if isinstance(p.run_number, int) else -1)
    if latest.state != "success":
        raise _Failed


def _ci_green(ctx: _Ctx) -> None:
    outcomes: list[type[Exception]] = []
    for part in (_github_ci, _woodpecker_ci):
        try:
            part(ctx)
        except _Failed:
            outcomes.append(_Failed)
        except Exception:                              # noqa: BLE001 - any error is "could not verify"
            outcomes.append(_Unchecked)
    if _Failed in outcomes:
        raise _Failed
    if outcomes:
        raise _Unchecked


def _descends(ctx: _Ctx, health_url: Any) -> None:
    if ctx.gh is None or ctx.health is None or not isinstance(health_url, str) or not health_url:
        raise _Unchecked
    deployed = ctx.call(ctx.health.deployed_commit, health_url)
    if not isinstance(deployed, str) or not _SHA.fullmatch(deployed):
        raise _Unchecked                               # a short or odd commit is never guessed at
    if deployed == ctx.match.sha:
        raise _Failed                                  # nothing new to deploy
    span = ctx.call(ctx.gh.commits_between, ctx.repo, deployed, ctx.match.sha)
    if span.status not in _COMPARE_STATUSES or span.ahead_by is None or span.behind_by is None:
        raise _Unchecked
    if span.status != "ahead" or span.behind_by != 0 or span.ahead_by < 1 or span.total < 1:
        raise _Failed


def _evaluate(match: DeployMatch, scope_cfg: Any, gh: Any, ci: Any, health: Any, deadline: float | None,
              ) -> PreconditionResult:
    require = scope_cfg.get("require", list(REQUIRE_ALL)) if isinstance(scope_cfg, dict) else None
    if not isinstance(require, list) or not require:
        return PreconditionResult(False, (), REQUIRE_ALL)
    wanted = [n for n in REQUIRE_ALL if n in require]
    stray = [n for n in require if n not in REQUIRE_ALL]
    if stray:                                           # a name this hub cannot verify can never be satisfied
        return PreconditionResult(False, (), tuple(dict.fromkeys([*wanted, *map(str, stray)])))
    if not isinstance(match.sha, str) or not _SHA.fullmatch(match.sha) or not isinstance(match.repo, str):
        return PreconditionResult(False, (), tuple(wanted))
    ctx = _Ctx(match, gh, ci, health, deadline)
    checks: dict[str, Callable[[], None]] = {
        "on_main": lambda: _on_main(ctx),
        "ci_green": lambda: _ci_green(ctx),
        "descends_from_deployed": lambda: _descends(ctx, scope_cfg.get("health_url")),
    }
    failed: list[str] = []
    unchecked: list[str] = []
    for name in wanted:
        try:
            checks[name]()
        except _Failed:
            failed.append(name)
        except Exception:                               # noqa: BLE001 - never raised into decide
            unchecked.append(name)
    return PreconditionResult(not failed and not unchecked, tuple(failed), tuple(unchecked))


def check_preconditions(match: DeployMatch, scope_cfg: dict, gh: Any, ci: Any, health: Any, *,
                        deadline: float | None = None) -> PreconditionResult:
    """``ok`` only if EVERY required name is verified.  ``deadline`` (a ``time.monotonic`` value) stops new remote
    calls from starting after it.  Never raises."""
    try:
        return _evaluate(match, scope_cfg, gh, ci, health, deadline)
    except BaseException:                               # noqa: BLE001 - fail closed on anything at all
        return PreconditionResult(False, (), REQUIRE_ALL)


def run_preconditions(clients: PreconditionClients | None, match: DeployMatch, scope_cfg: dict,
                      timeout: float = DEFAULT_TIMEOUT_SECONDS) -> PreconditionResult:
    """``check_preconditions`` under one overall deadline, on a worker thread so a slow remote cannot hold the caller.
    Past the deadline every required name is unchecked.  The caller must hold no lock."""
    names = _required(scope_cfg)
    unverified = PreconditionResult(False, (), names)
    if clients is None:
        return unverified
    box: list[PreconditionResult] = []
    deadline = time.monotonic() + timeout

    def work() -> None:
        box.append(check_preconditions(match, scope_cfg, clients.gh, clients.ci, clients.health, deadline=deadline))

    thread = threading.Thread(target=work, name="deploy-preconditions", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or not box:
        return unverified
    return box[0]


def _required(scope_cfg: Any) -> tuple[str, ...]:
    require = scope_cfg.get("require") if isinstance(scope_cfg, dict) else None
    if not isinstance(require, list) or not require:
        return REQUIRE_ALL
    return tuple(dict.fromkeys([*[n for n in REQUIRE_ALL if n in require], *[str(n) for n in require
                                                                              if n not in REQUIRE_ALL]]))


def describe(result: PreconditionResult) -> str:
    """A reason built from check names only (never remote text)."""
    parts = []
    if result.failed:
        parts.append("failed " + ", ".join(result.failed))
    if result.unchecked:
        parts.append("unchecked " + ", ".join(result.unchecked))
    return "; ".join(parts) or "not verified"


def default_clients(timeout: float = 8.0) -> PreconditionClients:
    """The production readers: the fleet's own ``github.app`` credentials, the configured ``ci.url`` read without a
    token, and the health reader.  Each piece that cannot be built is None (its checks stay unverified)."""
    gh: Any = None
    ci: Any = None
    try:
        from herdr_engineering import github_auth
        from herdr_engineering.review_gate.github_app import GitHubAppClient
        gh = GitHubAppClient(None, credentials=github_auth.credentials, timeout=timeout,
                             client=urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()))
    except Exception:                                   # noqa: BLE001
        gh = None
    try:
        from herdr_engineering.ci import WoodpeckerProvider
        from herdr_engineering.host_config import host_value
        url = host_value("ci.url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("no ci.url configured")     # never fall back to the provider's built-in public host
        ci = WoodpeckerProvider(base_url=url.strip())
        ci.public_timeout = timeout
    except Exception:                                   # noqa: BLE001
        ci = None
    return PreconditionClients(gh=gh, ci=ci, health=HealthClient(timeout))
