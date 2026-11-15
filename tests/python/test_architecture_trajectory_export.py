"""Tests for the Dressage-compatible TITO JSONL trajectory exporter."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from ultimate_coders.architecture.trajectory_export import (
    TITOToken,
    TrajectoryExporter,
    TrajectoryRecord,
    TrajectoryTurn,
    pair_outcomes,
    trajectory_export_enabled,
)


def make_turn(role: str = "assistant", length: int = 3, mask: int = 1) -> TrajectoryTurn:
    return TrajectoryTurn(
        role=role,
        token_ids=tuple(range(100, 100 + length)),
        logprobs=tuple(-0.25 - index for index in range(length)),
        loss_mask=tuple(mask for _ in range(length)),
    )


def make_record(
    task_id: str = "task-1",
    subtask_id: str = "sub-1",
    attempt: int = 0,
    outcome: str = "failed",
    turns: tuple[TrajectoryTurn, ...] | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
    metadata: dict | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        task_id=task_id,
        subtask_id=subtask_id,
        attempt=attempt,
        outcome=outcome,
        turns=turns if turns is not None else (make_turn(),),
        created_at=created_at,
        metadata=metadata if metadata is not None else {},
    )


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_tito_token_round_trip_and_mask_validation() -> None:
    token = TITOToken(token_id=7, logprob=-0.5, loss_mask=0)

    assert token.to_dict() == {"token_id": 7, "logprob": -0.5, "loss_mask": 0}
    with pytest.raises(ValueError):
        TITOToken(token_id=7, logprob=-0.5, loss_mask=2)


def test_turn_requires_aligned_sequences() -> None:
    turn = make_turn(length=2)

    assert turn.to_dict() == {
        "role": "assistant",
        "token_ids": [100, 101],
        "logprobs": [-0.25, -1.25],
        "loss_mask": [1, 1],
    }
    with pytest.raises(ValueError):
        TrajectoryTurn(role="user", token_ids=(1, 2), logprobs=(0.0,), loss_mask=(1, 1))
    with pytest.raises(ValueError):
        TrajectoryTurn(role="", token_ids=(1,), logprobs=(0.0,), loss_mask=(1,))
    with pytest.raises(ValueError):
        TrajectoryTurn(role="user", token_ids=(1,), logprobs=(0.0,), loss_mask=(3,))


def test_record_round_trip_and_validation() -> None:
    record = make_record(metadata={"attempt_note": "ok"})

    payload = record.to_dict()

    assert payload["task_id"] == "task-1"
    assert payload["outcome"] == "failed"
    assert payload["turns"][0]["token_ids"] == [100, 101, 102]
    assert payload["metadata"] == {"attempt_note": "ok"}
    with pytest.raises(ValueError):
        make_record(outcome="errored")
    with pytest.raises(ValueError):
        make_record(attempt=-1)
    with pytest.raises(TypeError):
        TrajectoryRecord(
            task_id="task-1",
            subtask_id="sub-1",
            attempt=0,
            outcome="failed",
            turns=("not-a-turn",),  # type: ignore[arg-type]
            created_at="2026-01-01T00:00:00+00:00",
        )


def test_disabled_exporter_is_a_no_op(tmp_path) -> None:
    export_dir = tmp_path / "trajectories"
    exporter = TrajectoryExporter(export_dir, enabled=False)

    assert exporter.enabled is False
    assert exporter.write(make_record()) is None
    assert not export_dir.exists()


def test_exporter_defaults_to_disabled_without_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("UC_TRAJECTORY_EXPORT", raising=False)
    monkeypatch.delenv("UC_TRAJECTORY_EXPORT_DIR", raising=False)

    exporter = TrajectoryExporter(tmp_path)

    assert exporter.enabled is False
    assert exporter.write(make_record()) is None


def test_write_appends_jsonl_lines(tmp_path) -> None:
    exporter = TrajectoryExporter(tmp_path, enabled=True)
    first = make_record(metadata={"note": "héllo ✓"})

    path = exporter.write(first)
    exporter.write(make_record(task_id="task-2", outcome="succeeded"))

    assert path == tmp_path / "trajectories.jsonl"
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert "héllo" in raw
    assert "h\\u00e9" not in raw
    rows = read_lines(path)
    assert [row["task_id"] for row in rows] == ["task-1", "task-2"]
    assert rows[0]["metadata"] == {"note": "héllo ✓"}
    assert rows[0]["outcome"] == "failed"


def test_rotation_when_byte_budget_is_exceeded(tmp_path) -> None:
    exporter = TrajectoryExporter(tmp_path, enabled=True, max_file_bytes=512)
    first = make_record(task_id="first", metadata={"pad": "x" * 400})
    second = make_record(task_id="second", metadata={"pad": "y" * 400})

    active = exporter.write(first)
    exporter.write(second)

    rotated = tmp_path / "trajectories-1.jsonl"
    assert rotated.exists()
    assert active == tmp_path / "trajectories.jsonl"
    assert [row["task_id"] for row in read_lines(rotated)] == ["first"]
    assert [row["task_id"] for row in read_lines(active)] == ["second"]


def test_rotation_numbers_past_existing_files(tmp_path) -> None:
    (tmp_path / "trajectories-1.jsonl").write_text("old-one\n", encoding="utf-8")
    (tmp_path / "trajectories-2.jsonl").write_text("old-two\n", encoding="utf-8")
    exporter = TrajectoryExporter(tmp_path, enabled=True, max_file_bytes=200)

    exporter.write(make_record(task_id="first", metadata={"pad": "x" * 150}))
    exporter.write(make_record(task_id="second", metadata={"pad": "y" * 150}))

    archived = tmp_path / "trajectories-3.jsonl"
    assert archived.exists()
    assert read_lines(archived)[0]["task_id"] == "first"
    assert (tmp_path / "trajectories-1.jsonl").read_text(encoding="utf-8") == "old-one\n"
    assert (tmp_path / "trajectories-2.jsonl").read_text(encoding="utf-8") == "old-two\n"


def test_max_file_bytes_must_be_positive(tmp_path) -> None:
    with pytest.raises(ValueError):
        TrajectoryExporter(tmp_path, enabled=True, max_file_bytes=0)


def test_created_at_defaults_to_export_time(tmp_path) -> None:
    fixed = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    exporter = TrajectoryExporter(tmp_path, enabled=True, now=lambda: fixed)

    path = exporter.write(make_record(created_at=""))

    assert read_lines(path)[0]["created_at"] == "2026-02-03T04:05:06+00:00"


def test_redacts_secret_patterns_in_strings(tmp_path) -> None:
    exporter = TrajectoryExporter(tmp_path, enabled=True)
    record = make_record(
        metadata={
            "note": "used sk-abcdefgh1234 and Bearer abc.def.ghi",
            "endpoint": "https://api.example.com?key=sk-zzzzzzzz9999",
        }
    )

    path = exporter.write(record)
    raw = path.read_text(encoding="utf-8")
    payload = read_lines(path)[0]

    assert "sk-abcdefgh1234" not in raw
    assert "sk-zzzzzzzz9999" not in raw
    assert "Bearer abc.def.ghi" not in raw
    assert payload["metadata"]["note"] == "used [redacted] and [redacted]"


def test_redacts_sensitive_metadata_keys_recursively(tmp_path) -> None:
    exporter = TrajectoryExporter(tmp_path, enabled=True)
    record = make_record(
        metadata={
            "api_key": "sk-live-value",
            "nested": {"GITHUB_TOKEN": "ghp_live", "Authorization": "Bearer abc"},
            "safe": "visible",
        }
    )

    path = exporter.write(record)
    raw = path.read_text(encoding="utf-8")
    metadata = read_lines(path)[0]["metadata"]

    assert metadata["api_key"] == "[redacted]"
    assert metadata["nested"]["GITHUB_TOKEN"] == "[redacted]"
    assert metadata["nested"]["Authorization"] == "[redacted]"
    assert metadata["safe"] == "visible"
    assert "sk-live-value" not in raw
    assert "ghp_live" not in raw


def test_pair_outcomes_pairs_failed_with_rescued() -> None:
    records = [
        make_record(attempt=2, outcome="failed"),
        make_record(attempt=1, outcome="rescued"),
        make_record(attempt=1, outcome="failed"),
        make_record(attempt=2, outcome="rescued"),
    ]

    pairs = pair_outcomes(records)

    assert len(pairs) == 2
    assert [(failed.attempt, rescued.attempt) for failed, rescued in pairs] == [(1, 1), (2, 2)]
    assert all(failed.task_id == rescued.task_id == "task-1" for failed, rescued in pairs)


def test_pair_outcomes_is_deterministic_and_ignores_succeeded() -> None:
    records = [
        make_record(task_id="task-b", outcome="rescued"),
        make_record(task_id="task-b", outcome="failed"),
        make_record(task_id="task-a", outcome="succeeded"),
        make_record(task_id="task-a", outcome="rescued"),
        make_record(task_id="task-a", outcome="failed"),
    ]

    pairs = pair_outcomes(records)

    assert [failed.task_id for failed, _ in pairs] == ["task-a", "task-b"]
    assert pair_outcomes(sorted(records, key=lambda record: record.outcome)) == pairs


def test_pair_outcomes_ignores_unpaired_extra_records() -> None:
    records = [
        make_record(outcome="failed"),
        make_record(attempt=1, outcome="failed"),
        make_record(attempt=2, outcome="failed"),
        make_record(outcome="rescued"),
    ]

    pairs = pair_outcomes(records)

    assert len(pairs) == 1
    assert pairs[0][0].attempt == 0
    assert pairs[0][1].attempt == 0


def test_fsync_option_still_writes_parseable_jsonl(tmp_path) -> None:
    exporter = TrajectoryExporter(tmp_path, enabled=True, fsync=True)

    path = exporter.write(make_record())

    assert read_lines(path)[0]["task_id"] == "task-1"


@pytest.mark.parametrize("value", ["1", "true", " TRUE ", "on", "yes"])
def test_trajectory_export_enabled_truthy(monkeypatch, tmp_path, value: str) -> None:
    monkeypatch.setenv("UC_TRAJECTORY_EXPORT", value)
    monkeypatch.setenv("UC_TRAJECTORY_EXPORT_DIR", str(tmp_path))

    assert trajectory_export_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "", "off"])
def test_trajectory_export_disabled_by_flag(monkeypatch, tmp_path, value: str) -> None:
    monkeypatch.setenv("UC_TRAJECTORY_EXPORT", value)
    monkeypatch.setenv("UC_TRAJECTORY_EXPORT_DIR", str(tmp_path))

    assert trajectory_export_enabled() is False


def test_trajectory_export_requires_directory(monkeypatch) -> None:
    monkeypatch.setenv("UC_TRAJECTORY_EXPORT", "1")
    monkeypatch.delenv("UC_TRAJECTORY_EXPORT_DIR", raising=False)
    assert trajectory_export_enabled() is False

    monkeypatch.setenv("UC_TRAJECTORY_EXPORT_DIR", "   ")
    assert trajectory_export_enabled() is False


def test_explicit_enabled_overrides_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("UC_TRAJECTORY_EXPORT", "1")
    monkeypatch.setenv("UC_TRAJECTORY_EXPORT_DIR", str(tmp_path / "unused"))
    exporter = TrajectoryExporter(tmp_path / "used", enabled=False)

    assert exporter.write(make_record()) is None
    assert not (tmp_path / "unused").exists()
    assert not (tmp_path / "used").exists()
