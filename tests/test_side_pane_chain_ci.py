"""CI cards for the whole chain: every check of the rollup, every branch the chain's records name, who pushed it.

The rollup is the real `gh pr list --json statusCheckRollup` capture (tests/fixtures/side_pane/gh_pr_list_rollup.json); the
Woodpecker reads go to the same recorded-JSON server as tests/test_side_pane_ci.py; the records and events are real files.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from herdr_engineering.agent_ops_tui import RenderOpts
from herdr_engineering.ci import WoodpeckerProvider
from herdr_engineering.handoff_controller.events import EventLog
from herdr_engineering.side_pane.ci_fetch import CiSchedule, CiWorker, branches_to_read
from herdr_engineering.side_pane.ci_tab import CiCard, CiState, build_cards, lines
from herdr_engineering.side_pane.model import build_model, chain_branches
from tests.test_side_pane_ci import REPO, ROUTES, Served, serve
from tests.test_side_pane_model import NOW, ago, event, session_ref, write_record

ROLLUP = json.loads((Path(__file__).parent / "fixtures" / "side_pane" / "gh_pr_list_rollup.json").read_text())


@pytest.fixture
def woodpecker() -> Iterator[Served]:
    yield from serve(ROUTES)


@pytest.fixture
def down() -> WoodpeckerProvider:
    return WoodpeckerProvider(base_url="http://127.0.0.1:9", token=None, allow_loopback_http=True)


def pull(number: int) -> dict[str, object]:
    return next(p for p in ROLLUP if p["number"] == number)


def only(provider: WoodpeckerProvider, number: int) -> CiCard:
    row = pull(number)
    return build_cards(REPO, [str(row["headRefName"])], provider, [row])[0]


def test_every_rollup_check_is_a_chip_by_its_name_and_woodpecker_is_not_doubled(woodpecker: Served) -> None:
    got = only(woodpecker[0], 890)
    assert "GitGuardian Security Checks" in got.workflows and "verify (macos-15)" in got.workflows
    assert got.workflows.count("unit-gates") == 1 and not any(w.startswith("ci/woodpecker") for w in got.workflows)
    assert len(got.workflows) == len(set(got.workflows))


def test_two_checks_with_one_name_are_told_apart_by_their_workflow(down: WoodpeckerProvider) -> None:
    got = only(down, 890)
    assert "verify (Agent communications)" in got.workflows and "verify (Host Cargo cache)" in got.workflows


def test_a_failing_context_fails_the_card_even_when_woodpecker_cannot_be_read(down: WoodpeckerProvider) -> None:
    got = only(down, 889)
    assert got.state is CiState.FAILED and got.failed == ("unit-gates",)
    assert got.reason == "unit-gates failed"  # the first failing check's name; the rollup carries no description


def test_a_pending_context_is_running(down: WoodpeckerProvider) -> None:
    got = only(down, 884)
    assert got.state is CiState.RUNNING and got.running == ("unit-gates",)


def test_a_required_check_that_never_reported_is_not_invented(down: WoodpeckerProvider) -> None:
    got = only(down, 890)  # GitHub lists no ui-gates for this PR
    assert "ui-gates" not in got.workflows and got.state is CiState.PASSED


def test_a_check_that_exists_but_never_started_is_grey(down: WoodpeckerProvider) -> None:
    row = {**pull(890), "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "slow-lane", "status": "QUEUED", "conclusion": "", "workflowName": "CI"},
        {"__typename": "StatusContext", "context": "ci/woodpecker/pr/ui-gates", "state": "EXPECTED"},
        {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "workflowName": "CI"}]}
    got = build_cards(REPO, [str(row["headRefName"])], down, [row])[0]
    assert got.waiting == ("slow-lane", "ui-gates") and got.failed == () and got.running == ()
    shown = "\n".join(lines([got], 80, RenderOpts(color=False)))
    assert "slow-lane" in shown and "ui-gates" in shown


def test_an_error_on_a_review_bot_context_is_a_failed_chip(down: WoodpeckerProvider) -> None:
    row = pull(862)
    cards = build_cards(REPO, ["feat/pretooluse-guard-hooks"], down, [{**row, "state": "OPEN", "mergedAt": None}])
    assert "Independent AI review" in cards[0].failed and cards[0].state is CiState.FAILED


def test_the_failure_reason_is_shown_on_its_own_line(down: WoodpeckerProvider) -> None:
    shown = "\n".join(lines([only(down, 889)], 80, RenderOpts(color=False)))
    assert "unit-gates failed" in shown and "failing" in shown


# --- the chain's branches ----------------------------------------------------------------------------------------

def test_chain_branches_are_the_records_branches_in_this_repo(tmp_path: Path) -> None:
    write_record(tmp_path, 1, succ=2, pane="p-1", session="s1", branch="feat/a", repo=REPO, child="p-2", child_session="s2")
    write_record(tmp_path, 2, pred=1, pane="p-2", session="s2", branch="feat/b", child="p-3", child_session="s3")
    write_record(tmp_path, 9, pane="p-9", session="s9", branch="feat/other-chain", repo=REPO)
    got = chain_branches(session_ref("p-3", "s3"), tmp_path)
    assert got == ((REPO, "feat/a"), (None, "feat/b"))


def test_branches_to_read_skips_other_repositories_main_and_repeats() -> None:
    chain = ((REPO, "feat/a"), (None, "feat/b"), ("acme/else", "feat/c"), (REPO, "main"), (REPO, "feat/a"), (REPO, "now"))
    assert branches_to_read(REPO, "now", chain) == ["now", "feat/a", "feat/b"]


def test_cards_cover_every_branch_not_just_the_current_one(woodpecker: Served) -> None:
    pulls = [pull(890), pull(889), pull(884)]
    cards = build_cards(REPO, ["feat/handoff-harness-adapters", "fix/scratch-reaper-cargo-1-100", "feat/automatic-handoff-triggers"],
                        woodpecker[0], pulls)
    assert [c.ref for c in cards] == ["#890", "#889", "#884"]


def test_the_schedule_reads_again_at_once_when_the_branches_change() -> None:
    seen: list[tuple[Path, tuple[str, ...]]] = []
    worker = CiWorker(lambda cwd, branches=(): seen.append((cwd, branches)) or (), clock=time.monotonic)  # type: ignore[func-returns-value]
    schedule = CiSchedule(worker, interval_s=60.0)
    assert schedule.maybe_request(Path("/w"), 0.0, ("a",))
    deadline = time.monotonic() + 5
    while worker.latest() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not schedule.maybe_request(Path("/w"), 10.0, ("a",))
    assert schedule.maybe_request(Path("/w"), 11.0, ("a", "b"))
    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [(Path("/w"), ("a",)), (Path("/w"), ("a", "b"))]


# --- pushed by ---------------------------------------------------------------------------------------------------

def _pushed_store(store: Path, *, with_commit: bool) -> None:
    write_record(store, 1, succ=2, pane="p-1", session="s1", branch="feat/a", child="p-2", child_session="s2", created=ago(300))
    write_record(store, 2, pred=1, pane="p-2", session="s2", branch="feat/b", child="p-3", child_session="s3", created=ago(150))
    log = EventLog(store)
    log.append(event("handoff.triggered", chain="c2", pane="p-2", session="s2", at=ago(160), pct=82.0))
    log.append(event("handoff.wip_committed", chain="c2", pane="p-2", session="s2", at=ago(155),
                     commit="abc1234" if with_commit else None))


def _cards() -> tuple[CiCard, ...]:
    return (CiCard("#2", "second", CiState.PASSED, (), None, "feat/b"), CiCard("#1", "first", CiState.PASSED, (), None, "feat/a"))


def test_a_card_says_which_session_pushed_its_branch_and_when(tmp_path: Path) -> None:
    _pushed_store(tmp_path, with_commit=True)
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW, _cards())
    by_branch = {c.branch: c for c in model.ci}
    assert by_branch["feat/b"].pushed_by == (2, NOW - 155 * 60)  # session #2 of the chain, at the checkpoint's time
    assert by_branch["feat/a"].pushed_by is None  # no checkpoint was recorded for session #1: blank, not guessed
    shown = "\n".join(lines(list(model.ci), 80, RenderOpts(color=False), NOW))
    assert "Pushed by #2, 2h 35m ago" in shown and shown.count("Pushed by") == 1


def test_pushed_by_is_left_out_when_the_event_has_no_commit(tmp_path: Path) -> None:
    _pushed_store(tmp_path, with_commit=False)
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW, _cards())
    assert all(c.pushed_by is None for c in model.ci)
