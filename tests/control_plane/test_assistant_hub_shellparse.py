import pytest

from herdr_engineering.control_plane.assistant_hub.shellparse import (
    SimpleCommand,
    Unparseable,
    parse,
)


def argvs(command):
    return [c.argv for c in parse(command)]


def test_splits_and_or_semicolon_newline_background():
    assert argvs("a 1 && b 2 || c; d\ne &\nf") == [
        ("a", "1"), ("b", "2"), ("c",), ("d",), ("e",), ("f",),
    ]


def test_pipe_yields_every_stage():
    assert argvs("cat f | grep x |& wc -l") == [("cat", "f"), ("grep", "x"), ("wc", "-l")]


def test_subshell_and_brace_groups_are_flattened():
    assert argvs("(a; b) && { c; d; }") == [("a",), ("b",), ("c",), ("d",)]
    assert argvs("echo { } x") == [("echo", "{", "}", "x")]


def test_env_assignment_and_wrappers_are_stripped():
    cmds = parse("FOO=bar BAZ='a b' env -i X=1 nice -n 5 nohup time -p rm -rf /tmp/x")
    assert [c.argv for c in cmds] == [("rm", "-rf", "/tmp/x")]
    assert cmds[0].env == (("FOO", "bar"), ("BAZ", "a b"), ("X", "1"))
    assert cmds[0].wrapped is False


def test_sudo_marks_env_and_skips_option_arguments():
    cmds = parse("sudo -u bob -n -- command exec builtin git push")
    assert cmds == [SimpleCommand(("git", "push"), (("__sudo__", "1"),), False)]
    assert argvs("sudo -nu bob rm x") == [("rm", "x")]
    assert argvs("/usr/bin/sudo --user=bob rm x") == [("rm", "x")]


def test_other_wrapper_options():
    assert argvs("timeout -s KILL -k 5 30 sleep 1") == [("sleep", "1")]
    assert argvs("timeout 30s ls") == [("ls",)]
    assert argvs("stdbuf -oL -e0 tail f") == [("tail", "f")]
    assert argvs("ionice -c 2 -n 7 cp a b") == [("cp", "a", "b")]
    assert argvs("nice -5 ls") == [("ls",)]
    assert argvs("env -u HOME ls") == [("ls",)]
    assert argvs("command -p ls") == [("ls",)]
    assert argvs("exec -a foo ls") == [("ls",)]


def test_sh_dash_c_is_recursed():
    cmds = parse("bash -lc 'git status && rm -rf x'")
    assert [(c.argv, c.wrapped) for c in cmds] == [
        (("git", "status"), True), (("rm", "-rf", "x"), True),
    ]
    assert argvs("sh -c 'a'") == [("a",)]
    assert argvs("zsh -x -c 'a'") == [("a",)]
    assert argvs("dash -ic 'a'") == [("a",)]
    assert argvs("/bin/ksh -c 'a'") == [("a",)]
    assert argvs("bash -c ''") == []


def test_sudo_and_env_propagate_into_shell_c():
    cmds = parse("A=1 sudo bash -c 'rm x'")
    assert cmds == [SimpleCommand(("rm", "x"), (("A", "1"), ("__sudo__", "1")), True)]


def test_command_substitution_and_backticks_are_extracted():
    cmds = parse('echo "$(date)" `whoami` $(a $(b))')
    assert [(c.argv, c.wrapped) for c in cmds] == [
        (("echo", "$(date)", "`whoami`", "$(a $(b))"), False),
        (("date",), True), (("whoami",), True), (("a", "$(b)"), True), (("b",), True),
    ]
    assert argvs("A=$(rm x)") == [("rm", "x")]
    assert argvs("echo \"`rm x`\"") == [("echo", "`rm x`"), ("rm", "x")]


def test_xargs_marks_unknown_tail():
    assert argvs("find . | xargs -0 -n 1 -I {} rm {}") == [
        ("find", "."), ("rm", "{}", "__xargs__"),
    ]
    assert argvs("ls | xargs") == [("ls",), ("echo", "__xargs__")]
    assert argvs("sudo xargs rm") == [("rm", "__xargs__")]


def test_quotes_do_not_split():
    assert argvs("echo 'a && b' \"c; d\" e\\ f \"g\\\"h\" 'i|j'") == [
        ("echo", "a && b", "c; d", "e f", 'g"h', "i|j"),
    ]
    assert argvs('echo "a\nb"') == [("echo", "a\nb")]
    assert argvs("echo ab\\\ncd") == [("echo", "abcd")]
    assert argvs("echo $HOME ${HOME} ${X:-y} \"$HOME\" '$'") == [
        ("echo", "$HOME", "${HOME}", "${X:-y}", "$HOME", "$"),
    ]
    assert argvs("echo a # ; rm x") == [("echo", "a")]
    assert argvs('echo "#x" a#b') == [("echo", "#x", "a#b")]


