"""CI cards against a real local HTTP server that serves RECORDED Woodpecker JSON (no token, no network).

The JSON is captured from the public API of ci.metabolomics.us and from `gh pr list`; see
tests/fixtures/side_pane/woodpecker/README.md for the URLs, the date and the one derived file.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from herdr_engineering.ci import WoodpeckerProvider
from herdr_engineering.side_pane.ci_tab import CiCard, CiState, build_cards, ci_attention

SIDE = Path(__file__).parent / "fixtures" / "side_pane"
RECORDED = SIDE / "woodpecker"
PULLS = json.loads((SIDE / "gh_pr_list.json").read_text())
REPO = "berlinguyinca/herdr-engineering"
ROUTES = {f"/api/repos/lookup/{REPO}": "lookup.json", "/api/repos/120/pipelines": "pipelines.json",
          **{f"/api/repos/120/pipelines/{n}": f"pipeline_{n}.json" for n in (1722, 1728, 1741, 1745, 1746, 1750, 1751)}}
Served = tuple[WoodpeckerProvider, list[str]]


def serve(routes: dict[str, str]) -> Iterator[Served]:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.path)
            name = routes.get(self.path)
            body = (RECORDED / name).read_bytes() if name else b'{"error": "not found"}'
            self.send_response(200 if name else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield WoodpeckerProvider(base_url=base, token=None, allow_loopback_http=True), seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def woodpecker() -> Iterator[Served]:
    yield from serve(ROUTES)


@pytest.fixture
def woodpecker_without_workflows() -> Iterator[Served]:
    yield from serve({**ROUTES, "/api/repos/120/pipelines/1751": "pipeline_1751_no_workflows.json"})


def card(cards: tuple[CiCard, ...], ref: str) -> CiCard:
    return next(c for c in cards if c.ref == ref)


def cards_for(provider: WoodpeckerProvider, *branches: str, pulls: list[dict[str, object]] | None = None) -> tuple[CiCard, ...]:
    return build_cards(REPO, list(branches), provider, PULLS if pulls is None else pulls)


def test_running_pipeline_card(woodpecker: Served) -> None:
    got = card(cards_for(woodpecker[0], "fix/unit-gates-worker-crash-rerun"), "#888")
    assert got.state is CiState.RUNNING  # a pull-request pipeline: branch "main", ref refs/pull/888/merge
    assert got.title == "fix(ci): rerun the parallel pytest pass once when xdist workers die"
    assert got.workflows == ("artifact-handoff", "structural-gates", "ui-gates", "unit-gates")
    assert got.running == ("unit-gates",)
    assert got.url == "https://github.com/berlinguyinca/herdr-engineering/pull/888"
    assert got.branch == "fix/unit-gates-worker-crash-rerun"


def test_failed_workflow_listed(woodpecker: Served) -> None:
    got = card(cards_for(woodpecker[0], "feat/automatic-handoff-triggers"), "#884")
    assert got.state is CiState.FAILED
    assert got.failed == ("unit-gates",)
    assert got.reason == "unit-gates failed"


def test_passed_pipeline_is_ready(woodpecker: Served) -> None:
    got = card(cards_for(woodpecker[0], "feat/handoff-harness-adapters"), "#890")
    assert got.state is CiState.PASSED and got.failed == () and got.running == ()


def test_merged_pr_collapses_to_one_line(woodpecker: Served) -> None:
    provider, seen = woodpecker
    got = card(cards_for(provider, "fix/cli-cwd-and-tailscale-doctor"), "#891")
    assert got.state is CiState.MERGED
    assert got.workflows == () and got.failed == ()
    assert seen == []  # a merged PR needs no CI read


def test_no_pipeline_is_not_started(woodpecker: Served) -> None:
    got = cards_for(woodpecker[0], "feat/never-pushed")[0]
    assert (got.ref, got.state, got.workflows) == ("feat/never-pushed", CiState.NOT_STARTED, ())


def test_zero_workflows_is_not_started_not_an_error(woodpecker_without_workflows: Served) -> None:
    got = card(cards_for(woodpecker_without_workflows[0], "fix/unit-gates-worker-crash-rerun"), "#888")
    assert got.state is CiState.NOT_STARTED


def test_provider_down_is_unavailable() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()  # nothing listens on the port any more
    provider = WoodpeckerProvider(base_url=f"http://127.0.0.1:{port}", token=None, allow_loopback_http=True)
    cards = cards_for(provider, "fix/unit-gates-worker-crash-rerun", "fix/cli-cwd-and-tailscale-doctor")
    assert {c.ref: c.state for c in cards} == {"#888": CiState.UNAVAILABLE, "#891": CiState.MERGED}


def test_unsafe_ci_address_and_unknown_repository_are_unavailable(woodpecker: Served) -> None:
    provider, _ = woodpecker
    branch = "fix/unit-gates-worker-crash-rerun"
    assert build_cards("acme/other", [branch], provider, PULLS)[0].state is CiState.UNAVAILABLE  # 404 on lookup
    assert build_cards("not a repo", [branch], provider, PULLS)[0].state is CiState.UNAVAILABLE
    plain_http = WoodpeckerProvider(base_url="http://127.0.0.1:9", token=None)  # http is not a safe place to read from
    assert cards_for(plain_http, branch)[0].state is CiState.UNAVAILABLE


def test_branch_without_pull_request_shows_the_branch(woodpecker: Served) -> None:
    got = cards_for(woodpecker[0], "main", pulls=[])[0]
    assert (got.ref, got.title, got.url) == ("main", "main", None)
    assert got.state is CiState.RUNNING  # newest pipeline recorded for branch main (1751)


def test_closed_pr_is_left_out_and_latest_pipeline_wins(woodpecker: Served) -> None:
    provider, seen = woodpecker
    cards = cards_for(provider, "feat/rakazo-default-assistant", "feat/automatic-handoff-triggers")
    assert [c.ref for c in cards] == ["#884"]
    assert "/api/repos/120/pipelines/1746" in seen and "/api/repos/120/pipelines/1745" not in seen  # newest of #884, not older


def test_attention_when_something_runs_or_fails(woodpecker: Served) -> None:
    provider, _ = woodpecker
    assert ci_attention(cards_for(provider, "fix/unit-gates-worker-crash-rerun"))
    assert ci_attention(cards_for(provider, "feat/automatic-handoff-triggers"))
    assert not ci_attention(cards_for(provider, "fix/cli-cwd-and-tailscale-doctor", "feat/handoff-harness-adapters", "feat/never-pushed"))
    assert not ci_attention(())


def test_merged_card_without_a_number_shows_the_branch_not_none() -> None:
    row = {"number": None, "title": "", "headRefName": "feat/x", "state": "MERGED", "mergedAt": "2026-11-18T09:00:00Z", "url": None}
    got = build_cards(REPO, ["feat/x"], WoodpeckerProvider(base_url="https://127.0.0.1:9", token=None), [row])[0]
    assert got.state is CiState.MERGED and got.ref == "feat/x" and "None" not in got.ref + got.title
