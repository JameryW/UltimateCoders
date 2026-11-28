from __future__ import annotations

import pytest
from ultimate_coders.agent.evogroup_harness import (
    EVOGROUP_HARNESS_ENV,
    EvoGroupError,
    EvoGroupHarness,
    GroupTrace,
    HarnessMutation,
    ProgressiveDisclosureAnalyzer,
    SharedHarness,
    ToolSpec,
    TrajectorySelector,
    evogroup_harness_enabled,
)


def test_flag_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EVOGROUP_HARNESS_ENV, "yes")
    assert evogroup_harness_enabled()
    monkeypatch.setenv(EVOGROUP_HARNESS_ENV, "off")
    assert not evogroup_harness_enabled()


def test_selector_keeps_normal_traces_and_flags_low_confidence() -> None:
    selector = TrajectorySelector(low_confidence=0.4, high_confidence=0.95)
    selection = selector.select(
        [
            GroupTrace("normal", 0.7, True, ("search",)),
            GroupTrace("low", 0.2, True, ("search",)),
            GroupTrace("overconfident", 0.99, False, ("edit",)),
        ]
    )
    assert [trace.trace_id for trace in selection.kept] == ["normal"]
    assert [trace.trace_id for trace in selection.reflect] == ["low"]
    assert [trace.trace_id for trace in selection.rejected] == ["overconfident"]


def test_selector_rejects_inverted_band() -> None:
    with pytest.raises(EvoGroupError):
        TrajectorySelector(0.8, 0.4)


def test_progressive_disclosure_uses_bucket_count() -> None:
    analyzer = ProgressiveDisclosureAnalyzer()
    selection = TrajectorySelector().select([])
    report = analyzer.analyze(
        [
            GroupTrace("a", 0.7, True, ("search",)),
            GroupTrace("b", 0.7, False, ("edit",)),
            GroupTrace("c", 0.7, True, ("test",)),
        ],
        selection,
    )
    assert report.recommended_level == "bucketed"
    assert [bucket.phase for bucket in report.buckets] == ["edit", "search", "test"]


def test_shared_harness_applies_instructions_and_versions() -> None:
    harness = SharedHarness((ToolSpec("read"), ToolSpec("write")))
    harness.apply(HarnessMutation("instructions", "inspect first", ("trace",)))
    assert harness.version == 2
    assert harness.instructions == ("inspect first",)


def test_shared_harness_rejects_duplicate_tools() -> None:
    with pytest.raises(EvoGroupError):
        SharedHarness((ToolSpec("read"), ToolSpec("read")))


def test_evogroup_evolve_adds_reflection_instruction() -> None:
    group = EvoGroupHarness(tools=(ToolSpec("read"), ToolSpec("edit")))
    group.record(GroupTrace("low", 0.2, True, ("search",)))
    selection, report, harness = group.evolve()
    assert selection.kept_count == 0
    assert report.reflected == 1
    assert any("confidence" in item for item in harness.instructions)


def test_evogroup_summary_disclosure_adds_summary_rule() -> None:
    group = EvoGroupHarness(tools=(ToolSpec("read"), ToolSpec("edit")))
    for index in range(5):
        group.record(GroupTrace(f"t{index}", 0.7, True, (f"phase{index}",)))
    _, report, harness = group.evolve()
    assert report.recommended_level == "summary"
    assert any("Summarize" in item for item in harness.instructions)


def test_evogroup_reset_clears_traces_and_version() -> None:
    group = EvoGroupHarness(tools=(ToolSpec("read"),))
    group.record(GroupTrace("t", 0.7, True, ("search",)))
    group.evolve()
    group.reset()
    assert group.traces == ()
    assert group.harness.version == 1


def test_invalid_tool_and_trace_are_rejected() -> None:
    with pytest.raises(EvoGroupError):
        ToolSpec("")
    with pytest.raises(EvoGroupError):
        GroupTrace("", 0.5, True)
    with pytest.raises(EvoGroupError):
        GroupTrace("x", 1.5, True)
