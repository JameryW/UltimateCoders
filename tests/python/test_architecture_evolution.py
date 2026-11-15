"""Tests for AST-anchored compact attempt evolution."""

from __future__ import annotations

import pytest
from ultimate_coders.architecture.evolution import (
    EVOLUTION_ENABLED_ENV,
    MAX_STORED_CHAINS,
    AttemptEvolutionStore,
    EvolutionChain,
    EvolutionChange,
    anchor_symbol,
    compact_evolution,
    evolution_enabled,
)

SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "class Outer:\n"
    "    def method(self):\n"
    "        value = 1\n"
    "        return value\n"
    "\n"
    "\n"
    "async def fetch():\n"
    "    return 2\n"
)

DIFF_OLD_TO_NEW = "@@ -1,1 +1,1 @@\n-old\n+new\n"
DIFF_NEW_TO_OLD = "@@ -1,1 +1,1 @@\n-new\n+old\n"
DIFF_ADD_ONLY = "@@ -1,1 +1,3 @@\n keep\n+first\n+second\n"
DIFF_REMOVE_ONLY = "@@ -5,2 +5,0 @@\n-gone one\n-gone two\n"
DIFF_MIXED = "@@ -1,1 +1,1 @@\n-removed a\n-removed b\n+added a\n"
DIFF_ADD_TEMP = "@@ -1,1 +1,2 @@\n keep\n+temp = 1\n"
DIFF_REMOVE_TEMP = "@@ -1,2 +1,1 @@\n keep\n-temp = 1\n"
DIFF_DROP_GUARD = "@@ -3,1 +3,0 @@\n-keep = 2\n"
DIFF_RESTORE_GUARD = "@@ -3,0 +4,1 @@\n+keep = 2\n"


def _round(diff: str, path: str = "pkg/mod.py") -> dict:
    return {"patches": [{"path": path, "diff": diff}]}


