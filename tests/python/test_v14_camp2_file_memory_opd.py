"""Tests for the v14 CAMP-II native file memory and OPD supervision (default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.agent.camp2_file_memory_opd import (
    CAMP2_FILE_MEMORY_OPD_ENV,
    CONTINUATION_PATH,
    NOTES_PATH,
    Camp2OpdDistiller,
    FileMemoryError,
    FileMemoryPrior,
    FileMemorySession,
    FileMemoryWrite,
    MemoryFile,
    MemoryWriteSample,
    TokenLogprobError,
    camp2_file_memory_opd_enabled,
    validate_memory_path,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CAMP2_FILE_MEMORY_OPD_ENV, raising=False)
    assert camp2_file_memory_opd_enabled() is False
    monkeypatch.setenv(CAMP2_FILE_MEMORY_OPD_ENV, "1")
    assert camp2_file_memory_opd_enabled() is True


def test_native_memory_paths_are_strictly_bounded() -> None:
    assert validate_memory_path(NOTES_PATH) == NOTES_PATH
    assert validate_memory_path("./" + CONTINUATION_PATH) == CONTINUATION_PATH
    for bad in ("src/main.py", "../notes.md", "/tmp/notes.md", ".uc_memory/other.md"):
        with pytest.raises(FileMemoryError):
            validate_memory_path(bad)


def test_render_write_carries_step_evidence_and_next_action() -> None:
    write = FileMemoryWrite(
        path=NOTES_PATH,
        content="The cache invalidation lives in Store.purge().",
        step=4,
        evidence=("src/store.py:88",),
        next_action="read src/store.py and run pytest",
    )
    rendered = FileMemoryPrior().render_write(write)
    assert "## Step 4" in rendered
    assert "Store.purge()" in rendered
    assert "Evidence:" in rendered
    assert "src/store.py:88" in rendered
    assert "Next: read src/store.py" in rendered


def test_continuation_requires_a_next_action() -> None:
    with pytest.raises(FileMemoryError):
        FileMemoryWrite(path=CONTINUATION_PATH, content="state", step=1)


def test_session_writes_both_native_files_and_renders_context() -> None:
    session = FileMemorySession()
    session.write(FileMemoryWrite(NOTES_PATH, "Found the failing invariant.", step=1))
    session.write(
        FileMemoryWrite(
            CONTINUATION_PATH,
            "Patch staged, regression pending.",
            step=2,
            next_action="run the regression suite",
        )
    )
    files = session.files()
    assert "failing invariant" in files[NOTES_PATH]
    assert "regression pending" in files[CONTINUATION_PATH]
    context = session.render_context()
    assert context.index(CONTINUATION_PATH) < context.index(NOTES_PATH)
    assert "run the regression suite" in context


def test_append_keeps_the_most_recent_tail() -> None:
    prior = FileMemoryPrior(max_chars=180)
    current = MemoryFile(NOTES_PATH, "old note " * 80)
    updated = prior.append(
        current,
        FileMemoryWrite(NOTES_PATH, "fresh evidence", step=3, next_action="continue"),
    )
    assert len(updated.content) <= 180
    assert updated.content.startswith("… (truncated)")
    assert "fresh evidence" in updated.content


def test_token_sample_lengths_must_align() -> None:
    with pytest.raises(TokenLogprobError):
        MemoryWriteSample(
            step=1,
            token_ids=(1, 2),
            teacher_logprobs=(-0.1,),
            student_logprobs=(-0.2, -0.3),
            write_mask=(True, False),
        )


def test_memory_write_tokens_receive_extra_advantage() -> None:
    sample = MemoryWriteSample(
        step=1,
        token_ids=(10, 11),
        teacher_logprobs=(-0.1, -0.2),
        student_logprobs=(-0.3, -0.4),
        write_mask=(True, False),
    )
    advantage = Camp2OpdDistiller(memory_write_weight=2.0).score(sample)
    assert advantage.write_tokens == 1
    assert advantage.per_token == pytest.approx((0.4, 0.2))
    assert advantage.write_mean == pytest.approx(0.2)
    assert advantage.weighted_mean == pytest.approx(0.3)


def test_distillation_accepts_positive_write_signal() -> None:
    sample = MemoryWriteSample(
        step=1,
        token_ids=(1, 2),
        teacher_logprobs=(-0.1, -0.2),
        student_logprobs=(-0.3, -0.4),
        write_mask=(True, False),
    )
    report = Camp2OpdDistiller().distill([sample])
    assert report.samples == 1
    assert report.write_tokens == 1
    assert report.mean_delta == pytest.approx(0.2)
    assert report.mean_write_advantage == pytest.approx(0.2)
    assert report.weighted_advantage == pytest.approx(0.3)
    assert report.accepted is True


def test_distillation_rejects_negative_write_signal() -> None:
    sample = MemoryWriteSample(
        step=1,
        token_ids=(1,),
        teacher_logprobs=(-0.5,),
        student_logprobs=(-0.1,),
        write_mask=(True,),
    )
    report = Camp2OpdDistiller().distill([sample])
    assert report.accepted is False
    assert report.weighted_advantage < 0
