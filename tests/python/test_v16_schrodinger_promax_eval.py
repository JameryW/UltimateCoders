"""Tests for SchrodingerRepo transforms and ProMax refactor transactions."""

from __future__ import annotations

import pytest
from ultimate_coders.acceptance.schrodinger_promax_eval import (
    SCHRODINGER_PROMAX_ENV,
    AtomicRefactorTransaction,
    RefactorEdit,
    RefactorError,
    RepoFile,
    RepositorySnapshot,
    SchrodingerConfig,
    SchrodingerTransformer,
    TransformationLevel,
    schrodinger_promax_enabled,
)


def snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        files=(
            RepoFile(
                "src/cache.py",
                "class Cache:\n    pass\n\ndef get(key):\n    if (key):\n        return Cache()\n",
                ("Cache", "get"),
            ),
            RepoFile(
                "src/service.py",
                "from src.cache import Cache\n\ndef run():\n    return Cache()\n",
                ("Cache", "run"),
            ),
        ),
        issue="Fix src/cache.py where Cache drops keys.",
    )


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SCHRODINGER_PROMAX_ENV, raising=False)
    assert schrodinger_promax_enabled() is False
    monkeypatch.setenv(SCHRODINGER_PROMAX_ENV, "true")
    assert schrodinger_promax_enabled() is True


def test_default_transform_applies_all_four_levels() -> None:
    result = SchrodingerTransformer().transform(snapshot())
    assert result.levels == (
        TransformationLevel.ISSUE_REWRITE,
        TransformationLevel.NAMESPACE_REMAP,
        TransformationLevel.LAYOUT_SHUFFLE,
        TransformationLevel.EQUIVALENT_REWRITE,
    )
    assert result.issue_original.startswith("Fix src/cache.py")
    assert result.repository.issue.startswith("A defect is reported")
    assert "src/cache.py" not in result.repository.issue
    assert "Cache" not in result.repository.issue
    assert result.namespace_map["Cache"].startswith("obs_")


def test_namespace_remap_changes_content_and_symbols() -> None:
    result = SchrodingerTransformer().transform(
        snapshot(),
        SchrodingerConfig(levels=(TransformationLevel.NAMESPACE_REMAP,)),
    )
    new_cache = result.namespace_map["Cache"]
    assert new_cache != "Cache"
    for item in result.repository.files:
        assert "Cache" not in item.content
        assert new_cache in item.content
        assert all(not symbol == "Cache" for symbol in item.symbols)


def test_layout_shuffle_changes_order_but_preserves_files() -> None:
    base = snapshot()
    result = SchrodingerTransformer().transform(
        base,
        SchrodingerConfig(levels=(TransformationLevel.LAYOUT_SHUFFLE,)),
    )
    assert set(result.repository.paths) == set(base.paths)
    assert len(result.repository.files) == len(base.files)


def test_equivalent_rewrite_removes_redundant_parentheses() -> None:
    result = SchrodingerTransformer().transform(
        snapshot(),
        SchrodingerConfig(levels=(TransformationLevel.EQUIVALENT_REWRITE,)),
    )
    assert "if key:" in result.repository.file("src/cache.py").content
    assert "if (key):" not in result.repository.file("src/cache.py").content


def test_issue_rewrite_redacts_paths_and_symbols() -> None:
    result = SchrodingerTransformer().transform(
        snapshot(),
        SchrodingerConfig(levels=(TransformationLevel.ISSUE_REWRITE,)),
    )
    issue = result.repository.issue
    assert "<module>" in issue
    assert "<symbol>" in issue
    assert "src/cache.py" not in issue
    assert result.repository.file("src/cache.py").content == snapshot().file(
        "src/cache.py"
    ).content


def test_transform_is_deterministic() -> None:
    first = SchrodingerTransformer().transform(snapshot())
    second = SchrodingerTransformer().transform(snapshot())
    assert first.repository.to_dict() == second.repository.to_dict()
    assert first.namespace_map == second.namespace_map


def test_custom_salt_changes_the_map() -> None:
    first = SchrodingerTransformer().transform(snapshot())
    second = SchrodingerTransformer().transform(
        snapshot(),
        SchrodingerConfig(salt="another-salt"),
    )
    assert first.namespace_map != second.namespace_map


