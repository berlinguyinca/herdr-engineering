"""ai-sync fabric status feed: read-only, bounded, secret-free.

Parsing tests use output captured from the real tool in a throwaway HOME (tests/fixtures/ai_sync). The collection
tests run the real ``ai-sync`` binary when installed, only ever against a throwaway HOME inside ``tmp_path``.
"""
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import herdr_engineering.ai_sync_feed as feed
from herdr_engineering.ai_sync_feed import (
    AiSyncError,
    AiSyncFeed,
    collect_status,
    parse_env_list,
    parse_status,
    parse_verify,
    run_ai_sync,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ai_sync"
PAYLOAD_KEYS = {"host_id", "ai_sync_version", "envs_enabled", "last_sync_ts", "ahead", "behind", "branch",
                "drifted_files_count", "drift_sample", "skipped_links_count", "last_error", "checked_at", "verify_ok", "verify_errors_count",
                "verify_warnings_count", "verify_checked", "verify_error_sample", "verify_checked_at"}


def fixture(name):
    return (FIXTURES / name).read_text()


def script(tmp_path, body, name="ai-sync"):
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_parse_clean_status_has_no_drift():
    parsed = parse_status(fixture("status-clean.txt"))
    assert parsed.drifted_paths == ["claude/commands/sync.md"]  # the real tool reports its own command as local-only
    assert parsed.skipped_links_count == 0


def test_parse_drift_status_lists_paths_only():
    parsed = parse_status(fixture("status-drift.txt"))
    assert parsed.drifted_paths == ["claude/CLAUDE.md", "claude/commands/sync.md"]


def test_parse_status_counts_env_counters_when_no_path_list():
    parsed = parse_status("Env claude: local-only=2 remote-only=3 both-changed=1 staged-pending=0\n")
    assert parsed.drifted_paths == [] and parsed.drifted_count == 6


def test_parse_status_with_remote_ahead_line_still_parses_paths():
    parsed = parse_status(fixture("status-behind.txt"))
    assert parsed.drifted_paths == ["claude/CLAUDE.md", "claude/commands/sync.md"]


def test_parse_status_skipped_links_are_counted_defensively():
    text = "Skipped 3 links (target outside sync root)\nsomething skipped\nExcluded: 4 files (not in sync manifest)\n"
    assert parse_status(text).skipped_links_count == 4


def test_parse_status_survives_garbage():
    parsed = parse_status("\x00\x01 not ai-sync output\n" * 50)
    assert parsed.drifted_paths == [] and parsed.drifted_count == 0


def test_parse_env_list_returns_enabled_ids():
    assert parse_env_list(fixture("env-list.txt")) == ["claude", "pi"]


def test_only_read_only_subcommands_may_run(tmp_path):
    binary = script(tmp_path, "echo ran")
    for forbidden in (("pull",), ("push",), ("init",), ("link",), ("update",), ("status", "--summarize"), ("env", "enable", "x")):
        with pytest.raises(ValueError):
            run_ai_sync(binary, forbidden, timeout=2, home=tmp_path)
    assert run_ai_sync(binary, ("--version",), timeout=2, home=tmp_path).strip() == "ran"


def test_every_invocation_disables_the_updater(tmp_path):
    binary = script(tmp_path, 'echo "$@"')
    assert run_ai_sync(binary, ("status",), timeout=2, home=tmp_path).split() == ["--no-update-check", "status"]


def test_run_ai_sync_times_out_hard(tmp_path):
    binary = script(tmp_path, "sleep 30")
    started = time.monotonic()
    with pytest.raises(AiSyncError, match="timed out"):
        run_ai_sync(binary, ("status",), timeout=0.5, home=tmp_path)
    assert time.monotonic() - started < 5


def test_error_text_is_redacted(tmp_path):
    binary = script(tmp_path, "echo 'fatal: https://alice:ghp_0123456789abcdef0123456789abcdef0123@github.com/x/y.git "
                              "AKIAABCDEFGHIJKLMNOP0123456789abcdefABCD' >&2; exit 3")
    with pytest.raises(AiSyncError) as raised:
        run_ai_sync(binary, ("status",), timeout=2, home=tmp_path)
    message = str(raised.value)
    assert "alice" not in message and "ghp_" not in message and "AKIA" not in message
    assert "exited 3" in message


def make_repo(tmp_path):
    """A throwaway sync repo with an upstream, entirely inside tmp_path."""
    home = tmp_path / "home"
    repo = home / ".ai-sync"
    remote = tmp_path / "remote.git"
    git_env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, env=git_env, check=True, capture_output=True)

    home.mkdir()
    git("init", "-q", "--bare", "-b", "main", str(remote))
    git("init", "-q", "-b", "main", str(repo))
    (repo / "claude").mkdir()
    (repo / "claude" / "CLAUDE.md").write_text("SECRET-CONTENT-MARKER\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-q", "-u", "origin", "main", cwd=repo)
    return home, repo, remote, git, git_env