def test_redirections_are_dropped_from_argv_and_targets_kept():
    cmds = parse("cat a > /quobyte/x 2>&1 >> log < in &> all 2>/dev/null")
    assert cmds[0].argv == ("cat", "a")
    assert cmds[0].redirects == ("/quobyte/x", "log", "in", "all", "/dev/null")
    assert parse("echo hi>f")[0].redirects == ("f",)
    assert parse("echo hi >&2")[0].redirects == ()
    assert parse("echo hi >& f")[0].redirects == ("f",)
    assert parse("echo hi >| f")[0].redirects == ("f",)
    assert parse("echo hi 2>&-")[0].redirects == ()
    assert parse("echo \"a>b\"")[0].redirects == ()


def test_bare_redirect_is_a_null_command_with_redirect():
    assert parse("> /quobyte/f") == [SimpleCommand((":",), (), False, ("/quobyte/f",))]
    cmds = parse("exec > /quobyte/f")
    assert cmds == [SimpleCommand((":",), (), False, ("/quobyte/f",))]


def test_assignment_only_is_skipped():
    assert parse("FOO=bar") == []
    assert argvs("FOO=bar; ls") == [("ls",)]


def test_redirect_on_shell_c_is_kept():
    cmds = parse("bash -c 'a' > /quobyte/o")
    assert [(c.argv, c.redirects) for c in cmds] == [(("a",), ()), ((":",), ("/quobyte/o",))]


def test_command_v_is_not_stripped():
    assert argvs("command -v rm") == [("command", "-v", "rm")]


def test_shell_without_dash_c_is_a_plain_command():
    assert argvs("bash script.sh") == [("bash", "script.sh")]
    assert argvs("./bash -c 'rm x'") == [("./bash", "-c", "rm x")]


def test_nonstandard_path_is_not_treated_as_wrapper():
    assert argvs("./sudo rm x") == [("./sudo", "rm", "x")]


def test_depth_four_is_allowed():
    cmd = "echo $(echo $(echo $(echo hi)))"
    assert len(parse(cmd)) == 4


NESTED_5 = "echo $(echo $(echo $(echo $(echo $(echo hi)))))"
NESTED_SH_5 = "bash -c \"bash -c 'bash -c \\\"bash -c \\\\\\\"bash -c true\\\\\\\"\\\"'\""


@pytest.mark.parametrize(
    "command",
    [
        "echo 'unterminated",
        'echo "unterminated',
        "echo \\",
        "echo $(unterminated",
        "echo `unterminated",
        "(a; b",
        "a; b)",
        "{ a; b",
        "a; }",
        "echo a (b)",
        "foo() { rm x; }",
        "A=(1 2)",
        "((1+1))",
        "echo $((1+1))",
        "eval 'rm x'",
        "sudo eval rm",
        "command eval rm",
        "source ./x.sh",
        ". ./x.sh",
        "trap 'rm x' EXIT",
        "cat <<EOF\nhi\nEOF",
        "cat <<-EOF",
        "cat <<< hi",
        "diff <(a) <(b)",
        "tee >(cat)",
        "echo ${x:-$(rm y)}",
        "echo ${x:-`rm y`}",
        "echo ${unterminated",
        "echo $'\\x72m'",
        "echo $\"hi\"",
        "echo a\x00b",
        "echo a" + "b" * 9000,
        NESTED_5,
        NESTED_SH_5,
        "",
        "   \n\t ",
        "echo a\rb",
        "if true; then rm x; fi",
        "while true; do rm x; done",
        "for i in 1; do rm x; done",
        "case x in x) rm y;; esac",
        "! rm x",
        "$X -rf /",
        "$(echo rm) x",
        "\"$CMD\" x",
        "''",
        "env -S 'rm x'",
        "env --bogus rm",
        "sudo -s 'rm x'",
        "sudo -i",
        "sudo -l",
        "sudo --bogus rm",
        "sudo -u",
        "nice -n",
        "timeout",
        "xargs -z rm",
        "xargs --bogus rm",
        "bash -c",
        "bash -c -o",
        "bash --rcfile x -c y",
        "echo a >",
        "echo a > && b",
        "echo a ;; b",
        "cat < <(x)",
        "echo >(x)",
        "time -x ls",
        "stdbuf -z ls",
        "ionice -p 1 ls",
    ],
)
def test_unparseable_raises(command):
    with pytest.raises(Unparseable):
        parse(command)


def test_non_string_raises():
    with pytest.raises(Unparseable):
        parse(None)  # type: ignore[arg-type]


def test_exactly_8192_chars_is_allowed():
    assert len(parse("a" * 8192)) == 1


