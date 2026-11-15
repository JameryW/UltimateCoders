"""Bounded git history deepening: pure parsers, bounds and offline tolerance.

The deepening reads repository history read-only and is split into pure parsers
plus a thin git runner, so these tests exercise the parsers directly (no
repository needed) and only assert that the runner degrades to an empty
deepening instead of raising.
"""

from __future__ import annotations

from ultimate_coders.knowledge.deepen import (
    CommitInfo,
    FileChurn,
    GitDeepen,
    deepen_for_scope,
    deepen_from_log,
    git_deepen,
    parse_git_log_numstat,
    parse_git_numstat,
    parse_git_paths,
    summarize_deepen,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
FIELD_SEP = "\x1f"
RECORD_SEP = "\x00"


def _header(
    sha: str,
    *,
    parents: str = "",
    author: str = "Ada",
    when: str = "2024-01-02T03:04:05+00:00",
    subject: str = "change",
) -> str:
    return FIELD_SEP.join([sha, parents, author, when, subject]) + RECORD_SEP


def _record(
    sha: str,
    numstat: list[str],
    *,
    parents: str = "",
    author: str = "Ada",
    when: str = "2024-01-02T03:04:05+00:00",
    subject: str = "change",
) -> str:
    header = _header(sha, parents=parents, author=author, when=when, subject=subject)
    body = "".join(f"{line}\n" for line in numstat)
    return f"{header}\n{body}"


LOG = (
    _record(
        SHA_A,
        ["10\t2\tsrc/a.py", "4\t0\tsrc/b.py"],
        parents=f"{SHA_B} {SHA_C}",
        subject="newest",
    )
    + _record(SHA_B, ["3\t1\tsrc/a.py", "-\t-\timg/x.png"], subject="middle")
    + _record(SHA_C, ["0\t0\tdocs/readme.md"], subject="oldest")
)


def test_parse_git_log_numstat_parses_commits_and_churn() -> None:
    commits, churn, truncated = parse_git_log_numstat(LOG)

    assert [commit.sha for commit in commits] == [SHA_A, SHA_B, SHA_C]
    assert commits[0].parents == (SHA_B, SHA_C)
    assert commits[0].author == "Ada"
    assert commits[0].subject == "newest"
    assert commits[0].committed_at == "2024-01-02T03:04:05+00:00"
    assert truncated is False

    by_path = {entry.path: entry for entry in churn}
    assert by_path["src/a.py"].commits == 2
    assert by_path["src/a.py"].additions == 13
    assert by_path["src/a.py"].deletions == 3
    assert by_path["img/x.png"].commits == 1
    assert by_path["img/x.png"].lines_changed == 0
    assert churn[0].path == "src/a.py"


def test_parse_git_log_numstat_tolerates_malformed_and_crlf() -> None:
    noisy = (
        "10\t2\tsrc/orphan.py\n"
        + "garbage without a separator\n"
        + _record("nothexsha", ["1\t1\tbad.py"])
        + _record(SHA_A, ["junk line", "1\t2\tsrc/a.py"])
    )

    commits, churn, _truncated = parse_git_log_numstat(noisy)

    assert [commit.sha for commit in commits] == [SHA_A]
    assert [entry.path for entry in churn] == ["src/a.py"]
    assert "src/orphan.py" not in {entry.path for entry in churn}

    crlf_commits, crlf_churn, _ = parse_git_log_numstat(LOG.replace("\n", "\r\n"))
    assert crlf_commits == parse_git_log_numstat(LOG)[0]
    assert crlf_churn == parse_git_log_numstat(LOG)[1]


def test_parse_git_log_numstat_is_commit_atomic_under_bounds() -> None:
    one_commit, churn, truncated = parse_git_log_numstat(LOG, max_commits=1)

    assert [commit.sha for commit in one_commit] == [SHA_A]
    assert truncated is True
    assert {entry.path for entry in churn} == {"src/a.py", "src/b.py"}

    assert parse_git_log_numstat(LOG, max_commits=0)[0] == ()
    assert parse_git_log_numstat(LOG, max_commits=0)[2] is True

    path_limited = _record(SHA_A, ["1\t0\ta.py", "1\t0\tb.py", "1\t0\tc.py"])
    commits, limited_churn, limited = parse_git_log_numstat(path_limited, max_paths=2)
    assert len(commits) == 1
    assert limited is True
    assert {entry.path for entry in limited_churn} == {"a.py", "b.py"}

    oversized = _record(SHA_A, ["10\t2\tsrc/a.py"])
    commits, churn, truncated = parse_git_log_numstat(oversized, max_lines_changed=5)
    assert commits == ()
    assert churn == ()
    assert truncated is True


def test_parse_git_log_numstat_ignores_non_string_input() -> None:
    assert parse_git_log_numstat(None) == ((), (), False)
    assert parse_git_log_numstat(123) == ((), (), False)


def test_parse_git_numstat_ranks_paths_and_tolerates_binary() -> None:
    numstat = (
        "12\t3\tsrc/a.py\n"
        "-\t-\tassets/logo.png\n"
        "\n"
        "4\t0\tsrc/b.py\n"
        "bad line\n"
        "0\t0\tdocs/readme.md\n"
    )

    churn = parse_git_numstat(numstat)

    assert [(entry.path, entry.additions, entry.deletions) for entry in churn] == [
        ("src/a.py", 12, 3),
        ("src/b.py", 4, 0),
        ("assets/logo.png", 0, 0),
        ("docs/readme.md", 0, 0),
    ]
    assert churn[0].lines_changed == 15
    assert churn[2].commits == 1


def test_parse_git_numstat_bounds_and_normalizes_paths() -> None:
    numstat = "12\t3\tsrc/a.py\n-\t-\tassets/logo.png\n4\t0\tsrc/b.py\n"

    limited = parse_git_numstat(numstat, max_paths=2)
    assert [entry.path for entry in limited] == ["src/a.py", "assets/logo.png"]

    budget = parse_git_numstat(numstat, max_lines_changed=0)
    assert [entry.path for entry in budget] == ["src/a.py"]

    assert parse_git_numstat("1\t1\told.py => new.py\n")[0].path == "new.py"
    assert parse_git_numstat("1\t1\tsrc/{old => new name}/c.py\n")[0].path == "src/new name/c.py"
    assert parse_git_numstat('1\t1\t"src/a b.py"\n')[0].path == "src/a b.py"
    assert parse_git_numstat("1\t1\t./src/c.py\n")[0].path == "src/c.py"
    assert parse_git_numstat(None) == ()
    assert parse_git_numstat(123) == ()


def test_parse_git_paths() -> None:
    assert parse_git_paths("b.py\na.py\n./c.py\n\nb.py\n") == ("b.py", "a.py", "c.py")
    assert parse_git_paths("b.py\na.py\nc.py\n", max_paths=2) == ("b.py", "a.py")
    assert parse_git_paths(None) == ()
    assert parse_git_paths(123) == ()


def test_deepen_from_log_derives_head_and_paths() -> None:
    deepen = deepen_from_log(LOG, base_ref="origin/main")

    assert deepen.base_ref == "origin/main"
    assert deepen.head == SHA_A
    assert [commit.sha for commit in deepen.recent_commits] == [SHA_A, SHA_B, SHA_C]
    assert deepen.paths == ("docs/readme.md", "img/x.png", "src/a.py", "src/b.py")
    assert deepen.truncated is False
    assert deepen.is_empty is False
    assert deepen.total_lines_changed() == 20

    assert deepen_from_log(LOG, head=SHA_C.upper()).head == SHA_C
    assert deepen_from_log("").is_empty is True
    assert deepen_from_log(LOG, max_commits=1).truncated is True
    assert deepen_from_log(None).base_ref == "HEAD"


def test_git_deepen_records_round_trip_and_tolerate_junk() -> None:
    deepen = deepen_from_log(LOG)

    assert GitDeepen.from_dict(deepen.to_dict()) == deepen
    assert deepen.to_summary() == {
        "head": SHA_A[:12],
        "commits": 3,
        "paths": 4,
        "lines_changed": 20,
        "truncated": False,
    }
    assert set(deepen.to_dict()) == {
        "base_ref",
        "head",
        "recent_commits",
        "churn",
        "paths",
        "truncated",
    }

    tolerant = GitDeepen.from_dict(
        {
            "recent_commits": "junk",
            "churn": [None, {"path": "x.py"}],
            "paths": [1, "x.py", "./x.py"],
            "head": "zzz",
        }
    )
    assert tolerant.head == ""
    assert [entry.path for entry in tolerant.churn] == ["x.py"]
    assert tolerant.paths == ("x.py",)

    duplicate = GitDeepen(recent_commits=(CommitInfo(SHA_A), CommitInfo(SHA_A.upper())))
    assert len(duplicate.recent_commits) == 1

    ranked = GitDeepen(
        churn=(FileChurn("z.py", commits=1, additions=5), FileChurn("a.py", commits=2))
    )
    assert [entry.path for entry in ranked.churn] == ["a.py", "z.py"]


def test_commit_info_and_file_churn_are_bounded() -> None:
    commit = CommitInfo(
        sha="ABCDEF12",
        parents=(SHA_A, SHA_A.upper()),
        author="A" * 300,
        committed_at=None,
        subject="s" * 500,
    )

    assert commit.sha == "abcdef12"
    assert commit.parents == (SHA_A,)
    assert len(commit.author) == 120
    assert len(commit.subject) <= 200
    assert commit.subject.endswith("...")
    assert commit.short_sha() == "abcdef12"
    assert CommitInfo(sha="short").sha == ""
    assert "A" * 300 not in str(commit.to_summary())

    entry = FileChurn("x.py", commits=-3, additions=-1, deletions="bad")
    assert entry.commits == 0
    assert entry.additions == 0
    assert entry.deletions == 0
    assert entry.lines_changed == 0


def test_summarize_deepen_is_log_safe() -> None:
    deepen = deepen_from_log(LOG)

    summary = summarize_deepen(deepen)

    assert summary == {"commits": 3, "paths": 4, "lines_changed": 20, "truncated": False}
    assert "src/a.py" not in str(summary)
    assert "newest" not in str(summary)
    assert summarize_deepen(None) == {
        "commits": 0,
        "paths": 0,
        "lines_changed": 0,
        "truncated": False,
    }
    assert summarize_deepen(
        {"commits": 5, "paths": 2, "lines_changed": 30, "truncated": True}
    ) == {"commits": 5, "paths": 2, "lines_changed": 30, "truncated": True}


def test_deepen_for_scope_labels_facts_without_inventing_attribution() -> None:
    class Scope:
        def key(self) -> str:
            return "tenant:repo:main"

    projection = deepen_for_scope(deepen_from_log(LOG), scope=Scope(), max_commits=1, max_paths=2)

    assert projection["scope"] == "tenant:repo:main"
    assert projection["commit_count"] == 3
    assert len(projection["commits"]) == 1
    assert projection["path_count"] == 4
    assert projection["paths"] == ["docs/readme.md", "img/x.png"]
    assert projection["head"] == SHA_A[:12]
    assert projection["lines_changed"] == 20
    assert projection["base_ref"] == "HEAD"

    assert deepen_for_scope(None, scope=None)["scope"] == ""


def test_git_deepen_never_raises_offline(tmp_path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    # A broken gitdir marker keeps discovery from escaping into a parent repo.
    (work / ".git").write_text("gitdir: /nonexistent-uc-test\n", encoding="utf-8")

    deepen = git_deepen(work, base_ref="main")

    assert isinstance(deepen, GitDeepen)
    assert deepen.base_ref == "main"
    assert deepen.recent_commits == ()
    assert deepen.churn == ()

    missing = git_deepen(tmp_path / "does-not-exist")
    assert missing.base_ref == "HEAD"
    assert missing.is_empty is True
