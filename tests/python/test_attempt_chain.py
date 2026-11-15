"""Attempt diff-chain ledger: parsing, symbol location and immutable rounds."""

from __future__ import annotations

import json

import pytest
from ultimate_coders.knowledge.attempt_chain import (
    NAMESPACE,
    AttemptChainConflictError,
    AttemptChainLedger,
    AttemptRound,
    FilePatch,
    SymbolChange,
    locate_symbols,
    parse_git_diff,
)
from ultimate_coders.knowledge.model import AttemptRef
from ultimate_coders.runtime_state import RuntimeState


@pytest.fixture(autouse=True)
def stable_capture_bounds(monkeypatch):
    """Pin rollout defaults so developer env vars never change test expectations."""

    monkeypatch.setenv("UC_KNOWLEDGE_MAX_PATCH_FILES", "20")
    monkeypatch.setenv("UC_KNOWLEDGE_MAX_DIFF_CHARS", "20000")


def make_ledger(tmp_path) -> AttemptChainLedger:
    return AttemptChainLedger(RuntimeState(tmp_path / "state.db", database_url=""))


def modified_diff(path: str, old_lines: list[str], new_lines: list[str]) -> str:
    body = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,{len(old_lines)} +1,{len(new_lines)} @@",
    ]
    body.extend("-" + line for line in old_lines)
    body.extend("+" + line for line in new_lines)
    return "\n".join(body) + "\n"


def created_diff(path: str, new_lines: list[str]) -> str:
    body = [
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "index 0000000..1111111",
        "--- /dev/null",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(new_lines)} @@",
    ]
    body.extend("+" + line for line in new_lines)
    return "\n".join(body) + "\n"


def deleted_diff(path: str, old_lines: list[str]) -> str:
    body = [
        f"diff --git a/{path} b/{path}",
        "deleted file mode 100644",
        f"--- a/{path}",
        "+++ /dev/null",
        f"@@ -1,{len(old_lines)} +0,0 @@",
    ]
    body.extend("-" + line for line in old_lines)
    return "\n".join(body) + "\n"


def renamed_diff(old_path: str, new_path: str) -> str:
    return "\n".join(
        [
            f"diff --git a/{old_path} b/{new_path}",
            "similarity index 95%",
            f"rename from {old_path}",
            f"rename to {new_path}",
            "",
        ]
    )


def make_round(ref: AttemptRef, **overrides) -> AttemptRound:
    payload = {"attempt": ref, "outcome": "success", "patches": ()}
    payload.update(overrides)
    return AttemptRound(**payload)


# ---------------------------------------------------------------------------
# parse_git_diff
# ---------------------------------------------------------------------------


def test_parse_git_diff_captures_all_change_types():
    text = (
        created_diff("new_module.py", ["def alpha():", "    return 1", ""])
        + modified_diff("src/app.py", ["OLD = 1"], ["NEW = 2", "EXTRA = 3"])
        + deleted_diff("old_name.txt", ["gone", "gone again"])
        + renamed_diff("pkg/old.py", "pkg/new.py")
    )
    patches = parse_git_diff(text, max_files=10, max_diff_chars=100_000)

    assert [patch.path for patch in patches] == [
        "new_module.py",
        "old_name.txt",
        "pkg/new.py",
        "src/app.py",
    ]
    by_path = {patch.path: patch for patch in patches}
    assert by_path["new_module.py"].change_type == "added"
    assert by_path["new_module.py"].additions == 3
    assert by_path["new_module.py"].deletions == 0
    assert by_path["old_name.txt"].change_type == "deleted"
    assert by_path["old_name.txt"].deletions == 2
    assert by_path["pkg/new.py"].change_type == "renamed"
    assert by_path["pkg/new.py"].old_path == "pkg/old.py"
    assert by_path["src/app.py"].change_type == "modified"
    assert (by_path["src/app.py"].additions, by_path["src/app.py"].deletions) == (2, 1)
    assert "diff --git" in by_path["src/app.py"].diff
    assert "@@ -1,1 +1,2 @@" in by_path["src/app.py"].diff
    assert all(not patch.truncated for patch in patches)
    # Line-level symbol ranges are captured together with the patch by default.
    assert by_path["src/app.py"].symbols
    assert by_path["src/app.py"].symbols[0].kind == "line"


