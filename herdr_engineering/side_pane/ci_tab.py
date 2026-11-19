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
from .style import Seg, Style, clean, duration, line


class CiProvider(Protocol):
    def public_pipelines(self, repository: str) -> list[PipelineView] | None: ...
    def public_workflows(self, repository: str, number: int) -> list[dict[str, str]] | None: ...


class CiState(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    NOT_STARTED = "not_started"  # nothing ran: shown grey, not red
    ATTENTION = "attention"  # a check was cancelled, skipped or is in a state this pane does not know: never "ready"
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
    other: tuple[str, ...] = ()  # checks that ended or stand in a state that is neither pass nor fail: grey, with the state named
    committed_by: tuple[int, float, bool] | None = None  # (session number in the chain, epoch seconds, pushed) of the checkpoint commit
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
_QUEUED = frozenset({"QUEUED", "PENDING", "WAITING", "REQUESTED"})
_STATE_CUT = 20
_WOODPECKER = "ci/woodpecker/"


def _kind(entry: Mapping[str, object]) -> tuple[str, str]:
    """(kind, state) of one rollup entry: kind is `passed`, `failed`, `running`, `waiting` (exists, not started) or `other`
    (cancelled, skipped, or a state this pane does not know); state is the raw word GitHub gave, for the chip of an `other`."""
    if _field(entry, "context"):
        state = _field(entry, "state") or ""
        return _CONTEXT_KIND.get(state, "other"), state
    status = _field(entry, "status") or ""
    if status == "COMPLETED":
        conclusion = _field(entry, "conclusion") or ""
        kind = "failed" if conclusion in _FAILED_CONCLUSIONS else "passed" if conclusion in ("SUCCESS", "NEUTRAL") else "other"
        return kind, conclusion
    return ("running" if status == "IN_PROGRESS" else "waiting" if status in _QUEUED else "other"), status


def _said(state: str) -> str:
    """A raw state as a few lower-case words: `STALE`, `TIMED_OUT` -> `stale`, `timed out`; never an escape sequence."""
    words = " ".join("".join(c if c.isalnum() else " " for c in clean(state).lower()).split())
    return (words[:_STATE_CUT] or "unknown").strip()


def _rollup(pull: Mapping[str, object] | None) -> list[tuple[str, str]]:
    """One (name, kind) per check of the pull request's `statusCheckRollup`, named as the rollup names it. Two checks of
    one name (the same job in two workflows) are told apart by their workflow. A check GitHub does not list is not here."""
    raw = pull.get("statusCheckRollup") if pull else None
    entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    names = [_field(e, "context") or _field(e, "name") or "" for e in entries]
    out: list[tuple[str, str]] = []
    for entry, name in zip(entries, names, strict=True):
        kind, raw = _kind(entry)
        if not name:
            continue
        if name.startswith(_WOODPECKER):
            name = name.rsplit("/", 1)[-1]
        elif names.count(name) > 1 and _field(entry, "workflowName"):
            name = f"{name} ({_field(entry, 'workflowName')})"
        out.append((f"{name} ({_said(raw)})" if kind == "other" else name, kind))
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
    other = tuple(n for n, k in extra if k == "other")
    state = card.state
    if failed:
        state = CiState.FAILED
    elif running or waiting:
        state = CiState.RUNNING
    elif other and state is not CiState.FAILED:
        state = CiState.ATTENTION  # ready needs every known check to have passed
    elif state in (CiState.UNAVAILABLE, CiState.NOT_STARTED) and checks:
        state = CiState.PASSED if any(k == "passed" for _, k in checks) else CiState.NOT_STARTED
    return replace(card, state=state, workflows=workflows, failed=failed, running=running, waiting=waiting, other=other,
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
    return any(c.state in (CiState.RUNNING, CiState.FAILED, CiState.ATTENTION) and not c.stale for c in cards)


_STYLE = {CiState.RUNNING: Style.YELLOW, CiState.ATTENTION: Style.YELLOW, CiState.PASSED: Style.GREEN, CiState.FAILED: Style.RED, CiState.NOT_STARTED: Style.GRAY,
          CiState.MERGED: Style.GRAY, CiState.UNAVAILABLE: Style.GRAY}
_WORD = {CiState.RUNNING: "running", CiState.ATTENTION: "needs attention", CiState.PASSED: "ready", CiState.FAILED: "failing", CiState.NOT_STARTED: "not started",
         CiState.UNAVAILABLE: "CI unavailable"}


def _chips(card: CiCard) -> list[Seg]:
    segs: list[Seg] = []
    for name in card.workflows:
        style = (Style.RED if name in card.failed else Style.YELLOW if name in card.running
                 else Style.GRAY if name in card.waiting or name in card.other else Style.GREEN)
        segs += [(" " if segs else "", None), (name, style)]
    return segs


def _counts(cards: Sequence[CiCard]) -> str:
    open_cards = [c for c in cards if c.state is not CiState.MERGED]
    running = sum(c.state is CiState.RUNNING for c in cards)
    failing = sum(c.state is CiState.FAILED for c in cards)
    return f"{len(open_cards)} open · {running} running · {failing} failing"


def _committed(committed_by: tuple[int, float, bool], now: float) -> str:
    number, at, pushed = committed_by
    return f"committed by #{number}, {duration(now - at)} ago" + ("" if pushed else ", not pushed")


def lines(cards: Sequence[CiCard], width: int, opts: RenderOpts, now: float | None = None) -> list[str]:
    """One card per open PR or branch; every merged one shares a single line. `now` (epoch seconds) dates "committed by"."""
    if not cards:
        return [line([("no CI to show", Style.GRAY)], width, opts)]
    out = [line([(_counts(cards), Style.BOLD)], width, opts)]
    for card in (c for c in cards if c.state is not CiState.MERGED):
        style = _STYLE[card.state]
        out.append(line([("● ", style), (f"{card.ref} {card.title}", Style.WHITE)], width, opts))
        out.append(line([("  ", None), (_WORD[card.state] + (" (stale)" if card.stale else ""), style)], width, opts))
        made = [(_committed(card.committed_by, now), Style.GRAY)] if card.committed_by and now is not None else []
        for segs in (_chips(card), [(card.reason, Style.RED)] if card.reason else [], made):
            if segs:
                out.append(line([("  ", None), *segs], width, opts))
    merged = [c.ref for c in cards if c.state is CiState.MERGED]
    if merged:
        out.append(line([("merged: ", Style.GRAY), (" ".join(merged), Style.GRAY)], width, opts))
    return out
