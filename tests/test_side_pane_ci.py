"""CI cards against a real local HTTP server that serves recorded Woodpecker JSON (no token, no network)."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from herdr_engineering.ci import WoodpeckerProvider
from herdr_engineering.side_pane.ci_tab import CiCard, CiState, build_cards, ci_attention

FIXTURES = Path(__file__).parent / "fixtures"
PULLS = json.loads((FIXTURES / "side_pane" / "gh_pr_list.json").read_text())
ROUTES = {"/api/repos/lookup/acme/widgets": "lookup.json", "/api/repos/120/pipelines": "pipelines.json",
          **{f"/api/repos/120/pipelines/{n}": f"pipeline_{n}.json" for n in (8, 9, 10, 11, 12)}}


@pytest.fixture
def woodpecker() -> Iterator[tuple[WoodpeckerProvider, list[str]]]:
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.path)
            name = ROUTES.get(self.path)
            body = (FIXTURES / "woodpecker" / name).read_bytes() if name else b'{"error": "not found"}'
            self.send_response(200 if name else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield WoodpeckerProvider(base_url=base, token=None, allow_loopback_http=True), seen
    server.shutdown()
    server.server_close()


def card(cards: tuple[CiCard, ...], ref: str) -> CiCard:
    return next(c for c in cards if c.ref == ref)


def test_running_pipeline_card(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, _ = woodpecker
    got = card(build_cards("acme/widgets", ["feat/running"], provider, PULLS), "#44")
    assert got.state is CiState.RUNNING
    assert got.title == "box size + Copy as CSS"
    assert got.workflows == ("woodpecker", "verify")
    assert got.running == ("woodpecker",)
    assert got.url == "https://github.com/acme/widgets/pull/44"
    assert got.branch == "feat/running"


def test_failed_workflow_listed(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, _ = woodpecker
    got = card(build_cards("acme/widgets", ["feat/failed"], provider, PULLS), "#45")
    assert got.state is CiState.FAILED
    assert got.failed == ("unit-gates",)
    assert got.workflows == ("woodpecker", "unit-gates")
    assert got.reason == "unit-gates failed"


def test_merged_pr_collapses_to_one_line(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, seen = woodpecker
    got = card(build_cards("acme/widgets", ["feat/merged"], provider, PULLS), "#39")
    assert got.state is CiState.MERGED
    assert got.workflows == () and got.failed == ()
    assert seen == []  # a merged PR needs no CI read


def test_no_pipeline_is_not_started(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, _ = woodpecker
    got = card(build_cards("acme/widgets", ["feat/none"], provider, PULLS), "#51")
    assert got.state is CiState.NOT_STARTED
    assert got.workflows == ()


def test_zero_workflows_is_not_started_not_an_error(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, _ = woodpecker
    assert card(build_cards("acme/widgets", ["feat/zero"], provider, PULLS), "#50").state is CiState.NOT_STARTED


def test_provider_down_is_unavailable() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()  # nothing listens on the port any more
    provider = WoodpeckerProvider(base_url=f"http://127.0.0.1:{port}", token=None, allow_loopback_http=True)
    cards = build_cards("acme/widgets", ["feat/running", "feat/merged"], provider, PULLS)
    assert {c.ref: c.state for c in cards} == {"#44": CiState.UNAVAILABLE, "#39": CiState.MERGED}


def test_unsafe_ci_address_and_unknown_repository_are_unavailable(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, _ = woodpecker
    assert build_cards("acme/other", ["feat/running"], provider, PULLS)[0].state is CiState.UNAVAILABLE  # 404 on lookup
    assert build_cards("not a repo", ["feat/running"], provider, PULLS)[0].state is CiState.UNAVAILABLE
    plain_http = WoodpeckerProvider(base_url="http://127.0.0.1:9", token=None)  # http is not a safe place to read from
    assert build_cards("acme/widgets", ["feat/running"], plain_http, PULLS)[0].state is CiState.UNAVAILABLE


def test_branch_without_pull_request_shows_the_branch(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, _ = woodpecker
    got = build_cards("acme/widgets", ["feat/running"], provider, [])[0]
    assert (got.ref, got.title, got.state, got.url) == ("feat/running", "feat/running", CiState.RUNNING, None)


def test_closed_pr_is_left_out_and_latest_pipeline_wins(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, seen = woodpecker
    cards = build_cards("acme/widgets", ["feat/closed", "feat/running"], provider, PULLS)
    assert [c.ref for c in cards] == ["#44"]
    assert "/api/repos/120/pipelines/12" in seen and "/api/repos/120/pipelines/9" not in seen  # #12, not the older #9


def test_attention_when_something_runs_or_fails(woodpecker: tuple[WoodpeckerProvider, list[str]]) -> None:
    provider, _ = woodpecker
    assert ci_attention(build_cards("acme/widgets", ["feat/running"], provider, PULLS))
    assert ci_attention(build_cards("acme/widgets", ["feat/failed"], provider, PULLS))
    assert not ci_attention(build_cards("acme/widgets", ["feat/merged", "feat/none", "feat/zero"], provider, PULLS))
    assert not ci_attention(())