def test_collect_reads_git_state_without_a_binary(tmp_path):
    home, repo, _, git, _ = make_repo(tmp_path)
    (repo / "x.txt").write_text("a")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "ahead", cwd=repo)
    payload = collect_status("h1", binary=str(tmp_path / "missing"), home=home)
    assert set(payload) == PAYLOAD_KEYS
    assert (payload["ahead"], payload["behind"], payload["branch"]) == (1, 0, "main")
    assert payload["ai_sync_version"] is None and "not installed" in payload["last_error"]
    assert payload["last_sync_ts"].endswith("+00:00")


def test_collect_with_no_repo_reports_error_not_exception(tmp_path):
    payload = collect_status("h1", binary=str(tmp_path / "missing"), home=tmp_path)
    assert payload["ahead"] is None and payload["behind"] is None and payload["last_sync_ts"] is None
    assert payload["last_error"]


@pytest.mark.skipif(shutil.which("ai-sync") is None, reason="ai-sync not installed")
def test_collect_with_real_ai_sync_in_throwaway_home(tmp_path):
    home, repo, _, git, git_env = make_repo(tmp_path)
    assert str(home).startswith(str(tmp_path)) and home != Path.home()
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("SECRET-CONTENT-MARKER changed locally\n")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, env=git_env).stdout
    before = {p: p.read_bytes() for p in (home / ".claude").rglob("*") if p.is_file()}
    payload = collect_status("h1", binary=shutil.which("ai-sync"), home=home, timeout=30)
    assert set(payload) == PAYLOAD_KEYS
    assert payload["ai_sync_version"] and payload["last_error"] is None
    assert "claude" in payload["envs_enabled"]
    assert (payload["ahead"], payload["behind"]) == (0, 0)
    assert "SECRET-CONTENT-MARKER" not in json.dumps(payload)
    # strictly read-only: HEAD and the local config files are untouched
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, env=git_env).stdout == head
    assert {p: p.read_bytes() for p in (home / ".claude").rglob("*") if p.is_file()} == before


def test_drift_sample_is_bounded_and_paths_only(tmp_path):
    changes = "".join(f"  M claude/file-{n}.md\n" for n in range(45))
    (tmp_path / "status.txt").write_text("Local changes:\n" + changes)
    binary = script(tmp_path, f'''case "$2" in
  --version) echo 1.2.3;;
  status) cat "{tmp_path}/status.txt";;
  env) printf "  claude       Claude Code      enabled\\n";;
esac''')
    payload = collect_status("h1", binary=binary, home=tmp_path)
    assert payload["drifted_files_count"] == 45 and len(payload["drift_sample"]) == 20
    assert payload["ai_sync_version"] == "1.2.3" and payload["envs_enabled"] == ["claude"]
    assert all(isinstance(p, str) and p.startswith("claude/") for p in payload["drift_sample"])


class Capture(BaseHTTPRequestHandler):
    posts: list = []
    status = 200

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Capture.posts.append((self.path, self.headers.get("X-API-Key"), body))
        self.send_response(Capture.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": Capture.status == 200,
                                     "entity": {"entity_id": body["entity_id"]}}).encode())

    def log_message(self, *_):
        pass


