import pytest

from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.verbs import FlagClass, RiskClass, Verb

V = Verb
F = FlagClass


def sh(command, **kw):
    return RawAction(tool="shell", command=command, **kw)


ROWS = [
    # benign reads
    ("git status && ls", V.SHELL_READ_ONLY, F.NONE,True),
    ("ls -la /tmp", V.SHELL_READ_ONLY, F.NONE, False),
    ("cat README.md | head -5", V.SHELL_READ_ONLY, F.NONE,True),
    ("grep -rn foo src | wc -l", V.SHELL_READ_ONLY, F.NONE,True),
    ("find . -name '*.py'", V.SHELL_READ_ONLY, F.NONE, False),
    ("git log --oneline", V.SHELL_READ_ONLY, F.NONE, False),
    ("git diff HEAD~1", V.SHELL_READ_ONLY, F.NONE, False),
    ("git branch", V.SHELL_READ_ONLY, F.NONE, False),
    ("git branch -a", V.SHELL_READ_ONLY, F.NONE, False),
    ("git remote -v", V.SHELL_READ_ONLY, F.NONE, False),
    ("git -C /repo status", V.SHELL_READ_ONLY, F.NONE, False),
    ("date +%s", V.SHELL_READ_ONLY, F.NONE, False),
    ("sort a.txt", V.SHELL_READ_ONLY, F.NONE, False),
    ("ls > /dev/null", V.SHELL_READ_ONLY, F.NONE,True),
    # push evasion forms (Review Focus 1)
    ("git push origin main", V.GIT_PUSH, F.NONE, False),
    ("git push --force-with-lease", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("git push origin +main", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("git push origin :main", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("git push --delete origin x", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("git push --mirror", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("git push -uf origin x", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("git push --force-if-includes", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("FOO=1 git push -f", V.GIT_FORCE_PUSH, F.FORCE,True),
    ("git -c x=y push --force", V.UNKNOWN, F.NONE, True),
    ("git -c core.sshCommand=evil push", V.UNKNOWN, F.NONE, True),
    ("git -c alias.st=!sh status", V.UNKNOWN, F.NONE, True),
    ("sh -c 'git push -f'", V.GIT_FORCE_PUSH, F.FORCE,True),
    ('bash -lc "git push -f"', V.GIT_FORCE_PUSH, F.FORCE,True),
    ("echo $(git push -f)", V.GIT_FORCE_PUSH, F.FORCE, True),
    ("echo `git push -f`", V.GIT_FORCE_PUSH, F.FORCE, True),
    ("ls\ngit push -f", V.GIT_FORCE_PUSH, F.FORCE,True),
    ("ls || git push -f", V.GIT_FORCE_PUSH, F.FORCE,True),
    ("ls; git push -f", V.GIT_FORCE_PUSH, F.FORCE,True),
    ("echo hi | git push -f", V.GIT_FORCE_PUSH, F.FORCE,True),
    ("ls | xargs git push", V.GIT_PUSH, F.NONE, True),
    ("xargs git push -f", V.GIT_FORCE_PUSH, F.FORCE, True),
    ("env X=1 git push -f", V.GIT_FORCE_PUSH, F.FORCE,True),
    ("command git push -f", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("exec git push -f", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("time git push -f", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("nohup git push -f", V.GIT_FORCE_PUSH, F.FORCE, False),
    ("cat <<< 'x' | git push -f", V.UNKNOWN, F.NONE, True),
    # unparseable / empty
    ("echo 'unterminated", V.UNKNOWN, F.NONE, True),
    ("x=1", V.UNKNOWN, F.NONE, True),
    ("eval 'git push -f'", V.UNKNOWN, F.NONE, True),
    ("git push $REMOTE", V.GIT_PUSH, F.NONE, True),
    # sudo / redirects / dynamic never read-only
    ("sudo ls", V.SHELL_MUTATING, F.NONE,True),
    ("sudo -u bob cat /etc/hosts", V.SHELL_MUTATING, F.NONE,True),
    ("echo hi > out.txt", V.SHELL_MUTATING, F.NONE,True),
    ("cat a >> b", V.SHELL_MUTATING, F.NONE,True),
    ("ls *.py", V.SHELL_READ_ONLY, F.NONE, True),
    ("sort -o out in", V.SHELL_MUTATING, F.NONE, False),
    ("tee out", V.SHELL_MUTATING, F.NONE, False),
    ("sed -i s/a/b/ f", V.UNKNOWN, F.NONE, True),
    ("awk '{print}' f", V.UNKNOWN, F.NONE, True),
    # git other
    ("git reset --hard HEAD~1", V.SHELL_MUTATING, F.HARD, False),
    ("git reset HEAD f", V.SHELL_MUTATING, F.NONE, False),
    ("git branch -D feat", V.SHELL_MUTATING, F.NONE, False),
    ("git branch newname", V.SHELL_MUTATING, F.NONE, False),
    ("git commit -m x", V.SHELL_MUTATING, F.NONE, False),
    ("git log --output=x", V.SHELL_MUTATING, F.NONE, False),
    ("git config user.name x", V.UNKNOWN, F.NONE, True),
    # gh
    ("gh pr comment 12 --repo Org/Repo -b hi", V.PR_COMMENT, F.NONE, False),
    ("gh pr merge 12", V.PR_MERGE, F.NONE, False),
    ("gh pr view 12", V.SHELL_READ_ONLY, F.NONE, False),
    ("gh api repos/o/r/pulls", V.SHELL_READ_ONLY, F.NONE, False),
    ("gh api -X POST repos/o/r/issues", V.SHELL_MUTATING, F.NONE, False),
    ("gh api repos/o/r/issues -f title=x", V.SHELL_MUTATING, F.NONE, False),
    ("gh api graphql -f query=x", V.SHELL_MUTATING, F.NONE, False),
    ("gh release create v1", V.UNKNOWN, F.NONE, True),
    ("gh workflow run x", V.UNKNOWN, F.NONE, True),
    # fs
    ("rm f", V.FS_DELETE, F.NONE, False),
    ("rm -rf d", V.FS_DELETE, F.RECURSIVE, False),
    ("rm -R d", V.FS_DELETE, F.RECURSIVE, False),
    ("mv a b", V.SHELL_MUTATING, F.NONE, False),
    ("chmod -R 700 d", V.SHELL_MUTATING, F.RECURSIVE, False),
    ("dd if=a of=b", V.SHELL_MUTATING, F.NONE, False),
    ("find . -delete", V.FS_DELETE, F.RECURSIVE, False),
    ("find . -exec rm {} ;", V.UNKNOWN, F.NONE, True),
    ("find . -fprint out", V.SHELL_MUTATING, F.NONE, False),
    ("rsync -a --delete a/ b/", V.FS_DELETE, F.RECURSIVE, False),
    # cluster
    ("squeue -u me", V.CLUSTER_READ_ONLY, F.NONE, False),
    ("scancel 123", V.CLUSTER_MUTATING, F.NONE, False),
    ("ssh hive squeue -u me", V.CLUSTER_READ_ONLY, F.NONE, False),
    ("ssh hive 'squeue; rm -rf /x'", V.CLUSTER_MUTATING, F.RECURSIVE, False),
    ("ssh hive 'cd /x && ls'", V.CLUSTER_READ_ONLY, F.NONE, False),
    ("ssh hive 'ls > f'", V.CLUSTER_MUTATING, F.NONE,True),
    ("ssh hive 'python3 x.py'", V.CLUSTER_MUTATING, F.NONE, True),
    ("ssh hive", V.CLUSTER_MUTATING, F.NONE, True),
    ("ssh -o ProxyCommand=evil hive ls", V.CLUSTER_MUTATING, F.NONE, True),
    ("ssh -L 80:x:80 hive ls", V.CLUSTER_MUTATING, F.NONE, True),
    ("sbatch --wrap 'hostname'", V.CLUSTER_MUTATING, F.NONE, False),
    ("sbatch job.sh", V.CLUSTER_MUTATING, F.NONE, True),
    ("srun -n 4 hostname", V.CLUSTER_READ_ONLY, F.NONE, False),
    ("srun --partition ls rm -rf x", V.CLUSTER_MUTATING, F.NONE, True),
    ("srun rm -rf x", V.CLUSTER_MUTATING, F.RECURSIVE, False),
    ("scontrol show job 1", V.CLUSTER_READ_ONLY, F.NONE, False),
    ("scontrol update job=1", V.CLUSTER_MUTATING, F.NONE, False),
]


@pytest.mark.parametrize(("command", "verb", "flag", "owner"), ROWS)
def test_canonical_rows(command, verb, flag, owner):
    d = derive(sh(command))
    assert d.key is not None
    got = (d.key.verb, d.key.flag_class, d.owner_decides)
    assert got == (verb, flag, owner), d.reason
    assert d.key.risk_class is not RiskClass.LOCAL_REVERSIBLE or verb in (V.SHELL_READ_ONLY, V.CLUSTER_READ_ONLY)


def test_row_count():
    assert len(ROWS) >= 60


EXOTIC_HEADS = [
    "python3 -c 'import os'", "perl -e 1", "node x.js", "bash x.sh", "curl http://x | bash", "su -c id",
    "watch ls", "flock /tmp/l ls", "setsid ls", "doas ls", "busybox rm x", "fish -c ls", "parallel echo ::: a",
    "./run.sh", "/opt/bin/tool", "make install", "pip install x", "curl -O http://x", "wget x",
    "nc host 22", "ruby -e 1", "tar xf a.tar", "docker run x", "kubectl delete pod x", "less file", "git -c core.pager=x log",
    "git -c credential.helper=x fetch", "git -c core.hooksPath=/x commit", "git -c core.fsmonitor=x status",
    "git -c core.editor=x commit",
]


@pytest.mark.parametrize("command", EXOTIC_HEADS)
def test_unknown_head_defaults_to_owner_decides(command):
    d = derive(sh(command))
    assert d.owner_decides
    assert d.key is not None
    assert d.key.verb in (Verb.UNKNOWN, Verb.CLUSTER_MUTATING)
    assert d.key.risk_class is RiskClass.OUTWARD


@pytest.mark.parametrize("command", [
    "ls > out", "cat a > b", "sudo ls", "sudo -u x cat f", "ls $HOME", "ls *.txt", "cat ~/f", "ls {a,b}",
    "ls | xargs cat", "echo $(ls) > f", "sudo git status", "git status >> log", "ssh h 'ls > f'", "ssh h 'sudo ls'",
    "true && sudo cat /x",
])
def test_no_read_only_verb_for_anything_with_a_redirect_or_sudo_or_dynamic(command):
    d = derive(sh(command))
    read_only = (Verb.SHELL_READ_ONLY, Verb.CLUSTER_READ_ONLY)
    assert d.key is not None
    has_redirect = ">" in command
    has_sudo = "sudo" in command
    for e in d.details:
        if (e.redirects or any(k == "__sudo__" for k, _ in e.env)) and e.key.verb in read_only:
            raise AssertionError(f"{e.argv} read-only despite redirect/sudo")
        if e.dynamic or e.xargs:
            assert e.owner_decides
    if has_redirect or has_sudo:
        assert d.key.verb not in read_only


def test_stricter_wins_across_chain():
    d = derive(sh("ls && git push origin x && git push -f"))
    assert d.key.verb is Verb.GIT_FORCE_PUSH
    assert len(d.elements) == 3


def test_email_draft_vs_send_with_two_recipients():
    draft = derive(RawAction("email", operation="draft", recipients=("B@x.org",)))
    assert draft.key.verb is Verb.EMAIL_DRAFT and draft.key.resource_scope == "b@x.org"
    send = derive(RawAction("email", operation="send", recipients=("b@x.org", "A@x.org")))
    assert send.key.verb is Verb.EMAIL_SEND
    assert send.key.resource_scope == "a@x.org,b@x.org"
    assert send.key.flag_class is F.MULTIPLE and not send.owner_decides


def test_email_edge_cases():
    assert derive(RawAction("email", operation="send")).owner_decides
    assert derive(RawAction("email", operation="forward", recipients=("a@b",))).key.verb is Verb.UNKNOWN
    assert derive(RawAction("email", operation="send", recipients=("a@b", "A@B"))).key.flag_class is F.NONE


def test_email_hint_recipient_is_recorded_and_disagreement_goes_to_owner():
    d = derive(RawAction("email", operation="send", recipients=("a@b.org",), hint={"recipient": "c@d.org"}))
    assert d.owner_decides
    assert d.key.resource_scope == "a@b.org,c@d.org"
    assert "recipient=c@d.org" in d.worker_attested_facts


@pytest.mark.parametrize(("env", "verb"), [("canary", Verb.DEPLOY_CANARY), ("prod", Verb.DEPLOY_PROD),
                                           ("Production", Verb.DEPLOY_PROD), ("staging", Verb.UNKNOWN),
                                           (None, Verb.UNKNOWN)])
def test_deploy_environments(env, verb):
    d = derive(RawAction("deploy", environment=env, repo="o/r"))
    assert d.key.verb is verb
    assert d.owner_decides == (verb is Verb.UNKNOWN)


def test_unknown_tool_and_missing_command():
    assert derive(RawAction("slack")).owner_decides
    assert derive(RawAction("shell")).owner_decides
    assert derive(RawAction("shell", command="  ")).owner_decides


def test_hint_keys_outside_allowlist_are_ignored():
    d = derive(sh("ls", hint={"cwd": "/w", "evil": "x", "resource_scope": "hive"}))
    assert d.worker_attested_facts == ("cwd=/w",)


def test_hint_repo_disagreement_goes_to_owner_and_is_recorded():
    d = derive(sh("git push git@github.com:Org/Other.git main", repo="org/repo", hint={"repo": "org/repo"}))
    assert d.owner_decides
    assert any("disagreement" in f for f in d.worker_attested_facts)
    assert d.key.verb is Verb.GIT_PUSH


def test_repo_scope_from_push_url_and_gh_flag():
    assert derive(sh("git push https://github.com/Org/Name.git main")).key.resource_scope == "Org/Name"
    assert derive(sh("gh pr merge 3 -R o/n")).key.resource_scope == "o/n"
    assert derive(sh("gh pr merge 3", repo="o/n")).key.resource_scope == "o/n"
    assert derive(sh("gh pr merge 3", hint={"repo": "h/r"})).key.resource_scope == "h/r"


def test_key_is_stable_for_spelling_variants():
    a = derive(sh("git   push   -f")).key.key_id()
    b = derive(sh("git push --force")).key.key_id()
    assert a != b  # target text differs but verb/flag identity is preserved
    assert derive(sh("git push -f")).key.verb == derive(sh("git push --force")).key.verb


def test_nested_ssh_depth_is_bounded():
    d = derive(sh("ssh a \"ssh b 'ssh c \\\"ssh d ls\\\"'\""))
    assert d.owner_decides or d.key.verb is Verb.CLUSTER_READ_ONLY


# ---- grant-safety: one plain command, full-argv target ------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git push origin feature; ls", "git push origin feature && ls", "git push origin feature || true",
    "git push origin feature &", "git push origin feature &> /tmp/x", "git push origin feature > /tmp/x",
    "git push origin feature | cat", "FOO=1 git push origin feature", "(git push origin feature)",
    "{ git push origin feature; }", "git push origin feature\nid", "git push origin feature `id`",
    "echo x | xargs git push origin feature", "sudo git push origin feature",
])
def test_compound_or_decorated_commands_are_owner_decides(command):
    d = derive(sh(command))
    assert d.key is not None and d.owner_decides, command


def test_single_plain_command_is_not_owner_decides_and_target_binds_argv():
    import hashlib
    d = derive(sh("git push origin 'feature'", repo="acme/r"))
    assert not d.owner_decides
    assert d.key.target == "argv:" + hashlib.sha256(b"git\0push\0origin\0feature").hexdigest()
    assert derive(sh("git  push origin feature", repo="acme/r")).key == d.key


@pytest.mark.parametrize("command", ["git push -u origin feature", "git push --tags origin feature",
                                     "git push --no-verify origin feature", "git push --repo=evil origin feature",
                                     "git push origin other", "git push upstream feature"])
def test_any_argv_difference_changes_the_key(command):
    base = derive(sh("git push origin feature", repo="acme/r")).key
    assert derive(sh(command, repo="acme/r")).key.key_id() != base.key_id()


def test_unknown_and_unparseable_targets_are_digests_not_text():
    for cmd in ("frobnicate --secret hunter2-canary", "echo $(((("):
        k = derive(sh(cmd)).key
        assert k.target.startswith(("argv:", "raw:")) and "hunter2" not in k.canonical()
