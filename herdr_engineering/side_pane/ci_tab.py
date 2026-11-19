"""CI cards for the branches a chain pushed: tokenless reads of a public Woodpecker, never an exception.

The data comes from `WoodpeckerProvider.public_pipelines` / `public_workflows` (no credential is read or sent) and from
the rows of `gh pr list --json number,title,headRefName,state,mergedAt,url,statusCheckRollup`, which the caller has already
fetched: every check of a rollup (GitHub Actions, review bots, GitGuardian, Woodpecker contexts) is one chip, the
Woodpecker contexts being left out where the public API already gave the richer per-workflow chip. The
control plane's `pr_trail.build` is not used: it joins server-side session and review records a local pane does not have,
and the card only needs to know whether a PR is merged.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from ..agent_ops_tui import RenderOpts
from ..ci import PipelineView
from ..errors import HerdrEngineeringError
from .style import Seg, Style, duration, line


class CiProvider(Protocol):
    def public_pipelines(self, repository: str) -> list[PipelineView] | None: ...
    def public_workflows(self, repository: str, number: int) -> list[dict[str, str]] | None: ...


class CiState(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    NOT_STARTED = "not_started"  # nothing ran: shown grey, not red
    MERGED = "merged"
    UNAVAILABLE = "unavailable"  # CI could not be read


@dataclass(frozen=True)
class CiCard:
    ref: str  # "#44" for a pull request, else the branch
    title: str
    state: CiState
    workflows: tuple[str, ...]
    url: str | None
    branch: str = ""
    failed: tuple[str, ...] = ()
    running: tuple[str, ...] = ()
    reason: str | None = None
    waiting: tuple[str, ...] = ()  # checks that exist and have not started: grey
    pushed_by: tuple[int, float] | None = None  # (session number in the chain, epoch seconds) of the checkpoint push
    stale: bool = False  # the last good read, kept while the newest one failed or timed out


_STATE = {"running": CiState.RUNNING, "pending": CiState.RUNNING, "blocked": CiState.RUNNING,
          "success": CiState.PASSED, "failure": CiState.FAILED, "error": CiState.FAILED, "killed": CiState.FAILED,
          "declined": CiState.NOT_STARTED, "skipped": CiState.NOT_STARTED, "canceled": CiState.NOT_STARTED}
_BAD = frozenset({"failure", "error", "killed"})


def _field(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value else None


def _pull_for(branch: str, pulls: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    return next((p for p in pulls if _field(p, "headRefName") == branch), None)


def _newest(pipelines: Sequence[PipelineView], branch: str, number: object) -> PipelineView | None:
    pull_refs = {f"refs/pull/{number}/head", f"refs/pull/{number}/merge"} if number is not None else set()
    mine = [p for p in pipelines if p.branch == branch or p.ref in pull_refs]
    return max(mine, key=lambda p: p.run_number, default=None)


def _from_pipeline(pipeline: PipelineView, repo: str, provider: CiProvider) -> tuple[CiState, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The state, workflow names, failed ones and running ones of a pipeline."""
    state = _STATE.get(pipeline.state, CiState.UNAVAILABLE)
    try:
        flows = provider.public_workflows(repo, pipeline.run_number)
    except (HerdrEngineeringError, OSError):
        return state, (), (), ()  # the list says how it went; only the chips are missing
    if not flows:
        return CiState.NOT_STARTED, (), (), ()  # a pipeline without workflows ran nothing
    return (state, tuple(f["name"] for f in flows), tuple(f["name"] for f in flows if f["state"] in _BAD),
            tuple(f["name"] for f in flows if f["state"] == "running"))


def _read(repo: str, provider: CiProvider) -> list[PipelineView] | None:
    try:
        return provider.public_pipelines(repo)
    except (HerdrEngineeringError, OSError):
        return None