@pytest.fixture
def cp():
    Capture.posts, Capture.status = [], 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_run_once_posts_an_ai_sync_envelope_with_api_key(tmp_path, cp):
    home, *_ = make_repo(tmp_path)
    ticks = [0.0]
    agent = AiSyncFeed(feed.MetadataClient(cp, "k-test"), host_id="h1", binary=str(tmp_path / "missing"),
                       home=home, clock=lambda: ticks[0])
    summary = agent.run_once()
    assert summary["posted"] == 1 and summary["errors"] == 0
    path, key, body = Capture.posts[0]
    assert (path, key) == ("/api/v1/ingest", "k-test")
    assert (body["entity_type"], body["entity_id"], body["event_type"]) == ("ai_sync", "h1", "status")
    assert set(body["payload"]) == PAYLOAD_KEYS
    # unchanged state is not re-posted every cycle, only refreshed periodically
    ticks[0] = 60
    assert agent.run_once()["unchanged"] == 1 and len(Capture.posts) == 1
    ticks[0] = 700
    assert agent.run_once()["posted"] == 1 and len(Capture.posts) == 2


def test_run_once_counts_a_rejected_post(tmp_path, cp):
    Capture.status = 400
    agent = AiSyncFeed(feed.MetadataClient(cp, "k"), host_id="h1", binary=str(tmp_path / "x"), home=tmp_path)
    assert agent.run_once()["errors"] == 1


def test_dry_run_posts_nothing(tmp_path, cp):
    agent = AiSyncFeed(feed.MetadataClient(cp, "k"), host_id="h1", binary=str(tmp_path / "x"), home=tmp_path)
    assert agent.run_once(dry_run=True)["posted"] == 0 and Capture.posts == []


def test_allowed_commands_are_read_only():
    assert feed.ALLOWED_COMMANDS == frozenset({("--version",), ("status",), ("env", "list"), ("verify", "--json")})


def test_verify_is_allowed_with_the_global_flag_before_it_and_pull_push_stay_refused(tmp_path):
    binary = script(tmp_path, 'echo "$@"')
    assert run_ai_sync(binary, ("verify", "--json"), timeout=2, home=tmp_path).split() == [
        "--no-update-check", "verify", "--json"]
    for forbidden in (("pull",), ("push",), ("verify",), ("verify", "--fix"), ("verify", "--json", "--fix")):
        with pytest.raises(ValueError):
            run_ai_sync(binary, forbidden, timeout=2, home=tmp_path)


def test_parse_verify_ok_fixture():
    v = parse_verify(fixture("verify-ok.json"))
    assert (v.ok, v.errors_count, v.warnings_count) == (True, 0, 0)
    assert v.checked == {"skills": 12, "agents": 4, "commands": 9, "hooks": 3} and v.error_sample == []


def test_parse_verify_errors_fixture_builds_path_problem_sample():
    v = parse_verify(fixture("verify-errors.json"))
    assert (v.ok, v.errors_count, v.warnings_count) == (False, 2, 1)
    assert v.error_sample == ["skills/x/SKILL.md: missing frontmatter field: description", "settings.json: invalid JSON at line 4"]


def test_parse_verify_truncated_fixture_is_bounded_to_ten():
    v = parse_verify(fixture("verify-truncated.json"))
    assert v.ok is False and v.errors_count == 30 and len(v.error_sample) == 10


def test_parse_verify_multi_env_aggregates_all_envs():
    v = parse_verify(fixture("verify-multi-env.json"))
    assert (v.ok, v.errors_count, v.warnings_count) == (False, 1, 1)
    assert v.checked == {"skills": 14, "agents": 4, "commands": 10, "hooks": 3}
    assert v.error_sample == ["pi: skills/y/SKILL.md: unreadable"]


def test_parse_verify_redacts_and_bounds_samples():
    secret = "https://alice:pw@github.com/x " + "A" * 40
    doc = json.dumps({"ok": False, "checked": {}, "errors": [{"path": "a" * 150, "problem": secret + "z" * 300}]})
    v = parse_verify(doc)
    assert len(v.error_sample[0]) <= 200 and "alice" not in v.error_sample[0] and "AAAAAAAA" not in v.error_sample[0]


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", '{"ok":"yes"}', "{}", '{"ok":true,"envs":"x"}',
                                  fixture("verify-unknown-command.txt")])
def test_parse_verify_rejects_garbage_without_raising(text):
    assert parse_verify(text) is None


def test_parse_verify_tolerates_wrong_typed_members():
    doc = '{"ok":false,"checked":{"skills":"x","agents":-1,"commands":true,"hooks":2},"errors":[1,{"path":3},{"path":"p","problem":"q"}],"warnings":"w"}'
    v = parse_verify(doc)
    assert v.checked == {"skills": None, "agents": None, "commands": None, "hooks": 2}
    assert v.error_sample == ["p: q"] and v.warnings_count == 0


