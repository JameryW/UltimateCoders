"""Tests for the V18 ChainSWE chain editor (#67)."""

from __future__ import annotations

import math

import pytest
from ultimate_coders.agent.chain_editor import (
    BugScope,
    ChainEditor,
    ChainPatch,
    Edit,
    chain_editor_enabled,
)


def scope() -> BugScope:
    return BugScope(
        bug_id="bug-1",
        paths=("src/cache.py",),
        symbols=("Cache.get", "Cache.set"),
        required_symbols=("Cache.get",),
    )


def edit(path: str = "src/cache.py", symbol: str = "Cache.get") -> Edit:
    return Edit(path=path, symbol=symbol, added_lines=2, removed_lines=1)


def test_flag_defaults_off_and_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not chain_editor_enabled()
    monkeypatch.setenv("UC_CHAIN_EDITOR", "yes")
    assert chain_editor_enabled()


def test_in_scope_edit_is_kept() -> None:
    result = ChainEditor().sanitize(ChainPatch("bug-1", (edit(),)), scope())
    assert result.kept == (edit(),)
    assert result.stripped == ()
    assert result.clean
    assert math.isclose(result.survival, 1.0)


def test_out_of_scope_edit_is_stripped() -> None:
    patch = ChainPatch(
        "bug-1",
        (edit(), Edit("src/unrelated.py", "Helper.run", 50, 0)),
    )
    result = ChainEditor().sanitize(patch, scope())
    assert len(result.kept) == 1
    assert len(result.stripped) == 1
    assert result.overshoot_lines == 50
    assert math.isclose(result.overshoot_ratio, 50 / 53)


def test_missing_required_symbol_is_undershoot() -> None:
    result = ChainEditor().sanitize(
        ChainPatch("bug-1", (edit(symbol="Cache.set"),)), scope()
    )
    assert result.required_missing == ("Cache.get",)
    assert result.undershoot
    assert not result.clean


def test_bug_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match"):
        ChainEditor().sanitize(ChainPatch("other", (edit(),)), scope())


def test_empty_patch_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one edit"):
        ChainEditor().sanitize(ChainPatch("bug-1", ()), scope())


def test_empty_scope_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="at least one path or symbol"):
        BugScope("bug-1")


def test_chain_survival_and_invalid_rates() -> None:
    editor = ChainEditor(lambda_=1.0)
    assert math.isclose(
        editor.chain_survival((0.9, 0.8), overshoot_lines=2),
        0.72 * math.exp(-2.0),
    )
    with pytest.raises(ValueError, match="pass rates"):
        editor.chain_survival((1.2,))
    with pytest.raises(ValueError, match="at least one"):
        editor.chain_survival(())


def test_summary_reports_symbols_and_overshoot() -> None:
    result = ChainEditor().sanitize(
        ChainPatch(
            "bug-1",
            (edit(), Edit("src/unrelated.py", "Helper.run", 4, 0)),
        ),
        scope(),
    )
    summary = ChainEditor().summarize(result)
    assert summary.bug_id == "bug-1"
    assert summary.touched_symbols == ("Cache.get",)
    assert summary.kept_lines == 3
    assert summary.stripped_lines == 4
    assert "stripped 4 overshoot lines" in summary.text


def test_negative_lambda_is_rejected() -> None:
    with pytest.raises(ValueError, match="lambda"):
        ChainEditor(lambda_=-1.0)