_FAILED_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"})
_CONTEXT_KIND = {"SUCCESS": "passed", "FAILURE": "failed", "ERROR": "failed", "PENDING": "running", "EXPECTED": "waiting"}
_WOODPECKER = "ci/woodpecker/"


def _kind(entry: Mapping[str, object]) -> str | None:
    """`passed`, `failed`, `running` or `waiting` (exists, not started) for one rollup entry; None when unreadable."""
    if _field(entry, "context"):
        return _CONTEXT_KIND.get(_field(entry, "state") or "")
    status = _field(entry, "status")
    if status == "COMPLETED":
        conclusion = _field(entry, "conclusion") or ""
        return "failed" if conclusion in _FAILED_CONCLUSIONS else "passed" if conclusion in ("SUCCESS", "NEUTRAL") else "waiting"
    return "running" if status == "IN_PROGRESS" else "waiting" if status else None


def _rollup(pull: Mapping[str, object] | None) -> list[tuple[str, str]]:
    """One (name, kind) per check of the pull request's `statusCheckRollup`, named as the rollup names it. Two checks of
    one name (the same job in two workflows) are told apart by their workflow. A check GitHub does not list is not here."""
    raw = pull.get("statusCheckRollup") if pull else None
    entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    names = [_field(e, "context") or _field(e, "name") or "" for e in entries]
    out: list[tuple[str, str]] = []
    for entry, name in zip(entries, names, strict=True):
        kind = _kind(entry)
        if not name or kind is None:
            continue
        if name.startswith(_WOODPECKER):
            name = name.rsplit("/", 1)[-1]
        elif names.count(name) > 1 and _field(entry, "workflowName"):
            name = f"{name} ({_field(entry, 'workflowName')})"
        out.append((name, kind))
    return out


def _with_rollup(card: CiCard, checks: Sequence[tuple[str, str]]) -> CiCard:
    """The card with the rollup's checks added to its Woodpecker chips (a name Woodpecker already gave is not doubled), and
    its state raised to failed or running by them. Without a Woodpecker read the rollup alone says how the card stands."""
    known = set(card.workflows)
    extra = [(n, k) for n, k in checks if n not in known]
    workflows = (*card.workflows, *dict.fromkeys(n for n, _ in extra))
    failed = (*card.failed, *(n for n, k in extra if k == "failed"))
    running = (*card.running, *(n for n, k in extra if k == "running"))
    waiting = tuple(n for n, k in extra if k == "waiting")
    state = card.state
    if failed:
        state = CiState.FAILED
    elif running:
        state = CiState.RUNNING
    elif state in (CiState.UNAVAILABLE, CiState.NOT_STARTED) and checks:
        state = CiState.PASSED if any(k == "passed" for _, k in checks) else CiState.NOT_STARTED
    return replace(card, state=state, workflows=workflows, failed=failed, running=running, waiting=waiting,
                   reason=f"{failed[0]} failed" if failed else card.reason)


def _card(branch: str, pull: Mapping[str, object] | None, pipelines: Sequence[PipelineView] | None, repo: str,
          provider: CiProvider) -> CiCard:
    card = _pipeline_card(branch, pull, pipelines, repo, provider)
    checks = _rollup(pull)
    return _with_rollup(card, checks) if checks else card


def _pipeline_card(branch: str, pull: Mapping[str, object] | None, pipelines: Sequence[PipelineView] | None, repo: str,
                   provider: CiProvider) -> CiCard:
    number = pull.get("number") if pull else None
    ref, title = (f"#{number}", _field(pull, "title") or branch) if pull and number is not None else (branch, branch)
    url = _field(pull, "url") if pull else None
    if pipelines is None:
        return CiCard(ref, title, CiState.UNAVAILABLE, (), url, branch)
    found = _newest(pipelines, branch, number)
    if found is None:
        return CiCard(ref, title, CiState.NOT_STARTED, (), url, branch)
    state, flows, failed, running = _from_pipeline(found, repo, provider)
    reason = f"{failed[0]} failed" if failed else None
    return CiCard(ref, title, state, flows, url, branch, failed, running, reason)