def test_parse_git_diff_is_deterministic_and_capped_by_max_files():
    text = (
        modified_diff("b.py", ["old"], ["new"])
        + modified_diff("a.py", ["old"], ["new"])
        + modified_diff("c.py", ["old"], ["new"])
    )
    full = parse_git_diff(text, max_files=10, max_diff_chars=100_000)
    assert [patch.path for patch in full] == ["a.py", "b.py", "c.py"]

    capped = parse_git_diff(text, max_files=2, max_diff_chars=100_000)
    assert [patch.path for patch in capped] == ["a.py", "b.py"]
    assert capped == parse_git_diff(text, max_files=2, max_diff_chars=100_000)


def test_parse_git_diff_clips_total_diff_budget():
    text = modified_diff("a.txt", ["first"], ["second"]) + modified_diff(
        "b.txt", ["left"], ["right"]
    )
    patches = parse_git_diff(text, max_files=10, max_diff_chars=40)

    assert len(patches) == 2
    assert sum(len(patch.diff) for patch in patches) == 40
    assert patches[0].truncated is True
    assert len(patches[0].diff) == 40
    assert patches[1].diff == ""
    assert patches[1].truncated is True
    # Hunk statistics and symbol ranges come from the full section, not the clip.
    assert (patches[0].additions, patches[0].deletions) == (1, 1)
    assert patches[1].symbols

    generous = parse_git_diff(text, max_files=10, max_diff_chars=100_000)
    assert all(not patch.truncated for patch in generous)
    assert sum(len(patch.diff) for patch in generous) < 100_000


def test_parse_git_diff_never_raises_on_malformed_input():
    garbage = "not a diff at all\n@@ nonsense @@\n+orphan\n"
    valid = modified_diff("src/app.py", ["old"], ["new"])
    patches = parse_git_diff(garbage + valid, max_files=10, max_diff_chars=10_000)
    assert len(patches) == 1
    assert patches[0].path == "src/app.py"

    assert parse_git_diff(None) == ()
    assert parse_git_diff(12345) == ()

    broken_hunk = parse_git_diff(
        "diff --git a/x b/x\n@@ not a hunk @@\n+still text\n",
        max_files=5,
        max_diff_chars=500,
    )
    assert isinstance(broken_hunk, tuple)
    assert broken_hunk[0].path == "x"

    missing_plus = parse_git_diff("diff --git a/y b/y\n--- a/y\n", max_files=5, max_diff_chars=500)
    assert isinstance(missing_plus, tuple)
    assert missing_plus[0].path == "y"


# ---------------------------------------------------------------------------
# locate_symbols
# ---------------------------------------------------------------------------

MODULE_SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def top(value):\n"
    "    return value + 1\n"
    "\n"
    "\n"
    "class Widget:\n"
    "    def render(self):\n"
    '        return "x"\n'
    "\n"
    "\n"
    "async def fetch():\n"
    "    return None\n"
)

MODULE_DIFF = "\n".join(
    [
        "diff --git a/mod.py b/mod.py",
        "--- a/mod.py",
        "+++ b/mod.py",
        "@@ -1,1 +1,1 @@",
        "-import os",
        "+import sys",
        "@@ -5,1 +5,1 @@",
        "-    return value + 1",
        "+    return value + 2",
        "@@ -8,1 +8,1 @@",
        "-class Widget:",
        "+class Widget(object):",
        "@@ -10,1 +10,2 @@",
        '-        return "x"',
        '+        return "y"',
        "+        # extra",
        "@@ -14,1 +14,1 @@",
        "-    return None",
        "+    return 42",
    ]
)


def test_locate_symbols_python_ast_function_class_method(tmp_path):
    source_path = tmp_path / "module_under_test.py"
    source_path.write_text(MODULE_SOURCE, encoding="utf-8")
    source_text = source_path.read_text(encoding="utf-8")

    symbols = locate_symbols(str(source_path), MODULE_DIFF, source_text=source_text)

    assert [(item.symbol, item.kind, item.start_line, item.end_line) for item in symbols] == [
        ("", "line", 1, 1),
        ("top", "function", 4, 5),
        ("Widget", "class", 8, 10),
        ("Widget.render", "method", 9, 10),
        ("fetch", "function", 13, 14),
    ]
    assert all(item.path == str(source_path) for item in symbols)
    assert len(locate_symbols(str(source_path), MODULE_DIFF, source_text=source_text,
                              max_symbols=2)) == 2


def test_locate_symbols_uses_line_fallback_for_non_python():
    diff = "\n".join(
        [
            "diff --git a/docs/notes.txt b/docs/notes.txt",
            "--- a/docs/notes.txt",
            "+++ b/docs/notes.txt",
            "@@ -3,2 +3,3 @@",
            " line",
            "-old",
            "+new",
            "+extra",
            "@@ -10,1 +11,1 @@",
            "-last",
            "+last changed",
        ]
    )
    symbols = locate_symbols("docs/notes.txt", diff)
    assert [(item.kind, item.start_line, item.end_line) for item in symbols] == [
        ("line", 3, 5),
        ("line", 11, 11),
    ]
    assert all(item.symbol == "" for item in symbols)


