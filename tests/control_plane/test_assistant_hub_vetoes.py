import pytest

from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.verbs import Verb
from herdr_engineering.control_plane.assistant_hub.vetoes import Veto, VetoRule, check_vetoes


def code(command, **kw):
    veto = check_vetoes(derive(RawAction("shell", command=command, **kw)))
    return veto.code if veto else None


# (code, command) - at least three evasion forms per code.
VETOED = [
    # force_push
    ("force_push", "git push -f"),
    ("force_push", "git push --force-with-lease origin main"),
    ("force_push", "git push origin +main"),
    ("force_push", "git push origin :main"),
    ("force_push", "git push --delete origin x"),
    ("force_push", "git push --mirror"),
    ("force_push", "git push --force-if-includes"),
    ("force_push", "git push -uf origin x"),
    ("force_push", "FOO=1 git push -f"),
    ("force_push", "sudo git push -f"),
    ("force_push", "sh -c 'git push -f'"),
    ("force_push", 'bash -lc "ls && git push origin +x"'),
    ("force_push", "echo $(git push -f)"),
    ("force_push", "ls | xargs git push -f"),
    ("force_push", "git -c x=y push --force"),
    ("force_push", "git -C /repo push -f"),
    ("force_push", "ssh hive 'git push -f'"),
    ("force_push", "ls && git status; git push origin :old"),
    ("force_push", "command git push --force"),
    # protection_disable
    ("protection_disable", "gh api -X DELETE repos/o/r/branches/main/protection"),
    ("protection_disable", "gh api --method=put repos/o/r/branches/feat/x/protection -f a=b"),
    ("protection_disable", "gh api -XPATCH /repos/o/r/rulesets/12"),
    ("protection_disable", "gh api -X Delete repos/o/r/rulesets"),
    ("protection_disable", "sudo gh api -X PUT repos/o/r/branches/main/protection/required_status_checks"),
    ("protection_disable", "bash -c 'gh api -X DELETE repos/o/r/branches/main/protection'"),
    ("protection_disable", "ls && gh api -X DELETE 'repos/o/r/branches/main/protection?x=1'"),
    ("protection_disable", "curl -X DELETE https://api.github.com/repos/o/r/branches/main/protection"),
    ("protection_disable", "ssh hive 'gh api -X DELETE repos/o/r/rulesets/3'"),
    # quobyte_delete
    ("quobyte_delete", "rm -rf /quobyte/data"),
    ("quobyte_delete", "rm -rf /quobyte"),
    ("quobyte_delete", "sudo rm -rf /quobyte/x"),
    ("quobyte_delete", "ls && rm /quobyte/a"),
    ("quobyte_delete", "bash -c 'rm -rf /quobyte/x'"),
    ("quobyte_delete", "rm '/quobyte//x/../y'"),
    ("quobyte_delete", "rm /tmp/../quobyte/x"),
    ("quobyte_delete", "rm -rf ../quobyte/x"),
    ("quobyte_delete", "mv /quobyte/a /tmp/b"),
    ("quobyte_delete", "truncate -s 0 /quobyte/f"),
    ("quobyte_delete", "dd if=/dev/zero of=/quobyte/f"),
    ("quobyte_delete", "echo x > /quobyte/f"),
    ("quobyte_delete", "echo x >> //quobyte/f"),
    ("quobyte_delete", "find /quobyte -delete"),
    ("quobyte_delete", "find /quobyte/a -exec rm {} +"),
    ("quobyte_delete", "shred /quobyte/f"),
    ("quobyte_delete", "unlink /quobyte/f"),
    ("quobyte_delete", "rsync -a --delete /tmp/e/ /quobyte/d/"),
    ("quobyte_delete", "cp x /quobyte/y"),
    ("quobyte_delete", "cd /quobyte && rm -rf *"),
    ("quobyte_delete", "cd /quobyte; rm -rf ./data"),
    ("quobyte_delete", "(cd /quobyte/a; rm x)"),
    ("quobyte_delete", "find /quobyte -name '*.tmp' | xargs rm"),
    ("quobyte_delete", "rm -rf $(ls -d /quobyte/x)"),
    ("quobyte_delete", "ssh hive 'rm -rf /quobyte/x'"),
    ("quobyte_delete", "ssh hive 'cd /quobyte/x && rm -rf y'"),
    ("quobyte_delete", "srun rm -rf /quobyte/x"),
    ("quobyte_delete", "sbatch --wrap 'rm -rf /quobyte/x'"),
    ("quobyte_delete", "sed -i s/a/b/ /quobyte/f"),
    # fry_route
    ("fry_route", "ssh fry.fiehnlab.ucdavis.edu ls"),
    ("fry_route", "ssh FRY.Fiehnlab.UCDavis.edu ls"),
    ("fry_route", "ssh -J gw@fry.fiehnlab.ucdavis.edu hive ls"),
    ("fry_route", "ssh -o ProxyJump=fry.fiehnlab.ucdavis.edu hive ls"),
    ("fry_route", "scp x fry.fiehnlab.ucdavis.edu:/tmp"),
    ("fry_route", "bash -c 'curl https://fry.fiehnlab.ucdavis.edu/x'"),
    ("fry_route", "ls && ssh 'gw@fry.fiehnlab.ucdavis.edu' id"),
    ("fry_route", "H=fry.fiehnlab.ucdavis.edu ssh h"),
    ("fry_route", "ssh hive 'ssh fry.fiehnlab.ucdavis.edu'"),
    ("fry_route", "echo 'unterminated fry.fiehnlab.ucdavis.edu"),
    # cluster_install
    ("cluster_install", "ssh hive pip install requests"),
    ("cluster_install", "ssh hive 'sudo apt-get install -y vim'"),
    ("cluster_install", "ssh hive 'conda install numpy && ls'"),
    ("cluster_install", "ssh hive 'python3 -m pip install x'"),
    ("cluster_install", "ssh hive 'cargo install ripgrep'"),
    ("cluster_install", "ssh hive 'npm install -g left-pad'"),
    ("cluster_install", "ssh hive 'crontab -e'"),
    ("cluster_install", "ssh hive 'systemctl restart slurmd'"),
    ("cluster_install", "ssh hive 'apptainer build x.sif y.def'"),
    ("cluster_install", "ssh hive 'make install'"),
    ("cluster_install", "ssh hive 'gem install rake'"),
    ("cluster_install", "srun pip3 install x"),
    ("cluster_install", "sbatch --wrap 'dnf install -y gcc'"),
    ("cluster_install", "ssh farm 'FOO=1 yum install x'"),
    ("cluster_install", "ssh hive \"bash -c 'mamba install x'\""),
    # secret_print
    ("secret_print", "cat ~/.ssh/id_ed25519"),
    ("secret_print", "cat ~/.ssh/id_rsa.pub"),
    ("secret_print", "cat .env"),
    ("secret_print", "cat .ENV"),
    ("secret_print", "head -3 /home/u/.aws/credentials"),
    ("secret_print", "tail api_token.txt"),
    ("secret_print", "less server.PEM"),
    ("secret_print", "base64 my_secret.bin"),
    ("secret_print", "xxd id_rsa"),
    ("secret_print", "strings ~/.netrc"),
    ("secret_print", "od -c ~/.ssh/known_hosts"),
    ("secret_print", "grep -r pattern ~/.aws/"),
    ("secret_print", "grep -e x .env.local"),
    ("secret_print", "cat < ~/.ssh/id_rsa"),
    ("secret_print", "sudo cat /root/.ssh/id_ed25519"),
    ("secret_print", "bash -c 'cat .env'"),
    ("secret_print", "ls && cat \"id_rsa\""),
    ("secret_print", "echo $(cat ~/.ssh/id_rsa)"),
    ("secret_print", "printenv GITHUB_TOKEN"),
    ("secret_print", "printenv aws_secret_access_key"),
    ("secret_print", "echo $API_KEY"),
    ("secret_print", "echo \"${github_token}\""),
    ("secret_print", "ssh hive 'cat ~/.ssh/id_rsa'"),
]