def build_cards(repo: str, branches: Sequence[str], provider: CiProvider,
                pull_requests: Sequence[Mapping[str, object]] = ()) -> tuple[CiCard, ...]:
    """One card per branch, in the order given. A merged PR is one line and costs no CI read; a closed one is left out.
    Never raises: CI that cannot be read gives `UNAVAILABLE`."""
    pairs = [(b, _pull_for(b, pull_requests)) for b in branches]
    pairs = [(b, p) for b, p in pairs if not (p and _field(p, "state") == "CLOSED" and not _field(p, "mergedAt"))]
    merged = {b for b, p in pairs if p and (_field(p, "state") == "MERGED" or _field(p, "mergedAt"))}
    pipelines = _read(repo, provider) if len(merged) < len(pairs) else None
    cards = []
    for branch, pull in pairs:
        if branch in merged and pull:
            number = pull.get("number")
            cards.append(CiCard(f"#{number}" if number is not None else branch, _field(pull, "title") or branch, CiState.MERGED,
                                (), _field(pull, "url"), branch))
        else:
            cards.append(_card(branch, pull, pipelines, repo, provider))
    return tuple(cards)


def ci_attention(cards: Sequence[CiCard]) -> bool:
    """True when something runs or fails."""
    return any(c.state in (CiState.RUNNING, CiState.FAILED) and not c.stale for c in cards)


_STYLE = {CiState.RUNNING: Style.YELLOW, CiState.PASSED: Style.GREEN, CiState.FAILED: Style.RED, CiState.NOT_STARTED: Style.GRAY,
          CiState.MERGED: Style.GRAY, CiState.UNAVAILABLE: Style.GRAY}
_WORD = {CiState.RUNNING: "running", CiState.PASSED: "ready", CiState.FAILED: "failing", CiState.NOT_STARTED: "not started",
         CiState.UNAVAILABLE: "CI unavailable"}


def _chips(card: CiCard) -> list[Seg]:
    segs: list[Seg] = []
    for name in card.workflows:
        style = (Style.RED if name in card.failed else Style.YELLOW if name in card.running
                 else Style.GRAY if name in card.waiting else Style.GREEN)
        segs += [(" " if segs else "", None), (name, style)]
    return segs


def _counts(cards: Sequence[CiCard]) -> str:
    open_cards = [c for c in cards if c.state is not CiState.MERGED]
    running = sum(c.state is CiState.RUNNING for c in cards)
    failing = sum(c.state is CiState.FAILED for c in cards)
    return f"{len(open_cards)} open · {running} running · {failing} failing"


def lines(cards: Sequence[CiCard], width: int, opts: RenderOpts, now: float | None = None) -> list[str]:
    """One card per open PR or branch; every merged one shares a single line. `now` (epoch seconds) dates "Pushed by"."""
    if not cards:
        return [line([("no CI to show", Style.GRAY)], width, opts)]
    out = [line([(_counts(cards), Style.BOLD)], width, opts)]
    for card in (c for c in cards if c.state is not CiState.MERGED):
        style = _STYLE[card.state]
        out.append(line([("● ", style), (f"{card.ref} {card.title}", Style.WHITE)], width, opts))
        out.append(line([("  ", None), (_WORD[card.state] + (" (stale)" if card.stale else ""), style)], width, opts))
        pushed = [(f"Pushed by #{card.pushed_by[0]}, {duration(now - card.pushed_by[1])} ago", Style.GRAY)] if card.pushed_by and now is not None else []
        for segs in (_chips(card), [(card.reason, Style.RED)] if card.reason else [], pushed):
            if segs:
                out.append(line([("  ", None), *segs], width, opts))
    merged = [c.ref for c in cards if c.state is CiState.MERGED]
    if merged:
        out.append(line([("merged: ", Style.GRAY), (" ".join(merged), Style.GRAY)], width, opts))
    return out