def test_locate_symbols_falls_back_when_source_is_not_valid_python():
    diff = modified_diff("broken.py", ["x"], ["y"])
    symbols = locate_symbols("broken.py", diff, source_text="def broken(:\n")
    assert symbols
    assert all(item.kind == "line" for item in symbols)
    assert locate_symbols("broken.py", "") == ()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_file_patch_and_attempt_round_json_round_trip():
    symbol = SymbolChange(
        path="src/app.py", symbol="main", kind="function", start_line=3, end_line=8
    )
    patch = FilePatch(
        path="src/app.py",
        change_type="modified",
        additions=2,
        deletions=1,
        diff="@@ -3,1 +3,2 @@\n-old\n+new\n+extra\n",
        truncated=True,
        symbols=(symbol,),
    )
    renamed = FilePatch(path="pkg/new.py", change_type="renamed", old_path="pkg/old.py")
    record = AttemptRound(
        attempt=AttemptRef("task-1", "sub-1", 1, 2, worker_id="w", worker_epoch=3, graph_id="g"),
        outcome="success",
        patches=(patch, renamed),
        base_revision="base-sha",
        head_revision="head-sha",
        summary="did things",
        failure="",
        test_verdict="pass",
        created_at="2026-11-15T00:00:00+00:00",
        record_version=7,
    )

    restored = AttemptRound.from_dict(json.loads(json.dumps(record.to_dict())))
    restored_patch = FilePatch.from_dict(json.loads(json.dumps(patch.to_dict())))

    assert restored == record
    assert restored_patch == patch
    assert isinstance(record.to_dict()["patches"], list)
    assert SymbolChange.from_dict(symbol.to_dict()) == symbol


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_record_replay_is_idempotent(tmp_path):
    ledger = make_ledger(tmp_path)
    patch = FilePatch(path="src/app.py", additions=1, deletions=1, diff="-a\n+b\n")
    ref = AttemptRef("task-1", "sub-1", 0, 0)

    first = ledger.record(make_round(ref, patches=(patch,)))
    second = ledger.record(make_round(ref, patches=(patch,)))

    assert first.key() == "task-1:sub-1:0:0"
    assert second.created_at == first.created_at
    assert second.record_version == first.record_version == 1
    assert len(ledger.state.query(NAMESPACE, limit=10)) == 1


def test_record_conflicting_content_raises_and_keeps_original(tmp_path):
    ledger = make_ledger(tmp_path)
    ref = AttemptRef("task-1", "sub-1", 0, 0)
    first = ledger.record(make_round(ref, summary="first"))

    with pytest.raises(AttemptChainConflictError):
        ledger.record(make_round(ref, summary="second"))

    stored = ledger.get(ref)
    assert stored is not None
    assert stored.summary == "first"
    assert stored.record_version == first.record_version
    assert len(ledger.state.query(NAMESPACE, limit=10)) == 1


def test_get_accepts_ref_and_string_key(tmp_path):
    ledger = make_ledger(tmp_path)
    ref = AttemptRef("task-1", "sub-1", 2, 1)
    recorded = ledger.record(make_round(ref, summary="kept"))

    assert ledger.get(ref) == recorded
    assert ledger.get(ref.key()) == recorded
    assert ledger.get(AttemptRef("task-1", "sub-1", 9, 9)) is None
    assert ledger.get("task-1:sub-1:0:0") is None
    assert ledger.get("") is None


def test_prior_rounds_is_strictly_ordered_history(tmp_path):
    ledger = make_ledger(tmp_path)
    refs = [AttemptRef("t", "s", dispatch, attempt) for dispatch, attempt in [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
        (2, 0),
    ]]
    for ref in refs:
        ledger.record(make_round(ref, summary=ref.key()))

    current = refs[2]
    prior = ledger.prior_rounds(current)
    assert [record.key() for record in prior] == [refs[0].key(), refs[1].key()]
    assert [record.key() for record in ledger.prior_rounds(current, limit=1)] == [
        refs[1].key()
    ]
    assert ledger.prior_rounds(refs[0]) == []
    assert all(
        record.key() != current.key() for record in ledger.prior_rounds(current, limit=50)
    )