@pytest.mark.parametrize(("expected", "command"), VETOED)
def test_veto_reached_through_evasion_forms(expected, command):
    assert code(command) == expected


def test_table_has_three_forms_per_code():
    for c in ("force_push", "protection_disable", "quobyte_delete", "fry_route", "cluster_install", "secret_print"):
        assert sum(1 for k, _ in VETOED if k == c) >= 3


NOT_VETOED = [
    "git push origin main",
    "git branch -D old-shared",
    "git status && ls",
    "rm -rf ./build",
    "rm -rf ../sibling",
    "ls /quobyte/data",
    "cat /quobyte/data/f",
    "cp /quobyte/a /tmp/b",
    "rsync -a /quobyte/a/ /tmp/b/",
    "find /quobyte -name x",
    "cd /quobyte && ls",
    "rm /tmp/quobyte/x",
    "rm /quobyteish/x",
    "gh api repos/o/r/branches/main/protection",
    "gh api -X GET repos/o/r/rulesets",
    "gh api -X DELETE repos/o/r/issues/comments/3",
    "ssh hive pip --version",
    "ssh hive squeue -u me",
    "pip install requests",
    "make install",
    "cat README.md",
    "ls ~/.ssh",
    "echo hello",
    "echo $HOME $PATH",
    "grep key README.md",
    "printenv HOME",
    "crontab -l",
    "ssh hive crontab -l",
]


