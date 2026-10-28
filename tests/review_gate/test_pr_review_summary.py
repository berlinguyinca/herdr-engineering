from herdr_engineering.review_gate.github_app import GitHubAppClient

SHA = "a" * 40


class C(GitHubAppClient):
    def __init__(self, reviews=None, commits=None):
        self._r, self._c = reviews or [], commits or []

    def _send(self, method, path, *, body=None, bearer=None):
        return self._r if "/reviews" in path else self._c


def rv(login, state, sha=SHA, assoc="MEMBER", typ="User"):
    return {"user": {"login": login, "type": typ}, "state": state, "commit_id": sha, "author_association": assoc}


def test_only_current_head_member_people_count():
    c = C([rv("a", "APPROVED"), rv("bot", "APPROVED", typ="Bot"), rv("old", "APPROVED", sha="b" * 40),
           rv("rando", "APPROVED", assoc="NONE")])
    assert c.pr_review_summary("github.com/o/r", 1, SHA) == (1, False)


def test_latest_review_wins_and_changes_requested_flags():
    c = C([rv("a", "APPROVED"), rv("a", "CHANGES_REQUESTED"), rv("b", "APPROVED")])
    assert c.pr_review_summary("github.com/o/r", 1, SHA) == (1, True)
    assert C([rv("a", "CHANGES_REQUESTED"), rv("a", "DISMISSED")]).pr_review_summary("github.com/o/r", 1, SHA) == (0, False)


def test_commit_signatures_must_all_verify_and_end_at_head():
    ok = [{"sha": "1" * 40, "commit": {"verification": {"verified": True}}}, {"sha": SHA, "commit": {"verification": {"verified": True}}}]
    assert C(commits=ok).pr_commits_verified("github.com/o/r", 1, SHA) is True
    bad = [dict(ok[0], commit={"verification": {"verified": False}}), ok[1]]
    assert C(commits=bad).pr_commits_verified("github.com/o/r", 1, SHA) is False
    assert C(commits=ok[:1]).pr_commits_verified("github.com/o/r", 1, SHA) is False     # does not end at head
    assert C(commits=[]).pr_commits_verified("github.com/o/r", 1, SHA) is False


def test_merge_pr_sha_returns_the_merge_commit_or_none_when_refused():
    from herdr_engineering.review_gate.github_app import GitHubAppError

    class M(GitHubAppClient):
        def __init__(self, result):
            self.result = result

        def _send(self, method, path, *, body=None, bearer=None):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    assert M({"sha": "C" * 40, "merged": True}).merge_pr_sha("github.com/o/r", 1, SHA) == "c" * 40
    assert M({}).merge_pr_sha("github.com/o/r", 1, SHA) == ""                  # merged, id not reported
    assert M(GitHubAppError("not mergeable", status=405)).merge_pr_sha("github.com/o/r", 1, SHA) is None
    assert M({"sha": "c" * 40}).merge_pr("github.com/o/r", 1, SHA) is True


def test_author_approval_excluded_and_over_500_reviews_fail_closed():
    c = C([rv("author", "APPROVED"), rv("a", "APPROVED")])
    assert c.pr_review_summary("github.com/o/r", 1, SHA, exclude=frozenset({"author"})) == (1, False)

    class Many(C):
        def _send(self, method, path, *, body=None, bearer=None):
            return [rv(f"u{n}", "APPROVED") for n in range(100)] if "/reviews" in path else []
    assert Many().pr_review_summary("github.com/o/r", 1, SHA) == (0, True)
