from __future__ import annotations

import pytest
from ultimate_coders.agent.stateful_ptc import (
    STATEFUL_PTC_ENV,
    DeterministicPlan,
    ProgrammaticSession,
    ToolCall,
    TransientToolError,
    TypedValue,
    stateful_ptc_enabled,
)


def test_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(STATEFUL_PTC_ENV, raising=False)
    assert not stateful_ptc_enabled()
    monkeypatch.setenv(STATEFUL_PTC_ENV, "on")
    assert stateful_ptc_enabled()


def test_variables_are_typed_and_persistent() -> None:
    session = ProgrammaticSession()
    session.set("count", 3, "int")
    assert session.get("count", "int") == 3
    assert session.typed("count") == TypedValue("count", 3, "int")
    with pytest.raises(TypeError, match="not str"):
        session.get("count", "str")


def test_capacity_and_missing_variables_are_rejected() -> None:
    session = ProgrammaticSession(max_variables=1)
    session.set("a", 1)
    with pytest.raises(ValueError, match="capacity"):
        session.set("b", 2)
    with pytest.raises(KeyError):
        session.get("missing")


def test_transient_failure_is_retried_then_stored() -> None:
    attempts = 0

    def executor(tool: str, args: dict[str, object]) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientToolError("cold start")
        return "ok"

    session = ProgrammaticSession(max_attempts=2)
    record = session.call(ToolCall("search", output="result"), executor)
    assert record.ok
    assert record.attempts == 2
    assert session.get("result") == "ok"


def test_permanent_failure_is_not_retried() -> None:
    calls = 0

    def executor(tool: str, args: dict[str, object]) -> str:
        nonlocal calls
        calls += 1
        raise ValueError("bad argument")

    record = ProgrammaticSession(max_attempts=3).call(
        ToolCall("search", output="result"), executor
    )
    assert not record.ok
    assert calls == 1
    assert record.error == "bad argument"


def test_exhausted_transient_retries_fail_closed() -> None:
    def executor(tool: str, args: dict[str, object]) -> str:
        raise TransientToolError("still cold")

    record = ProgrammaticSession(max_attempts=2).call(
        ToolCall("search", output="result"), executor
    )
    assert not record.ok
    assert record.attempts == 2
    assert record.error == "still cold"


def test_plan_runs_steps_in_order_and_records_retries() -> None:
    def executor(tool: str, args: dict[str, object]) -> object:
        if tool == "slow" and args.get("retry"):
            return "ready"
        return f"{tool}-value"

    plan = DeterministicPlan(
        [
            ToolCall("lookup", output="x", result_type="str"),
            ToolCall("slow", {"retry": True}, output="y", result_type="str"),
        ]
    )
    report = plan.execute(ProgrammaticSession(), executor)
    assert report.complete
    assert report.retries == 0
    assert report.output("x") == "lookup-value"
    assert report.output("y") == "ready"
    assert [r.output for r in report.records] == ["x", "y"]


def test_plan_stops_on_permanent_failure() -> None:
    def executor(tool: str, args: dict[str, object]) -> object:
        if tool == "bad":
            raise RuntimeError("no")
        return tool

    report = DeterministicPlan(
        [ToolCall("ok", output="a"), ToolCall("bad", output="b"), ToolCall("ok", output="c")]
    ).execute(ProgrammaticSession(), executor)
    assert not report.complete
    assert [r.output for r in report.records] == ["a", "b"]
    assert report.to_dict()["complete"] is False


def test_snapshot_and_clear() -> None:
    session = ProgrammaticSession()
    session.set("a", 1)
    session.set("b", 2)
    assert session.snapshot() == {"a": 1, "b": 2}
    session.clear()
    assert session.snapshot() == {}


def test_invalid_plan_and_call_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DeterministicPlan([])
    with pytest.raises(ValueError, match="tool"):
        ToolCall("", output="x")
    with pytest.raises(ValueError, match="output"):
        ToolCall("lookup", output="")