def test_empty_config_levels_is_rejected() -> None:
    with pytest.raises(RefactorError):
        SchrodingerConfig(levels=())
    with pytest.raises(RefactorError):
        SchrodingerConfig(salt="")


def test_snapshot_validation() -> None:
    with pytest.raises(RefactorError):
        RepositorySnapshot(files=(), issue="issue")
    with pytest.raises(RefactorError):
        RepositorySnapshot(files=(RepoFile("a.py", "x"),), issue="")
    with pytest.raises(RefactorError):
        RepositorySnapshot(
            files=(RepoFile("a.py", "x"), RepoFile("a.py", "y")),
            issue="issue",
        )
    with pytest.raises(RefactorError):
        RepoFile("", "x")
    with pytest.raises(RefactorError):
        RepoFile("../a.py", "x")
    with pytest.raises(RefactorError):
        RepoFile("a.py", "")


def test_refactor_applies_across_files_and_updates_symbols() -> None:
    transaction = AtomicRefactorTransaction(
        snapshot(),
        (
            RefactorEdit("Cache", "MemoryCache", ("src/cache.py", "src/service.py")),
            RefactorEdit("run", "execute", ("src/service.py",)),
        ),
    )
    applied = transaction.apply()
    assert transaction.applied is True
    assert "MemoryCache" in applied.file("src/cache.py").content
    assert "MemoryCache" in applied.file("src/service.py").content
    assert "execute" in applied.file("src/service.py").content
    assert "MemoryCache" in applied.file("src/service.py").symbols


def test_refactor_chains_in_dependency_order() -> None:
    base = RepositorySnapshot(
        files=(RepoFile("src/a.py", "A B C\n", ("A", "B", "C")),),
        issue="rename",
    )
    transaction = AtomicRefactorTransaction(
        base,
        (
            RefactorEdit("A", "B", ("src/a.py",)),
            RefactorEdit("B", "C", ("src/a.py",)),
        ),
    )
    applied = transaction.apply()
    assert applied.file("src/a.py").content == "B C C\n"


def test_refactor_rollback_restores_the_snapshot() -> None:
    base = snapshot()
    transaction = AtomicRefactorTransaction(
        base,
        (RefactorEdit("Cache", "MemoryCache", ("src/cache.py",)),),
    )
    transaction.apply()
    restored = transaction.rollback()
    assert restored is base
    assert transaction.applied is False


def test_refactor_rejects_cycles_and_missing_symbols() -> None:
    base = snapshot()
    with pytest.raises(RefactorError):
        AtomicRefactorTransaction(
            base,
            (
                RefactorEdit("Cache", "get", ("src/cache.py", "src/service.py")),
                RefactorEdit("get", "Cache", ("src/cache.py",)),
            ),
        )
    with pytest.raises(RefactorError):
        AtomicRefactorTransaction(
            base,
            (RefactorEdit("Missing", "Other", ("src/cache.py",)),),
        )
    with pytest.raises(RefactorError):
        AtomicRefactorTransaction(
            base,
            (RefactorEdit("Cache", "Other", ("src/missing.py",)),),
        )


def test_refactor_rejects_duplicate_and_empty_edits() -> None:
    base = snapshot()
    with pytest.raises(RefactorError):
        AtomicRefactorTransaction(base, ())
    with pytest.raises(RefactorError):
        AtomicRefactorTransaction(
            base,
            (
                RefactorEdit("Cache", "A", ("src/cache.py",)),
                RefactorEdit("Cache", "B", ("src/cache.py",)),
            ),
        )
    with pytest.raises(RefactorError):
        RefactorEdit("Cache", "Cache", ("src/cache.py",))
    with pytest.raises(RefactorError):
        RefactorEdit("", "Other", ("src/cache.py",))
    with pytest.raises(RefactorError):
        RefactorEdit("Cache", "Other", ())


def test_transform_result_is_serializable() -> None:
    result = SchrodingerTransformer().transform(snapshot())
    payload = result.to_dict()
    assert payload["levels"][0] == "issue_rewrite"
    assert payload["issue_original"].startswith("Fix")
    assert "files" in payload["repository"]