def test_rounds_filters_latest_and_limit(tmp_path):
    ledger = make_ledger(tmp_path)
    for dispatch in range(2):
        ledger.record(make_round(AttemptRef("t1", "s1", dispatch, 0)))
    ledger.record(make_round(AttemptRef("t1", "s2", 0, 0)))
    ledger.record(make_round(AttemptRef("t2", "s1", 0, 0)))

    rounds = ledger.rounds(task_id="t1", subtask_id="s1")
    assert [record.key() for record in rounds] == ["t1:s1:0:0", "t1:s1:1:0"]
    assert [record.key() for record in ledger.rounds(task_id="t1", subtask_id="s1", limit=1)] == [
        "t1:s1:1:0"
    ]
    assert ledger.rounds(task_id="t1", subtask_id="s1", limit=0) == []
    assert ledger.rounds(task_id="t2", subtask_id="s2") == []

    latest = ledger.latest(task_id="t1", subtask_id="s1")
    assert latest is not None
    assert latest.key() == "t1:s1:1:0"
    assert ledger.latest(task_id="t1", subtask_id="s2").key() == "t1:s2:0:0"
    assert ledger.latest(task_id="missing", subtask_id="s1") is None


def test_record_bounds_patches_and_diffs(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_KNOWLEDGE_MAX_PATCH_FILES", "2")
    monkeypatch.setenv("UC_KNOWLEDGE_MAX_DIFF_CHARS", "600")
    ledger = make_ledger(tmp_path)
    ref = AttemptRef("task-1", "sub-1", 0, 0)
    oversized = tuple(
        FilePatch(path=f"file{index}.py", additions=1, diff="x" * 400) for index in range(3)
    )

    stored = ledger.record(make_round(ref, outcome="failed", patches=oversized))

    assert stored.file_count == 2
    assert stored.patches[0].truncated is False
    assert stored.patches[1].truncated is True
    assert sum(len(patch.diff) for patch in stored.patches) == 600
    assert ledger.get(ref) == stored
    assert len(json.dumps(stored.to_dict())) < 2000


def test_clear_removes_one_subtask_chain(tmp_path):
    ledger = make_ledger(tmp_path)
    for attempt in range(2):
        ledger.record(make_round(AttemptRef("t1", "s1", 0, attempt)))
    ledger.record(make_round(AttemptRef("t1", "s2", 0, 0)))

    assert ledger.clear(task_id="t1", subtask_id="s1") == 2
    assert ledger.rounds(task_id="t1", subtask_id="s1") == []
    assert ledger.latest(task_id="t1", subtask_id="s1") is None
    assert ledger.get(AttemptRef("t1", "s1", 0, 0)) is None
    assert ledger.clear(task_id="t1", subtask_id="s1") == 0
    assert ledger.clear(task_id="t1", subtask_id="missing") == 0

    assert [record.key() for record in ledger.rounds(task_id="t1", subtask_id="s2")] == [
        "t1:s2:0:0"
    ]
    # The tombstone allows the attempt to be recorded again after a clear.
    recorded = ledger.record(make_round(AttemptRef("t1", "s1", 0, 0), summary="again"))
    assert recorded.summary == "again"
    assert ledger.get(AttemptRef("t1", "s1", 0, 0)).summary == "again"


# ---------------------------------------------------------------------------
# Log-safe views
# ---------------------------------------------------------------------------


def test_file_patch_summary_and_round_helpers():
    patch = FilePatch(
        path="src/app.py",
        change_type="modified",
        additions=2,
        deletions=1,
        diff="@@ -1,1 +1,1 @@\n-SECRET\n+SECRET2\n",
        truncated=True,
        symbols=(
            SymbolChange(
                path="src/app.py", symbol="main", kind="function", start_line=3, end_line=4
            ),
        ),
    )
    summary = patch.summary()
    assert "\n" not in summary
    assert "SECRET" not in summary
    assert summary == "M src/app.py +2 -1 symbols=main truncated"

    renamed = FilePatch(path="pkg/new.py", change_type="renamed", old_path="pkg/old.py")
    assert renamed.summary().startswith("R pkg/old.py->pkg/new.py")
    assert patch.tokens() > 0
    assert patch.size_tokens == patch.tokens()

    ref = AttemptRef("t", "s", 0, 0)
    record = AttemptRound(
        attempt=ref,
        outcome="failed",
        patches=(patch, renamed),
        failure="pytest failed",
        summary="one round",
    )
    assert record.file_count == 2
    assert record.changed_paths == ("src/app.py", "pkg/new.py")
    assert record.is_failure is True
    assert make_round(ref, outcome="success").is_failure is False
    assert make_round(ref, outcome="unknown", failure="boom").is_failure is True
