"""PreToolUse guard hook: real git repos, real subprocess, real settings.json under a tmp HOME."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from herdr_engineering import workflow_hooks as wh


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, timeout=30)


def make_repo(path, branch="main", origin="https://example.com/acme/other.git"):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "checkout", "-q", "-b", branch)
    if origin:
        git(path, "remote", "add", "origin", origin)
    return path


def call(payload, raw=None):
    """Run the real CLI hook; return (returncode, stdout, parsed decision or None)."""
    done = subprocess.run(
        [sys.executable, "-m", "herdr_engineering.cli", "workflow", "hook", "PreToolUse"],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, timeout=60)
    out = done.stdout.strip()
    return done.returncode, out, (json.loads(out)["hookSpecificOutput"] if out else None)


def bash(cmd, cwd):
    return call({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)})


def decision(res):
    rc, _, out = res
    assert rc == 0
    return out["permissionDecision"] if out else None


@pytest.fixture(autouse=True)
def token(tmp_path, monkeypatch):
    """The one-shot push-approval token lives under tmp_path (the guard denies /tmp on Linux CI)."""
    path = tmp_path / "state" / "allow-push"
    monkeypatch.setenv("HERDR_WORKFLOW_ALLOW_PUSH", str(path))
    return path


@pytest.fixture
def feature(tmp_path):
    return make_repo(tmp_path / "feat", "feat/x")


@pytest.fixture
def on_main(tmp_path):
    return make_repo(tmp_path / "mainrepo", "main")


# ---- rule 1: /tmp scratch writes -------------------------------------------------------
@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"),
                                      ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
@pytest.mark.parametrize("path", ["/tmp/x.txt", "/private/tmp/a/b.txt", "/tmp/../tmp/y", "/tmp"])
def test_tmp_writes_are_denied(tool, key, path, tmp_path):
    res = call({"tool_name": tool, "tool_input": {key: path}, "cwd": str(tmp_path)})
    assert decision(res) == "deny"
    assert res[2]["hookEventName"] == "PreToolUse"
    assert "gitignored" in res[2]["permissionDecisionReason"]


@pytest.mark.parametrize("path", ["/private/tmp/claude-501/proj/s/x.md", "/tmp/claude-501/x.md",
                                  "/tmp/claude-1/a/b/c"])
def test_harness_scratchpad_is_allowed(path, tmp_path):
    assert decision(call({"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": str(tmp_path)})) is None


@pytest.mark.parametrize("path", ["/tmp/claude-abc/x", "/tmp/claude-501-evil/x", "/tmp/claude-501/../x"])
def test_scratchpad_lookalikes_are_denied(path, tmp_path):
    assert decision(call({"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": str(tmp_path)})) == "deny"


def test_project_and_relative_paths_allowed():
    # The check is path-only, so use a fixed project dir: pytest's tmp_path is under /tmp on Linux CI.
    proj = "/home/dev/proj"
    assert decision(call({"tool_name": "Write", "tool_input": {"file_path": f"{proj}/.proj/x.md"}, "cwd": proj})) is None
    rel = {"tool_name": "Edit", "tool_input": {"file_path": "notes.md"}, "cwd": proj}
    assert decision(call(rel)) is None


def test_relative_path_resolves_against_cwd(tmp_path):
    rel = {"tool_name": "Edit", "tool_input": {"file_path": "x.md"}, "cwd": "/tmp"}
    assert decision(call(rel)) == "deny"


def test_bash_tmp_is_left_alone(tmp_path):
    assert decision(bash("echo hi > /tmp/foo", tmp_path)) is None


def test_read_tool_never_guarded(tmp_path):
    assert decision(call({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}, "cwd": str(tmp_path)})) is None


# ---- rule 2: push to main/master -------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "git push origin main", "git push origin master", "git push origin HEAD:main",
    "git push origin x:refs/heads/main", "git push -u origin main", "git push --force origin main",
    "git status && git push origin main", "echo a; git push origin main", "ls | git push origin main",
    "git push origin +main", "git push origin HEAD:refs/heads/master",
])
def test_push_to_main_denied(cmd, feature):
    assert decision(bash(cmd, feature)) == "deny"


def test_bare_push_on_main_denied(on_main):
    assert decision(bash("git push", on_main)) == "deny"
    assert decision(bash("git push origin", on_main)) == "deny"
    assert decision(bash("git push -u origin", on_main)) == "deny"


def test_bare_push_on_master_denied(tmp_path):
    repo = make_repo(tmp_path / "m", "master")
    assert decision(bash("git push", repo)) == "deny"


def test_bare_push_on_feature_branch_allowed(feature):
    assert decision(bash("git push", feature)) is None
    assert decision(bash("git push -u origin feat/x", feature)) is None
    assert decision(bash("git push origin HEAD:feat/y", feature)) is None
    assert decision(bash("git push origin maintenance", feature)) is None


def test_git_dash_c_uses_that_repo(on_main, feature, tmp_path):
    # cwd is the feature repo, -C points at the main repo
    assert decision(bash(f"git -C {on_main} push", feature)) == "deny"
    # cwd is main, -C points at feature
    assert decision(bash(f"git -C {feature} push", on_main)) is None
    assert decision(bash(f"cd /nowhere && git -C {on_main} push origin main", tmp_path)) == "deny"


def test_non_push_git_commands_ignored(on_main):
    for cmd in ("git status", "git commit -m 'git push origin main'", "echo git push", "git pull origin main"):
        assert decision(bash(cmd, on_main)) is None


def test_push_outside_a_repo_with_no_refspec_is_allowed(tmp_path):
    assert decision(bash("git push", tmp_path)) is None


# ---- rule 3: herdr-engineering needs approval -----------------------------------------
@pytest.mark.parametrize("url", ["git@github.com:berlinguyinca/herdr-engineering.git",
                                 "https://github.com/berlinguyinca/herdr-engineering"])
def test_herdr_engineering_push_denied(url, tmp_path):
    repo = make_repo(tmp_path / "he", "feat/z", origin=url)
    res = bash("git push -u origin feat/z", repo)
    assert decision(res) == "deny"
    assert "herdr-engineering needs explicit user approval" in res[2]["permissionDecisionReason"]
    assert decision(bash("git push", repo)) == "deny"


def test_herdr_engineering_push_to_main_is_denied_not_asked(tmp_path):
    repo = make_repo(tmp_path / "he", "feat/z", origin="git@github.com:b/herdr-engineering.git")
    assert decision(bash("git push origin main", repo)) == "deny"


def test_herdr_engineering_via_dash_c_and_chain(tmp_path, feature):
    repo = make_repo(tmp_path / "he", "feat/z", origin="git@github.com:b/herdr-engineering.git")
    assert decision(bash(f"git -C {repo} push origin feat/z", feature)) == "deny"
    assert decision(bash("true && git push origin feat/x", repo)) == "deny"


def test_other_remote_feature_push_allowed(feature):
    assert decision(bash("git push origin feat/x", feature)) is None


# ---- fail open -------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", '{"tool_name": 5}',
                                 '{"tool_name": "Bash", "tool_input": "x"}',
                                 '{"tool_name": "Bash", "tool_input": {"command": "git push \'unterminated"}, "cwd": "/"}',
                                 '{"tool_name": "Write", "tool_input": {"file_path": 3}}'])
def test_malformed_input_is_silent_exit_zero(raw):
    assert call(None, raw=raw)[:2] == (0, "")


def test_push_with_missing_cwd_fails_open(tmp_path):
    res = call({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(tmp_path / "gone")})
    assert res[:2] == (0, "")


# ---- installer wiring -------------------------------------------------------------------
CMD = ["/opt/herdr-eng"]
MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    return h


def settings(home):
    return home / ".claude" / "settings.json"


def test_install_registers_pretooluse_with_matcher(home):
    wh.WorkflowInstaller(home, command=CMD).install()
    entry = json.loads(settings(home).read_text())["hooks"]["PreToolUse"]
    assert len(entry) == 1 and entry[0]["matcher"] == MATCHER
    cmd = entry[0]["hooks"][0]["command"]
    assert wh.MARKER in cmd and "workflow hook PreToolUse" in cmd and cmd.endswith(f"|| true # {wh.MARKER}")
    # other events carry no matcher
    assert "matcher" not in json.loads(settings(home).read_text())["hooks"]["SessionStart"][0]


def test_install_is_idempotent_with_pretooluse(home):
    i = wh.WorkflowInstaller(home, command=CMD)
    i.install()
    before = settings(home).read_bytes()
    assert i.install().action == "noop" and settings(home).read_bytes() == before


def test_old_install_without_pretooluse_is_drift_and_repair_fixes(home):
    i = wh.WorkflowInstaller(home, command=CMD)
    i.install()
    data = json.loads(settings(home).read_text())
    del data["hooks"]["PreToolUse"]
    settings(home).write_text(json.dumps(data))
    d = i.doctor()
    assert not d["ok"] and d["overall"] == "stale"
    assert i.repair().action == "update"
    assert i.doctor()["ok"]


def test_wrong_matcher_is_drift(home):
    i = wh.WorkflowInstaller(home, command=CMD)
    i.install()
    data = json.loads(settings(home).read_text())
    data["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
    settings(home).write_text(json.dumps(data))
    assert i.doctor()["overall"] == "stale"
    i.install()
    assert i.doctor()["ok"]


def test_foreign_pretooluse_entry_survives_install_and_uninstall(home):
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}
    settings(home).write_text(json.dumps({"hooks": {"PreToolUse": [foreign]}}))
    i = wh.WorkflowInstaller(home, command=CMD)
    i.install()
    pre = json.loads(settings(home).read_text())["hooks"]["PreToolUse"]
    assert foreign in pre and len(pre) == 2
    i.uninstall()
    assert json.loads(settings(home).read_text()) == {"hooks": {"PreToolUse": [foreign]}}


def test_generated_command_blocks_via_sh(home, tmp_path):
    wh.WorkflowInstaller(home, command=[sys.executable, "-m", "herdr_engineering.cli"]).install()
    cmd = json.loads(settings(home).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    done = subprocess.run(["sh", "-c", cmd], input=json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}, "cwd": str(tmp_path)}),
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---- reviewer bypasses (PR #862) --------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "git push -u origin HEAD", "git push origin HEAD", "git push origin @", "git push origin +HEAD",
    "git push origin +@", "git push origin +main", "git push origin +HEAD:main",
    "git push origin HEAD:refs/heads/main", "git push --signed origin main",
    "git push --force-with-lease origin HEAD", "git push -o ci.skip origin HEAD",
    "git status\ngit push origin main", "git status\ngit push -u origin HEAD",
])
def test_bypass_forms_denied_on_main(cmd, on_main):
    assert decision(bash(cmd, on_main)) == "deny"


@pytest.mark.parametrize("cmd", ["git push --signed origin main", "git status\ngit push origin main",
                                 "git push origin +main", "git push origin +HEAD:main"])
def test_bypass_forms_denied_from_feature_branch(cmd, feature):
    assert decision(bash(cmd, feature)) == "deny"


def test_cd_then_push_on_next_line_denied(on_main, tmp_path):
    assert decision(bash(f"cd {on_main}\ngit push origin main", tmp_path)) == "deny"


@pytest.mark.parametrize("cmd", ["git push -u origin HEAD", "git push origin @", "git push origin +HEAD",
                                 "git push --signed origin feat/x", "git push origin HEAD:feat/y"])
def test_head_and_signed_on_feature_branch_allowed(cmd, feature):
    assert decision(bash(cmd, feature)) is None


def test_multiline_message_with_unbalanced_quote_fails_open(feature):
    assert decision(bash('git commit -m "line one\nline two"', feature)) is None


def test_head_push_to_herdr_engineering_feature_asks(tmp_path):
    repo = make_repo(tmp_path / "he", "feat/z", origin="git@github.com:b/herdr-engineering.git")
    assert decision(bash("git push -u origin HEAD", repo)) == "deny"


# ---- shell redirections are not refspecs ------------------------------------------------
REDIRECTS = ["2>&1", ">/dev/null", "2>/dev/null", "&>log", "&> log", ">>log", ">> log", "> log",
             "2> log", "2>> log", "1>&2", "<<< x", "< in", ">&2", "2>&1 | tail -3", ">/dev/null 2>&1"]


@pytest.mark.parametrize("redir", REDIRECTS)
def test_redirect_after_bare_push_on_main_denied(redir, on_main):
    assert decision(bash(f"git push origin {redir}", on_main)) == "deny"
    assert decision(bash(f"git push {redir}", on_main)) == "deny"
    assert decision(bash(f"git push -u origin HEAD {redir}", on_main)) == "deny"


@pytest.mark.parametrize("redir", [r for r in REDIRECTS if "|" not in r])
def test_redirect_before_refspec_still_sees_main(redir, feature):
    assert decision(bash(f"git push origin main {redir}", feature)) == "deny"
    assert decision(bash(f"git push {redir} origin main", feature)) == "deny"


@pytest.mark.parametrize("redir", REDIRECTS)
def test_redirect_on_feature_branch_allowed(redir, feature):
    assert decision(bash(f"git push origin feat/x {redir}", feature)) is None
    assert decision(bash(f"git push origin {redir}", feature)) is None


def test_ampersand_in_redirect_does_not_hide_a_second_push(on_main):
    assert decision(bash("echo hi 2>&1 && git push origin main", on_main)) == "deny"
    assert decision(bash("sleep 1 & git push origin main", on_main)) == "deny"


@pytest.mark.parametrize("redir", ["2>&1", ">/dev/null", "&>log"])
def test_herdr_engineering_ask_survives_redirect(redir, tmp_path):
    repo = make_repo(tmp_path / "he", "feat/z", origin="git@github.com:b/herdr-engineering.git")
    assert decision(bash(f"git push origin feat/z {redir}", repo)) == "deny"


# ---- heredocs, multi-line quotes, continuations, untokenizable input --------------------
HEREDOC = "git commit -m \"$(cat <<'EOF'\nmsg line\nsecond\nEOF\n)\""
MULTILINE = 'git commit -m "first line\nsecond line"'


@pytest.mark.parametrize("prefix", [HEREDOC, MULTILINE, "git status\n\ngit add -A",
                                    "cat <<EOF\nbody\nEOF", "cat <<-'EOF'\n\tbody\n\tEOF"])
@pytest.mark.parametrize("sep", [" && ", "\n", " ; ", "\n\n"])
def test_push_main_after_multiline_prefix_denied(prefix, sep, feature):
    assert decision(bash(f"{prefix}{sep}git push origin main", feature)) == "deny"


@pytest.mark.parametrize("prefix", [HEREDOC, MULTILINE])
def test_bare_and_head_push_on_main_after_multiline_prefix_denied(prefix, on_main):
    assert decision(bash(f"{prefix} && git push", on_main)) == "deny"
    assert decision(bash(f"{prefix}\ngit push -u origin HEAD", on_main)) == "deny"


@pytest.mark.parametrize("prefix", [HEREDOC, MULTILINE])
def test_feature_push_after_multiline_prefix_allowed(prefix, feature):
    assert decision(bash(f"{prefix} && git push origin feat/x", feature)) is None
    assert decision(bash(f"{prefix}\ngit push -u origin HEAD", feature)) is None


@pytest.mark.parametrize("cmd", ["git push \\\norigin main", "git push origin \\\n  main",
                                 "git push \\\n  -u \\\n  origin HEAD:main"])
def test_backslash_continuation_denied(cmd, feature):
    assert decision(bash(cmd, feature)) == "deny"


def test_backslash_continuation_bare_push_on_main_denied(on_main):
    assert decision(bash("git push \\\n  -u origin \\\n  HEAD", on_main)) == "deny"


def test_backslash_continuation_feature_push_allowed(feature):
    assert decision(bash("git push \\\n  -u origin \\\n  feat/x", feature)) is None


def test_heredoc_body_text_is_not_a_command(feature):
    cmd = "git commit -F - <<'EOF'\nnever run git push origin main\nEOF\ngit push origin feat/x"
    assert decision(bash(cmd, feature)) is None
    msg = "git commit -m \"$(cat <<'EOF'\ndocs: git push origin main is blocked\nEOF\n)\""
    assert decision(bash(msg, feature)) is None


@pytest.mark.parametrize("cmd", ["git push origin main 'unterminated", 'echo "oops && git push origin main',
                                 "git push origin HEAD:main \"x", "git push +main '", "git push origin refs/heads/master `"])
def test_untokenizable_push_to_main_denied(cmd, feature):
    assert decision(bash(cmd, feature)) == "deny"


def test_untokenizable_herdr_engineering_push_denied(tmp_path):
    repo = make_repo(tmp_path / "he", "feat/z", origin="git@github.com:b/herdr-engineering.git")
    assert decision(bash("git push origin feat/z 'unterminated", repo)) == "deny"


def test_untokenizable_non_push_and_feature_push_allowed(feature):
    assert decision(bash("echo 'unterminated", feature)) is None
    assert decision(bash("git push origin feat/x 'unterminated", feature)) is None


# ---- cd tracking, wrappers and shell keywords -------------------------------------------
def test_cd_then_push_uses_the_cd_dir(on_main, feature, tmp_path):
    assert decision(bash(f"cd {on_main} && git push", feature)) == "deny"
    assert decision(bash(f"cd {on_main} && git push -u origin HEAD", tmp_path)) == "deny"
    assert decision(bash(f"cd {on_main}; git push origin", feature)) == "deny"
    assert decision(bash(f"cd {on_main}\ngit push", feature)) == "deny"
    assert decision(bash(f"cd {feature} && git push", on_main)) is None
    assert decision(bash(f"cd {feature} && git push -u origin HEAD", on_main)) is None


def test_relative_cd_resolves_against_session_cwd(on_main, tmp_path):
    assert decision(bash("cd mainrepo && git push -u origin HEAD", tmp_path)) == "deny"
    other = make_repo(tmp_path / "feat2", "feat/q")
    assert decision(bash("cd ../mainrepo && git push", other)) == "deny"


def test_cd_chain_and_dash_c_override(on_main, feature, tmp_path):
    assert decision(bash(f"cd {tmp_path} && cd mainrepo && git push", feature)) == "deny"
    assert decision(bash(f"cd {on_main} && git -C {feature} push", feature)) is None
    assert decision(bash(f"cd {feature} && git -C {on_main} push", feature)) == "deny"


def test_cd_to_herdr_engineering_repo_asks(tmp_path, feature):
    repo = make_repo(tmp_path / "he", "feat/z", origin="git@github.com:b/herdr-engineering.git")
    assert decision(bash(f"cd {repo} && git push origin feat/z", feature)) == "deny"


def test_unknown_cd_target_makes_bare_and_head_pushes_ask(feature, tmp_path):
    for cd in ("cd /no/such/dir", "cd -", "cd $SOMEWHERE"):
        assert decision(bash(f"{cd} && git push", feature)) == "deny"
        assert decision(bash(f"{cd} && git push -u origin HEAD", feature)) == "deny"
        assert decision(bash(f"{cd} && git push origin main", feature)) == "deny"
        assert decision(bash(f"{cd} && git push origin feat/x", feature)) is None


def test_tilde_cd_is_expanded(feature):
    # ~ expands to the real home, which is not a main-branch repo checkout we control: just
    # require that it resolves (no "unknown cwd" ask) for an explicit feature refspec.
    assert decision(bash("cd ~ && git push origin feat/x", feature)) is None


def test_subshell_cd_does_not_leak(on_main, feature):
    assert decision(bash(f"(cd {on_main} && git status); git push", feature)) is None
    assert decision(bash(f"(cd {feature} && git status); git push", on_main)) == "deny"


WRAPPED_MAIN = [
    "(cd {r} && git push origin main)", "( git push origin main )", "(git push origin main)",
    "if true; then git push origin main; fi", "if true\nthen\n  git push origin main\nfi",
    "while false; do git push origin main; done", "! git push origin main",
    "env GIT_X=1 git push origin main", "env -i GIT_X=1 git push origin main",
    "GIT_X=1 git push origin main", "A=1 B=2 git push origin main", "time git push origin main",
    "bash -c 'git push origin main'", "sh -c \"git push origin main\"", "zsh -c 'cd {r} && git push'",
    "{ git push origin main; }", "{ git push origin main;}", "command git push origin main",
    "exec git push origin main", "nohup git push origin main", "sudo git push origin main",
    "sudo -u bob git push origin main", "echo $(git push origin main)", "bash -c 'bash -c \"git push origin main\"'",
    "true && (git push origin main)", "git status || { git push origin main; }",
]


@pytest.mark.parametrize("cmd", WRAPPED_MAIN)
def test_wrapped_push_to_main_denied(cmd, on_main, feature):
    assert decision(bash(cmd.replace("{r}", str(on_main)), feature)) == "deny"


WRAPPED_FEATURE = [
    "(git push origin feat/x)", "if true; then git push origin feat/x; fi", "env A=1 git push origin feat/x",
    "A=1 git push -u origin HEAD", "time git push origin feat/x", "bash -c 'git push -u origin HEAD'",
    "{ git push origin feat/x; }", "nohup git push origin feat/x", "sudo git push origin feat/x",
    "(cd {r} && git push)", "bash -c 'echo hi'",
]


@pytest.mark.parametrize("cmd", WRAPPED_FEATURE)
def test_wrapped_feature_push_allowed(cmd, feature):
    assert decision(bash(cmd.replace("{r}", str(feature)), feature)) is None


def test_bash_c_bare_push_on_main_denied_and_uses_effective_cwd(on_main, feature):
    assert decision(bash("bash -c 'git push'", on_main)) == "deny"
    assert decision(bash(f"cd {on_main} && bash -c 'git push'", feature)) == "deny"


def test_wrapped_herdr_engineering_push_denied(tmp_path):
    repo = make_repo(tmp_path / "he", "feat/z", origin="git@github.com:b/herdr-engineering.git")
    assert decision(bash("(env A=1 git push origin feat/z)", repo)) == "deny"
    assert decision(bash("bash -c 'git push origin feat/z'", repo)) == "deny"


# ---- one-shot human override for guarded pushes ----------------------------------------
def herdr_repo(tmp_path):
    return make_repo(tmp_path / "he", "feat/z", "git@github.com:acme/herdr-engineering.git")


def test_guarded_push_deny_reason_tells_agent_how_to_get_approval(tmp_path, token):
    rc, _, out = bash("git push origin feat/z", herdr_repo(tmp_path))
    assert out["permissionDecision"] == "deny"
    assert f"! touch {token}" in out["permissionDecisionReason"]


def test_unknown_cwd_push_deny_reason_names_token(feature, token):
    rc, _, out = bash("cd - && git push", feature)
    assert out["permissionDecision"] == "deny"
    assert f"! touch {token}" in out["permissionDecisionReason"]


def test_token_allows_one_push_then_is_consumed(tmp_path, token):
    repo = herdr_repo(tmp_path)
    token.parent.mkdir(parents=True)
    token.touch()
    assert decision(bash("git push origin feat/z", repo)) is None
    assert not token.exists()
    assert decision(bash("git push origin feat/z", repo)) == "deny"


def test_token_allows_unknown_cwd_push(feature, token):
    token.parent.mkdir(parents=True)
    token.touch()
    assert decision(bash("cd - && git push", feature)) is None
    assert not token.exists()


def test_token_does_not_override_main_push_and_is_kept(on_main, token):
    token.parent.mkdir(parents=True)
    token.touch()
    assert decision(bash("git push origin main", on_main)) == "deny"
    assert token.exists()


def test_token_not_consumed_by_unguarded_push(feature, token):
    token.parent.mkdir(parents=True)
    token.touch()
    assert decision(bash("git push origin feat/x", feature)) is None
    assert token.exists()


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"),
                                      ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_agent_cannot_write_the_token(tool, key, token, tmp_path):
    for path in (str(token), str(token.parent / ".." / "state" / "allow-push")):
        res = call({"tool_name": tool, "tool_input": {key: path}, "cwd": str(tmp_path)})
        assert decision(res) == "deny"
    assert not token.exists()


def test_relative_token_write_denied(token):
    res = call({"tool_name": "Write", "tool_input": {"file_path": "allow-push"}, "cwd": str(token.parent)})
    assert decision(res) == "deny"


@pytest.mark.parametrize("fmt", ["touch {t}", "echo > {t}", "python -c \"open('{t}','w')\""])
def test_bash_referencing_token_is_denied(fmt, token, tmp_path):
    assert decision(bash(fmt.format(t=token), tmp_path)) == "deny"
    assert not token.exists()


def test_bash_home_forms_of_default_token_denied(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_WORKFLOW_ALLOW_PUSH")
    for cmd in ("touch ~/.local/state/herdr-engineering/allow-push",
                "touch $HOME/.local/state/herdr-engineering/allow-push"):
        assert decision(bash(cmd, tmp_path)) == "deny"


def test_unrelated_bash_and_writes_unaffected(tmp_path):
    assert decision(bash("ls", tmp_path)) is None
    res = call({"tool_name": "Write", "tool_input": {"file_path": "/home/dev/proj/x"}, "cwd": "/home/dev/proj"})
    assert decision(res) is None
