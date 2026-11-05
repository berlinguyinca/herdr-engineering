"""Slack token registration and the ``assistant-slack`` CLI (Phase 4, Task 1).

No mocks: the CLI runs as a real subprocess in a throwaway HOME and the "Slack" is a real local HTTP server speaking the
Web API shape.  Every token is a random value generated here and asserted absent from all output."""
import hashlib
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering.control_plane.assistant_hub.slack_register import (
    SlackRegisterError,
    register_tokens,
)
from tests.control_plane.slack_support import SlackListener

REPO = Path(__file__).resolve().parents[2]


def fp(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def bot_tok() -> str:
    return "xoxb-" + secrets.token_urlsafe(24)


def app_tok() -> str:
    return "xapp-" + secrets.token_urlsafe(24)


def env_for(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "PYTHONPATH": os.pathsep.join([str(REPO), *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]])}


@pytest.fixture()
def keydir(tmp_path: Path) -> Path:
    d = tmp_path / "keys"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture()
def slack():
    made: list[SlackListener] = []

    def make(bot: str, app: str) -> SlackListener:
        li = SlackListener(bot, app)
        made.append(li)
        return li
    yield make
    for li in made:
        li.close()


def run_cli(home: Path, tmp_path: Path, action: str, argv: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "control-plane", "assistant-slack", action, *argv],
                          input=stdin, env=env_for(home), capture_output=True, text=True, timeout=60, cwd=str(tmp_path))


def reg_args(li: SlackListener, keydir: Path) -> list[str]:
    return ["--base-url", li.base_url, "--bot-token-file", str(keydir / "bot.tok"),
            "--app-token-file", str(keydir / "app.tok"), "--key-stdin"]


# ------------------------------------------------------------------------------------------------- library

def test_register_writes_both_files_0600(slack, keydir):
    bot, app = bot_tok(), app_tok()
    li = slack(bot, app)
    done = register_tokens(li.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), bot, app)
    assert (done.bot_fingerprint, done.app_fingerprint) == (fp(bot), fp(app))
    assert done.team == "Acme" and done.bot_user_id == "U0BOT0001"
    for name, tok in (("bot.tok", bot), ("app.tok", app)):
        p = keydir / name
        assert p.read_text() == tok + "\n"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert sorted(x.name for x in keydir.iterdir()) == ["app.tok", "bot.tok"]
    assert {r["path"].rsplit("/", 1)[-1] for r in li.requests} == {"auth.test", "apps.connections.open"}


def test_register_rotates_via_new_file(slack, keydir):
    old_bot, old_app, bot, app = bot_tok(), app_tok(), bot_tok(), app_tok()
    li = slack(old_bot, old_app)
    register_tokens(li.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), old_bot, old_app)
    li2 = slack(bot, app)
    register_tokens(li2.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), bot, app)
    assert (keydir / "bot.tok").read_text() == bot + "\n" and (keydir / "app.tok").read_text() == app + "\n"
    assert not list(keydir.glob("*.new"))


def test_bad_token_leaves_existing_files_untouched(slack, keydir):
    old_bot, old_app = bot_tok(), app_tok()
    li = slack(old_bot, old_app)
    register_tokens(li.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), old_bot, old_app)
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens(li.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), bot_tok(), old_app)
    assert exc.value.code == "auth_failed"
    assert (keydir / "bot.tok").read_text() == old_bot + "\n"
    assert not list(keydir.glob("*.new"))


def test_wrong_app_token_fails_and_leaves_no_files(slack, keydir):
    bot = bot_tok()
    li = slack(bot, app_tok())
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens(li.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), bot, app_tok())
    assert exc.value.code == "auth_failed"
    assert list(keydir.iterdir()) == []


@pytest.mark.parametrize("bot,app", [("", "xapp-1"), ("nope", "xapp-1"), ("xoxb-1", "xoxb-2"), ("xoxb-a b", "xapp-1"),
                                     ("xoxb-1", "")])
def test_malformed_tokens_are_bad_token(keydir, bot, app):
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens("https://slack.com/api", str(keydir / "b"), str(keydir / "a"), bot, app)
    assert exc.value.code == "bad_token"


def test_non_loopback_http_is_config_error(keydir):
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens("http://slack.example.org/api", str(keydir / "b"), str(keydir / "a"), "xoxb-1", "xapp-1")
    assert exc.value.code == "config"


def test_relative_path_and_same_file(keydir):
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens("https://slack.com/api", "rel.tok", str(keydir / "a"), "xoxb-1", "xapp-1")
    assert exc.value.code == "bad_path"
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens("https://slack.com/api", str(keydir / "x"), str(keydir / "x"), "xoxb-1", "xapp-1")
    assert exc.value.code == "bad_path"