def test_anchor_symbol_finds_enclosing_definitions(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mod.py").write_text(SOURCE, encoding="utf-8")
    assert anchor_symbol("mod.py", 6) == "mod.py::Outer::method"
    assert anchor_symbol("mod.py", 7) == "mod.py::Outer::method"
    assert anchor_symbol("mod.py", 4) == "mod.py::Outer"
    assert anchor_symbol("mod.py", 11) == "mod.py::fetch"
    assert anchor_symbol("mod.py", 1) == "mod.py::1"


def test_anchor_symbol_falls_back_without_raising(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "broken.py").write_text("def (\n", encoding="utf-8")
    assert anchor_symbol("broken.py", 3) == "broken.py::3"
    assert anchor_symbol("missing.py", 2) == "missing.py::2"
    assert anchor_symbol("notes.txt", 5) == "notes.txt::5"
    assert anchor_symbol("mod.py", 0) == "mod.py::0"
    assert anchor_symbol("", 4) == "::4"


def test_compact_evolution_pairs_removal_with_addition() -> None:
    chain = compact_evolution([_round(DIFF_OLD_TO_NEW)])
    assert chain.rounds == 1
    assert len(chain.changes) == 1
    change = chain.changes[0]
    assert (change.kind, change.old, change.new) == ("modified", "old", "new")
    assert change.symbol == "pkg/mod.py::1"
    assert change.round_index == 0
    assert change.reverted is False


def test_compact_evolution_keeps_leftover_additions_and_removals() -> None:
    added = compact_evolution([_round(DIFF_ADD_ONLY)])
    assert [change.kind for change in added.changes] == ["added", "added"]
    assert [change.new for change in added.changes] == ["first", "second"]

    removed = compact_evolution([_round(DIFF_REMOVE_ONLY)])
    assert [change.kind for change in removed.changes] == ["removed", "removed"]
    assert [change.old for change in removed.changes] == ["gone one", "gone two"]

    mixed = compact_evolution([_round(DIFF_MIXED)])
    assert [change.kind for change in mixed.changes] == ["modified", "removed"]
    assert mixed.changes[0].old == "removed a"
    assert mixed.changes[0].new == "added a"
    assert mixed.changes[1].old == "removed b"


def test_compact_evolution_marks_reverted_modification() -> None:
    chain = compact_evolution(
        [_round(DIFF_OLD_TO_NEW), _round(DIFF_NEW_TO_OLD)]
    )
    assert chain.rounds == 2
    assert len(chain.changes) == 2
    first, second = chain.changes
    assert first.reverted is True
    assert second.reverted is False
    assert chain.reverted_rounds == (1,)
    lines = chain.render().splitlines()
    assert lines[0].startswith("← reverted v2 ")
    assert "~ old -> new" in lines[0]
    assert lines[1] == "~ new -> old"


def test_compact_evolution_marks_reverted_addition() -> None:
    chain = compact_evolution([_round(DIFF_ADD_TEMP), _round(DIFF_REMOVE_TEMP)])
    added = chain.changes[0]
    assert added.kind == "added"
    assert added.reverted is True
    assert chain.reverted_rounds == (1,)
    assert chain.render().splitlines()[0] == "← reverted v2 + temp = 1"


def test_compact_evolution_marks_reverted_removal() -> None:
    chain = compact_evolution(
        [_round(DIFF_DROP_GUARD), _round(DIFF_RESTORE_GUARD)]
    )
    removed = chain.changes[0]
    assert removed.kind == "removed"
    assert removed.reverted is True
    assert chain.reverted_rounds == (1,)


def test_compact_evolution_reversion_is_path_scoped() -> None:
    chain = compact_evolution(
        [_round(DIFF_OLD_TO_NEW, "pkg/a.py"), _round(DIFF_NEW_TO_OLD, "pkg/b.py")]
    )
    assert chain.reverted_rounds == ()
    assert all(change.reverted is False for change in chain.changes)


def test_compact_evolution_orders_changes_deterministically() -> None:
    chain = compact_evolution(
        [
            {
                "patches": [
                    {"path": "zeta.py", "diff": DIFF_OLD_TO_NEW},
                    {"path": "alpha.py", "diff": DIFF_OLD_TO_NEW},
                ]
            },
            _round(DIFF_OLD_TO_NEW, "beta.py"),
        ]
    )
    assert [(change.round_index, change.path) for change in chain.changes] == [
        (0, "alpha.py"),
        (0, "zeta.py"),
        (1, "beta.py"),
    ]


def test_compact_evolution_skips_invalid_rounds_and_patches() -> None:
    chain = compact_evolution(
        [
            {"attempt": 1, "patches": []},
            "not a round",
            {
                "patches": [
                    {"path": "", "diff": DIFF_OLD_TO_NEW},
                    {"path": "pkg/mod.py", "diff": ""},
                ]
            },
            _round(DIFF_OLD_TO_NEW),
        ],
        task_id="task-1",
        subtask_id="sub-1",
    )
    assert chain.task_id == "task-1"
    assert chain.subtask_id == "sub-1"
    assert chain.rounds == 3
    assert len(chain.changes) == 1
    assert chain.changes[0].round_index == 2


def test_diff_header_shaped_lines_are_kept_as_content() -> None:
    diff = "@@ -1,2 +1,2 @@\n----\n+++\n title: demo\n"
    chain = compact_evolution([{"patches": [{"path": "docs/conf.yaml", "diff": diff}]}])
    assert [(change.kind, change.old, change.new) for change in chain.changes] == [
        ("modified", "---", "++"),
    ]


def test_evolution_change_coerces_fields() -> None:
    change = EvolutionChange(
        path="pkg/mod.py",
        symbol="pkg/mod.py::f",
        kind=" Added ",
        old=None,
        new="value",
        round_index=-3,
    )
    assert change.kind == "added"
    assert change.old == ""
    assert change.new == "value"
    assert change.round_index == 0
    assert EvolutionChange("p", "s", "weird", "", "", 2).kind == "modified"


def test_chain_render_formats_and_clips_values() -> None:
    long_new = "x" * 120
    chain = EvolutionChain(
        task_id="t",
        subtask_id="s",
        changes=(
            EvolutionChange("p.py", "p.py::f", "added", "", long_new, 0),
            EvolutionChange("p.py", "p.py::f", "removed", "gone", "", 0),
            EvolutionChange("p.py", "p.py::f", "modified", "a\nb", "c", 0),
        ),
        rounds=1,
    )
    lines = chain.render().splitlines()
    assert len(lines) == 3
    assert lines[0] == "+ " + "x" * 80
    assert lines[1] == "- gone"
    assert lines[2] == "~ a b -> c"
    assert chain.file_count == 1


def test_chain_render_caps_changes_and_empty_chain() -> None:
    changes = tuple(
        EvolutionChange("p.py", "p.py::f", "added", "", f"v{index}", 0)
        for index in range(3)
    )
    chain = EvolutionChain(changes=changes, rounds=1)
    assert chain.render(max_changes=2).splitlines() == ["+ v0", "+ v1"]
    assert chain.render(max_changes=0) == ""
    empty = EvolutionChain()
    assert empty.render() == ""
    assert empty.file_count == 0


def test_evolution_store_is_bounded_and_renders() -> None:
    store = AttemptEvolutionStore(max_chains=2)
    first = EvolutionChain(
        task_id="t1",
        subtask_id="s1",
        changes=(EvolutionChange("p.py", "p.py::f", "added", "", "v1", 0),),
        rounds=1,
    )
    second = EvolutionChain(task_id="t2", subtask_id="s2", rounds=1)
    third = EvolutionChain(task_id="t3", subtask_id="s3", rounds=1)
    store.record(first)
    store.record(second)
    assert store.get("t1", "s1") is first
    assert store.render("t1", "s1") == "+ v1"
    store.record(third)
    assert len(store) == 2
    assert store.get("t1", "s1") is None
    assert store.get("t3", "s3") is third
    replacement = EvolutionChain(task_id="t3", subtask_id="s3", rounds=2)
    store.record(replacement)
    assert len(store) == 2
    assert store.get("t3", "s3") is replacement
    assert store.get("missing", "missing") is None
    assert store.render("missing", "missing") == ""


def test_evolution_store_rejects_non_chain() -> None:
    store = AttemptEvolutionStore()
    with pytest.raises(TypeError):
        store.record({"task_id": "t"})


def test_evolution_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert EVOLUTION_ENABLED_ENV == "UC_AST_EVOLUTION"
    assert MAX_STORED_CHAINS == 200
    monkeypatch.delenv("UC_AST_EVOLUTION", raising=False)
    assert evolution_enabled() is False
    monkeypatch.setenv("UC_AST_EVOLUTION", "on")
    assert evolution_enabled() is True
    monkeypatch.setenv("UC_AST_EVOLUTION", "0")
    assert evolution_enabled() is False