@pytest.mark.parametrize("command", NOT_VETOED)
def test_benign_commands_are_not_vetoed(command):
    assert code(command) is None


def test_relative_path_outside_quobyte_is_not_vetoed():
    assert code("rm -rf ./data", hint={"cwd": "/home/u/work"}) is None
    assert code("rm -rf ../other", hint={"cwd": "/home/u/work"}) is None


def test_quobyte_relative_dotdot_resolves_into_quobyte():
    assert code("rm -rf ../quobyte/x", hint={"cwd": "/tmp"}) == "quobyte_delete"
    assert code("rm -rf ../../quobyte/x", hint={"cwd": "/home/u"}) == "quobyte_delete"
    assert code("rm -rf x/../../../quobyte", hint={"cwd": "/tmp/w"}) == "quobyte_delete"
    assert code("rm -rf data", hint={"cwd": "/quobyte/proj"}) == "quobyte_delete"
    assert code("rm -rf data", hint={"cwd": "/quobyte/proj/../.."}) is None


def test_hint_cwd_is_recorded_as_worker_attested():
    d = derive(RawAction("shell", command="rm x", hint={"cwd": "/quobyte/p"}))
    assert "cwd=/quobyte/p" in d.worker_attested_facts


@pytest.mark.parametrize("command", [
    "cat ~/.ssh/id_ed25519", "cat .ENV", "cat ~/.SSH/ID_RSA", "cat Prod.Token", "cat MY_KEY.txt", "cat server.Pem",
    "cat Credentials.json", "cat .NETRC", "cat .env.production", "cat ./a/.aws/config",
])
def test_secret_name_variants(command):
    assert code(command) == "secret_print"


@pytest.mark.parametrize("command", ["cat notes.txt", "cat config.yaml", "head log.txt", "grep token README.md"])
def test_non_secret_names_pass(command):
    assert code(command) is None


def test_veto_checks_every_element_not_just_the_strictest():
    # The strictest key is the cluster/outward one; the veto lives in the weaker local element.
    d = derive(RawAction("shell", command="cat ~/.ssh/id_rsa; ssh hive 'rm -rf x'; git push origin main"))
    assert d.key.verb is Verb.CLUSTER_MUTATING  # the stricter key is not the element that carries the veto
    assert check_vetoes(d).code == "secret_print"
    d2 = derive(RawAction("shell", command="echo hi > /quobyte/x; git push -f"))
    assert check_vetoes(d2) is not None
    d3 = derive(RawAction("shell", command="git push -f && rm -rf /quobyte/z"))
    assert check_vetoes(d3).code == "force_push"


def test_vetoes_run_on_dynamic_and_owner_decides_commands():
    d = derive(RawAction("shell", command="git push -f $REMOTE"))
    assert d.owner_decides
    assert check_vetoes(d).code == "force_push"
    d = derive(RawAction("shell", command="rm -rf /quobyte/*"))
    assert d.owner_decides
    assert check_vetoes(d).code == "quobyte_delete"


def test_cluster_install_via_hint_host():
    assert code("pip install x", hint={"host": "hive.ucdavis.edu"}) == "cluster_install"
    assert code("pip install x", hint={"host": "laptop"}) is None


def test_extra_rules_never_reduce_fixed_vetoes():
    d = derive(RawAction("shell", command="git push -f"))
    for extra in ([], [VetoRule("force_push", "dup")], [VetoRule("other", "x", verb=Verb.UNKNOWN)],
                  [VetoRule("none", "never matches", argv_contains="zzz")]):
        veto = check_vetoes(d, extra)
        assert veto is not None and veto.code == "force_push"
    assert check_vetoes(d, [VetoRule("force_push", "collides")]).reason != "collides"


def test_extra_rules_can_add_vetoes():
    d = derive(RawAction("shell", command="ls /tmp"))
    assert check_vetoes(d) is None
    assert check_vetoes(d, [VetoRule("no_ls", "ls banned", argv_contains="LS ")]) == Veto("no_ls", "ls banned")
    assert check_vetoes(d, [VetoRule("v", "r", verb=Verb.SHELL_READ_ONLY)]).code == "v"
    assert check_vetoes(d, [VetoRule("v", "r", verb=Verb.GIT_PUSH)]) is None
    assert check_vetoes(d, [VetoRule("all", "r", verb=None, argv_contains=None)]).code == "all"
    assert check_vetoes(d, [VetoRule("a", "r", verb=Verb.SHELL_READ_ONLY, argv_contains="nomatch")]) is None


def test_structured_tools_have_no_command_vetoes():
    assert check_vetoes(derive(RawAction("email", operation="send", recipients=("a@b.org",)))) is None
    assert check_vetoes(derive(RawAction("deploy", environment="prod", repo="o/r"))) is None