def test_group_writable_parent_refused(slack, tmp_path):
    bot, app = bot_tok(), app_tok()
    li = slack(bot, app)
    d = tmp_path / "loose"
    d.mkdir()
    d.chmod(0o770)
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens(li.base_url, str(d / "b"), str(d / "a"), bot, app)
    assert exc.value.code == "unsafe_dir"
    assert li.requests == []


def test_symlink_target_refused(slack, keydir, tmp_path):
    bot, app = bot_tok(), app_tok()
    li = slack(bot, app)
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    (keydir / "bot.tok").symlink_to(victim)
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens(li.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), bot, app)
    assert exc.value.code == "symlink"
    assert victim.read_text() == "keep\n"


def test_leftover_new_file_is_refused(slack, keydir):
    bot, app = bot_tok(), app_tok()
    li = slack(bot, app)
    (keydir / "app.tok.new").write_text("stale\n")
    with pytest.raises(SlackRegisterError) as exc:
        register_tokens(li.base_url, str(keydir / "bot.tok"), str(keydir / "app.tok"), bot, app)
    assert exc.value.code == "tmp_exists"
    assert not (keydir / "bot.tok").exists() and not (keydir / "bot.tok.new").exists()


# ------------------------------------------------------------------------------------------------- CLI

def test_cli_register_prints_fingerprints_only(home, tmp_path, keydir, slack):
    bot, app = bot_tok(), app_tok()
    li = slack(bot, app)
    argv = reg_args(li, keydir)
    assert bot not in " ".join(argv) and app not in " ".join(argv)
    done = run_cli(home, tmp_path, "register", argv, f"{bot}\n{app}\n")
    assert done.returncode == 0, done.stderr
    assert f"bot_fingerprint={fp(bot)}" in done.stdout and f"app_fingerprint={fp(app)}" in done.stdout
    for secret in (bot, app):
        assert secret not in done.stdout + done.stderr
    assert (keydir / "bot.tok").read_text() == bot + "\n"


def test_cli_register_refuses_without_key_stdin(home, tmp_path, keydir, slack):
    li = slack(bot_tok(), app_tok())
    argv = [a for a in reg_args(li, keydir) if a != "--key-stdin"]
    done = run_cli(home, tmp_path, "register", argv, "")
    assert done.returncode == 2 and "--key-stdin" in done.stderr
    assert list(keydir.iterdir()) == []


@pytest.mark.parametrize("stdin", ["", "xoxb-only\n", "xoxb-a xoxb-b\nxapp-1\n"])
def test_cli_register_refuses_missing_or_malformed_tokens(home, tmp_path, keydir, slack, stdin):
    li = slack(bot_tok(), app_tok())
    done = run_cli(home, tmp_path, "register", reg_args(li, keydir), stdin)
    assert done.returncode == 2 and "tokens on stdin" in done.stderr
    assert list(keydir.iterdir()) == []


def test_cli_register_reports_code_not_tokens(home, tmp_path, keydir, slack):
    bot, app = bot_tok(), app_tok()
    li = slack(bot_tok(), app_tok())
    done = run_cli(home, tmp_path, "register", reg_args(li, keydir), f"{bot}\n{app}\n")
    assert done.returncode == 1 and "auth_failed" in done.stderr
    assert bot not in done.stdout + done.stderr and app not in done.stdout + done.stderr


def test_cli_status_is_local_and_prints_fingerprints(home, tmp_path, keydir, slack):
    bot, app = bot_tok(), app_tok()
    li = slack(bot, app)
    assert run_cli(home, tmp_path, "register", reg_args(li, keydir), f"{bot}\n{app}\n").returncode == 0
    n_before = len(li.requests)
    done = run_cli(home, tmp_path, "status", ["--bot-token-file", str(keydir / "bot.tok"),
                                              "--app-token-file", str(keydir / "app.tok")])
    assert done.returncode == 0, done.stderr
    assert fp(bot) in done.stdout and fp(app) in done.stdout
    assert bot not in done.stdout + done.stderr and app not in done.stdout + done.stderr
    assert len(li.requests) == n_before            # local only: no network


def test_cli_status_reports_missing_and_loose_files(home, tmp_path, keydir):
    done = run_cli(home, tmp_path, "status", ["--bot-token-file", str(keydir / "bot.tok"),
                                              "--app-token-file", str(keydir / "app.tok")])
    assert done.returncode == 1 and "missing" in done.stdout
    (keydir / "bot.tok").write_text("xoxb-zzz\n")
    (keydir / "bot.tok").chmod(0o644)
    (keydir / "app.tok").write_text("xapp-zzz\n")
    (keydir / "app.tok").chmod(0o600)
    done = run_cli(home, tmp_path, "status", ["--bot-token-file", str(keydir / "bot.tok"),
                                              "--app-token-file", str(keydir / "app.tok")])
    assert done.returncode == 1 and "mode" in done.stdout
    assert "xoxb-zzz" not in done.stdout + done.stderr


def test_cli_status_needs_both_paths(home, tmp_path):
    done = run_cli(home, tmp_path, "status", [])
    assert done.returncode == 2