def verify_binary(tmp_path, verify_body):
    return script(tmp_path, f"""case "$2" in
  --version) echo 1.2.3;;
  status) echo clean;;
  env) printf "  claude       Claude Code      enabled\\n";;
  verify) {verify_body};;
esac""")


def test_collect_records_verify_from_exit_code_one_json(tmp_path):
    binary = verify_binary(tmp_path, f'cat "{FIXTURES}/verify-errors.json"; exit 1')
    payload = collect_status("h1", binary=binary, home=tmp_path)
    assert payload["verify_ok"] is False and payload["verify_errors_count"] == 2 and payload["verify_warnings_count"] == 1
    assert payload["verify_checked"] == {"skills": 12, "agents": 4, "commands": 9, "hooks": 3}
    assert payload["verify_checked_at"].endswith("+00:00")
    assert payload["last_error"] is None or "verify" not in payload["last_error"]


def test_collect_with_ok_verify(tmp_path):
    binary = verify_binary(tmp_path, f'cat "{FIXTURES}/verify-ok.json"')
    payload = collect_status("h1", binary=binary, home=tmp_path)
    assert payload["verify_ok"] is True and payload["verify_error_sample"] == []


def test_older_ai_sync_without_verify_yields_null_fields_and_no_error(tmp_path):
    binary = verify_binary(tmp_path, f'cat "{FIXTURES}/verify-unknown-command.txt" >&2; exit 1')
    payload = collect_status("h1", binary=binary, home=tmp_path)
    assert set(payload) == PAYLOAD_KEYS
    for key in (k for k in PAYLOAD_KEYS if k.startswith("verify_")):
        assert payload[key] is None, key
    assert "verify" not in (payload["last_error"] or "")


def test_verify_failure_exit_two_is_reported_once_and_leaves_fields_null(tmp_path):
    binary = verify_binary(tmp_path, "echo boom >&2; exit 2")
    payload = collect_status("h1", binary=binary, home=tmp_path)
    assert payload["verify_ok"] is None and "verify" in payload["last_error"] and "exited 2" in payload["last_error"]


def test_verify_garbage_output_is_not_trusted(tmp_path):
    binary = verify_binary(tmp_path, "echo '<html>nope</html>'")
    payload = collect_status("h1", binary=binary, home=tmp_path)
    assert payload["verify_ok"] is None and payload["verify_error_sample"] is None


def test_verify_timeout_is_bounded(tmp_path):
    binary = verify_binary(tmp_path, "sleep 30")
    started = time.monotonic()
    payload = collect_status("h1", binary=binary, home=tmp_path, timeout=0.5)
    assert time.monotonic() - started < 8 and payload["verify_ok"] is None and "timed out" in payload["last_error"]


def test_verify_never_sends_file_contents(tmp_path):
    doc = json.dumps({"ok": False, "checked": {"skills": 1}, "configDir": "/h", "contents": "SECRET-CONTENT-MARKER",
                      "errors": [{"path": "a.md", "problem": "bad", "content": "SECRET-CONTENT-MARKER"}]})
    (tmp_path / "v.json").write_text(doc)
    payload = collect_status("h1", binary=verify_binary(tmp_path, f'cat "{tmp_path}/v.json"; exit 1'), home=tmp_path)
    assert "SECRET-CONTENT-MARKER" not in json.dumps(payload) and "/h" not in json.dumps(payload["verify_error_sample"])


def test_verify_change_alone_changes_the_fingerprint_but_its_timestamp_does_not(tmp_path, cp):
    ticks = [0.0]
    ok = verify_binary(tmp_path, f'cat "{FIXTURES}/verify-ok.json"')
    agent = AiSyncFeed(feed.MetadataClient(cp, "k"), host_id="h1", binary=ok, home=tmp_path, clock=lambda: ticks[0])
    assert agent.run_once()["posted"] == 1
    ticks[0] = 60
    assert agent.run_once()["unchanged"] == 1
    agent.binary = verify_binary(tmp_path, f'cat "{FIXTURES}/verify-errors.json"; exit 1')
    assert agent.run_once()["posted"] == 1