def test_substitution_text_inside_single_quotes_is_literal_even_with_parens():
    assert argvs("echo '(' ')' \"(\" \")\"") == [("echo", "(", ")", "(", ")")]


# ---- fix round 1 ----

@pytest.mark.parametrize(
    "command",
    [
        "x='$(touch P)'; echo ${x@P}",
        "echo ${x@P}",
        "echo ${x@Q}",
        "echo $[1+1]",
        "echo ${a[$(touch P)]}",
        "echo ${a[0]}",
        "echo ${!x}",
        "a[$(touch P)]=1",
        "PS4='$(touch P)' set -x",
        "PS4='$(touch P)' bash -xc true",
        "PS4=x bash -xc true",
        "[ -v 'a[$(touch P)]' ]",
        "[ -f x ]",
        "[[ -f x ]]",
        "printf -v x %s y",
        "declare -n r=x",
        "typeset x",
        "local x",
        "read x",
        "unset x",
        "test -v x",
        "set -x",
        "shopt -s x",
        "alias a=b",
        "export A=1",
        "readonly A=1",
        "mapfile a",
        "readarray a",
        "getopts a b",
        "hash -p x y",
        "enable -n x",
        "let 'x=1'",
        "builtin printf -v x y",
        "command declare x",
        "echo '$(touch P)'",
        "echo '`touch P`'",
        "echo \\`x\\`",
        "echo $\\(x)",
        "echo '$'\"(\"x",
        "A='$(x)' git status",
        "A=1 env BASH_ENV=/tmp/x ls",
        "env LD_PRELOAD=/x ls",
        "sudo PATH=/x ls",
        "LD_LIBRARY_PATH=/x ls",
        "PROMPT_COMMAND=x ls",
        "IFS=x ls",
        "SHELLOPTS=xtrace ls",
        "BASHOPTS=x ls",
        "ENV=x ls",
        "PATH=/x ls",
        "bash -c 'echo hi' '$(x)'",
        "cat > '$(x)'",
        "x\"" + "$(" * 2700,
        "'\"$(" * 2700,
    ],
)
def test_fix1_late_evaluation_is_unparseable(command):
    with pytest.raises(Unparseable):
        parse(command)


def test_sh_dash_c_string_with_substitution_still_recursed():
    assert argvs("bash -c 'echo $(date)'") == [("echo", "$(date)"), ("date",)]


@pytest.mark.parametrize(
    "command",
    ["g{i,}t push -f", "git push {,--force}", "git $X", "git ${X}", "rm *", "rm -rf $HOME/..", "~/x",
     "ls a?", "ls [ab]", "echo \"$X\"", "git $(echo push)", "git {1..3}", "echo $1", "echo $@",
     "echo hi > $F"],
)
def test_dynamic_commands_are_flagged(command):
    cmds = parse(command)
    assert cmds[0].dynamic is True


@pytest.mark.parametrize(
    "command",
    ["git push 'a*'", "git push \"a*\"", "echo '$X'", "echo \\* \\~", "echo a~b", "echo {a}", "echo $ x",
     "git push origin main", "echo \"{a,b}\""],
)
def test_static_commands_are_not_dynamic(command):
    assert parse(command)[0].dynamic is False


def test_dynamic_does_not_leak_between_commands():
    cmds = parse("echo $X; ls")
    assert [c.dynamic for c in cmds] == [True, False]


def test_length_cap_counts_utf8_bytes():
    with pytest.raises(Unparseable):
        parse("\u00e9" * 5000)
    assert len(parse("\u00e9" * 4096)) == 1


def test_quoted_assignment_lookalike_is_a_command_head():
    assert parse('"A=1" git push -f')[0].argv == ("A=1", "git", "push", "-f")
    assert parse("\\cmdD=1 x")[0].argv == ("cmdD=1", "x")
    assert parse("'A'=1 x")[0].argv == ("A=1", "x")
    assert parse("A=1 x")[0].env == (("A", "1"),)
    assert parse('A="b c" x')[0].env == (("A", "b c"),)
    assert parse('env "A=1" x')[0].argv == ("A=1", "x")


def test_fuzz_only_parses_or_raises_unparseable():
    import random

    rng = random.Random(1234)
    alphabet = ["$(", ")", "(", "`", "'", '"', "\\", "$", "{", "}", "${", ";", "&", "|", ">", "<", "\n", " ",
                "a", "bash -c ", "sudo ", "env ", "=", "#", "$((", "<<", "x", "[", "*"]
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 60)))
        try:
            result = parse(text)
        except Unparseable:
            continue
        assert isinstance(result, list)
    for text in ("$(" * 3000, '"$(' * 2700, "`" * 5000, "(" * 4000, "{ " * 4000, "'" * 8000):
        try:
            parse(text)
        except Unparseable:
            pass
