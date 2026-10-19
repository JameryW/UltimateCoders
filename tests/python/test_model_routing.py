"""Model routing: stage/risk decisions, and whether a failure is worth another call.

Covers the last gap row of ``docs/agent-os-architecture.md`` §5:

* risk derivation from a unit of work's shape (never from a model),
* the (stage, risk) routing table, including its malformed-input behavior,
* the schema-failure escalation to a reduced recovery retry,
* the failure classification that decides whether another LLM call is
  worth making (transport -> retry, credentials -> gateway, malformed
  request -> one reduced repair, unknown -> retry bounded by the
  evidence gate),
* and the two integrations: the orchestrator's decompose calls and the
  worker's retry gates.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from ultimate_coders.agent.context_compiler import compile_context
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.routing import (
    DEFAULT_REPAIR_SCALE,
    ENV_ROUTE_TABLE,
    FailureKind,
    ModelRouter,
    ReasoningBudget,
    RiskLevel,
    RoutingTable,
    Stage,
    classify_failure,
    derive_risk,
)
from ultimate_coders.agent.sandbox import SandboxConfig
from ultimate_coders.agent.types import Subtask, SubtaskResult
from ultimate_coders.agent.worker import Worker


def _subtask(description: str, files: tuple[str, ...] = (), caps: tuple[str, ...] = ()) -> Subtask:
    return Subtask(
        id="st-route-1",
        parent_id="task-route-1",
        description=description,
        file_constraints=list(files),
        required_capabilities=list(caps),
    )


# ── risk derivation ──────────────────────────────────────────────


class TestDeriveRisk:
    def test_declared_read_only_capability_outranks_keywords(self):
        # Reviewing a deploy script is still read-only work: the capability
        # is enforced by the tool allowlist, the keyword is only prose.
        assert (
            derive_risk("Review the deploy script", capabilities=["review"])
            == RiskLevel.LOW.value
        )

    def test_dangerous_keywords_are_high(self):
        assert derive_risk("Deploy the service to production") == RiskLevel.HIGH.value
        assert derive_risk("Migrate the database schema") == RiskLevel.HIGH.value

    def test_wide_conflict_surface_is_high(self):
        assert derive_risk("Touch a few files", ("a.py", "b.py", "c.py")) == RiskLevel.HIGH.value

    def test_read_only_keywords_are_low(self):
        assert derive_risk("Audit the auth flow for gaps") == RiskLevel.LOW.value

    def test_writes_are_medium(self):
        assert derive_risk("Fix the login bug") == RiskLevel.MEDIUM.value

    def test_read_only_and_write_keywords_together_are_not_low(self):
        # "Review the fix" writes nothing but says so ambiguously; the
        # safe direction is not the cheap model.
        assert derive_risk("Review the fix for the bug") == RiskLevel.MEDIUM.value

    def test_unrecognized_shape_is_medium_not_low(self):
        assert derive_risk("Do the thing") == RiskLevel.MEDIUM.value

    def test_empty_description_is_medium(self):
        assert derive_risk("") == RiskLevel.MEDIUM.value


# ── budget ───────────────────────────────────────────────────────


class TestReasoningBudget:
    def test_identity_when_nothing_is_set(self):
        assert ReasoningBudget().is_identity()

    def test_shrink_halves_every_set_field(self):
        budget = ReasoningBudget(max_tokens=8000, max_tool_rounds=10, context_budget=4000)
        shrunk = budget.shrink(0.5)
        assert shrunk.max_tokens == 4000
        assert shrunk.max_tool_rounds == 5
        assert shrunk.context_budget == 2000

    def test_shrink_keeps_unset_fields_unset(self):
        shrunk = ReasoningBudget(max_tokens=100).shrink(0.5)
        assert shrunk.max_tokens == 50
        assert shrunk.max_tool_rounds is None
        assert shrunk.context_budget is None

    def test_shrink_never_rounds_a_field_to_zero(self):
        assert ReasoningBudget(max_tokens=1).shrink(0.1).max_tokens == 1

    def test_shrink_of_identity_is_identity(self):
        assert ReasoningBudget().shrink(0.5).is_identity()

    def test_shrink_at_or_above_one_is_a_no_op(self):
        budget = ReasoningBudget(max_tokens=100)
        assert budget.shrink(1.0) is budget


# ── table parsing ────────────────────────────────────────────────


class TestRoutingTable:
    def test_empty_env_routes_identity(self):
        assert not RoutingTable.from_env("")
        assert not RoutingTable.from_env("   ")

    def test_malformed_json_disables_routing_without_raising(self):
        assert not RoutingTable.from_env("{not json")

    def test_non_object_json_disables_routing(self):
        assert not RoutingTable.from_env("[1, 2, 3]")

    def test_unknown_stage_or_risk_key_is_ignored(self):
        table = RoutingTable.from_env(
            '{"nonsense:high": {"max_tokens": 10}, "planning:extreme": {}}'
        )
        assert not table

    def test_non_object_cell_is_ignored(self):
        table = RoutingTable.from_env('{"planning:high": 5}')
        assert not table

    def test_non_positive_budget_fields_are_ignored_not_clamped(self):
        table = RoutingTable.from_env('{"planning:high": {"max_tokens": 0, "max_tool_rounds": -3}}')
        cell = table.cell(Stage.PLANNING, RiskLevel.HIGH)
        assert cell.budget.is_identity()

    def test_known_cell_round_trips(self):
        table = RoutingTable.from_env(
            '{"implementation:high": {"model": "m", "max_tokens": 8000, "max_tool_rounds": 10}}'
        )
        cell = table.cell("implementation", "high")
        assert cell.model == "m"
        assert cell.budget.max_tokens == 8000
        assert cell.budget.max_tool_rounds == 10

    def test_missing_cell_is_identity(self):
        table = RoutingTable.from_env('{"planning:high": {"max_tokens": 10}}')
        assert table.cell(Stage.PLANNING, RiskLevel.LOW).budget.is_identity()


# ── failure classification ───────────────────────────────────────


class TestClassifyFailure:
    @pytest.mark.parametrize(
        "error",
        [
            "429 rate limit exceeded",
            "503 The system is busy, try again later",
            "connection reset by peer",
            "Read timed out after 600s",
            "SSL handshake failed",
        ],
    )
    def test_transport_failures_are_worth_an_identical_retry(self, error):
        action = classify_failure(error)
        assert action.kind == FailureKind.TRANSIENT.value
        assert action.action == "retry"
        assert action.worth_another_call

    @pytest.mark.parametrize(
        "error",
        [
            "401 invalid_api_key",
            "403 forbidden",
            "404 model not found",
            "Not signed in. Run grok login --device-code",
            "permission denied by gateway",
        ],
    )
    def test_credential_failures_are_not_worth_any_retry(self, error):
        action = classify_failure(error)
        assert action.kind == FailureKind.AUTH.value
        assert action.action == "escalate_gateway"
        assert not action.worth_another_call

    @pytest.mark.parametrize(
        "error",
        [
            "400 bad request",
            "invalid request body",
            "malformed tool_use block",
            "json decode error in arguments",
            "context length exceeded",
        ],
    )
    def test_malformed_requests_are_worth_one_reduced_retry(self, error):
        action = classify_failure(error)
        assert action.kind == FailureKind.SCHEMA.value
        assert action.action == "lightweight_repair"
        assert action.worth_another_call

    def test_unclassified_failures_retry_bounded_by_the_evidence_gate(self):
        action = classify_failure("disk full on /tmp")
        assert action.kind == FailureKind.UNKNOWN.value
        assert action.action == "retry"
        assert action.worth_another_call
        assert "evidence gate" in action.reason

    def test_empty_error_text_abstains(self):
        action = classify_failure("   ")
        assert not action.worth_another_call
        assert action.action == "abstain"

    def test_transient_wins_over_schema_when_both_match(self):
        # A 429 whose text mentions a parameter is still a retry.
        action = classify_failure("429 rate limit: parameter window exhausted")
        assert action.kind == FailureKind.TRANSIENT.value

    def test_to_dict_is_log_safe(self):
        payload = classify_failure("400 bad request").to_dict()
        assert payload["kind"] == "schema"
        assert payload["worth_another_call"] is True
        assert set(payload) == {"kind", "action", "worth_another_call", "reason"}


# ── the router ───────────────────────────────────────────────────


class TestModelRouter:
    def test_no_table_routes_everything_to_identity(self):
        router = ModelRouter(RoutingTable())
        decision = router.route(Stage.IMPLEMENTATION, RiskLevel.HIGH)
        assert decision.is_identity
        assert decision.reason == "identity"
        assert decision.to_dict()["budget"]["max_tokens"] is None

    def test_configured_cell_is_used_verbatim(self):
        table = RoutingTable.from_env(
            '{"implementation:high":'
            ' {"model": "strong", "max_tokens": 8000, "max_tool_rounds": 10}}'
        )
        decision = ModelRouter(table).route(Stage.IMPLEMENTATION, RiskLevel.HIGH)
        assert decision.model == "strong"
        assert decision.budget.max_tokens == 8000
        assert decision.reason == "table"
        assert not decision.is_identity

    def test_schema_failure_escalates_to_recovery_with_a_reduced_budget(self):
        table = RoutingTable.from_env(
            '{"implementation:high":'
            ' {"model": "strong", "max_tokens": 8000, "max_tool_rounds": 10}}'
        )
        decision = ModelRouter(table).route(
            Stage.IMPLEMENTATION, RiskLevel.HIGH, failure="400 bad request"
        )
        assert decision.stage == Stage.RECOVERY.value
        assert decision.risk == RiskLevel.HIGH.value
        # Same model -- the work is the same; half the budget -- the request
        # must not be repeated verbatim.
        assert decision.model == "strong"
        assert decision.budget.max_tokens == 4000
        assert decision.budget.max_tool_rounds == 5
        assert "lightweight_repair" in decision.reason

    def test_explicit_recovery_cell_is_honored_as_written(self):
        table = RoutingTable.from_env(
            '{"implementation:high": {"max_tokens": 8000}, "recovery:high": {"max_tokens": 1000}}'
        )
        decision = ModelRouter(table).route(
            Stage.IMPLEMENTATION, RiskLevel.HIGH, failure="400 bad request"
        )
        assert decision.budget.max_tokens == 1000
        assert decision.reason == "table"

    def test_schema_failure_without_any_cell_stays_identity(self):
        # No table: the repair relies on the request change (the rejection
        # reason) plus the evidence gate, not on a budget nobody configured.
        decision = ModelRouter(RoutingTable()).route(
            Stage.IMPLEMENTATION, RiskLevel.HIGH, failure="400 bad request"
        )
        assert decision.is_identity
        assert decision.stage == Stage.RECOVERY.value

    def test_consecutive_schema_failures_do_not_compound(self):
        # The repair budget always derives from the work's ordinary cell,
        # so the third attempt spends the same as the second -- not half of
        # half. (A shrinking-per-attempt budget would starve the repair.)
        table = RoutingTable.from_env('{"implementation:medium": {"max_tokens": 4096}}')
        router = ModelRouter(table)
        first = router.route_subtask("Fix the bug", attempt=1, last_failure="400 bad request")
        second = router.route_subtask("Fix the bug", attempt=2, last_failure="400 bad request")
        assert first.budget.max_tokens == 2048
        assert second.budget.max_tokens == 2048

    def test_transient_failure_does_not_escalate(self):
        table = RoutingTable.from_env('{"implementation:high": {"max_tokens": 8000}}')
        decision = ModelRouter(table).route(
            Stage.IMPLEMENTATION, RiskLevel.HIGH, failure="503 overloaded"
        )
        assert decision.stage == Stage.IMPLEMENTATION.value
        assert decision.budget.max_tokens == 8000

    def test_auth_failure_does_not_escalate(self):
        table = RoutingTable.from_env('{"implementation:high": {"max_tokens": 8000}}')
        decision = ModelRouter(table).route(
            Stage.IMPLEMENTATION, RiskLevel.HIGH, failure="401 unauthorized"
        )
        assert decision.stage == Stage.IMPLEMENTATION.value

    def test_route_subtask_picks_the_stage_from_the_attempt_role(self):
        router = ModelRouter(RoutingTable())
        assert router.route_subtask("Fix the bug").stage == Stage.IMPLEMENTATION.value
        assert (
            router.route_subtask("Review the change", capabilities=["review"]).stage
            == Stage.VERIFICATION.value
        )
        assert router.route_subtask("Fix the bug", attempt=2).stage == Stage.RECOVERY.value

    def test_route_subtask_derives_risk_from_the_subtask_shape(self):
        router = ModelRouter(RoutingTable())
        assert router.route_subtask("Deploy to production").risk == RiskLevel.HIGH.value
        assert router.route_subtask("Fix the bug").risk == RiskLevel.MEDIUM.value

    def test_repair_scale_is_configurable(self):
        table = RoutingTable.from_env('{"implementation:high": {"max_tokens": 8000}}')
        router = ModelRouter(table, repair_scale=0.25)
        decision = router.route(Stage.IMPLEMENTATION, RiskLevel.HIGH, failure="400 bad request")
        assert decision.budget.max_tokens == 2000

    def test_default_repair_scale_is_half(self):
        assert DEFAULT_REPAIR_SCALE == 0.5


# ── orchestrator: planning-stage routing ─────────────────────────


class _RecordingLLM:
    """Minimal llm_client stand-in: records calls, rejects once, then plans."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, messages=None, system=None, max_tokens=None, **kwargs):
        self.calls.append({"max_tokens": max_tokens, **kwargs})
        if len(self.calls) == 1:
            return _TextResponse("this is not a json array")
        return _TextResponse(
            '[{"description": "step one", "depends_on": [], "expected_output": "x"}]'
        )


class _TextResponse:
    def __init__(self, text: str) -> None:
        self.text = text


def _orchestrator(llm: _RecordingLLM, table_json: str | None) -> Orchestrator:
    if table_json:
        router = ModelRouter(RoutingTable.from_env(table_json))
    else:
        router = ModelRouter(RoutingTable())
    return Orchestrator(llm_client=llm, model_router=router)


class TestOrchestratorPlanningRoute:
    def test_no_table_keeps_the_historical_default_budget(self):
        llm = _RecordingLLM()
        asyncio.run(_orchestrator(llm, None).submit_task("Ship the thing"))
        assert [c["max_tokens"] for c in llm.calls] == [4096, 4096]
        assert all("model" not in c for c in llm.calls)

    def test_planning_cell_sets_model_and_budget(self):
        llm = _RecordingLLM()
        table = '{"planning:medium": {"model": "planner", "max_tokens": 3000}}'
        asyncio.run(_orchestrator(llm, table).submit_task("Update the config"))
        first = llm.calls[0]
        assert first["model"] == "planner"
        assert first["max_tokens"] == 3000

    def test_rejected_plan_re_asks_at_the_recovery_budget(self):
        llm = _RecordingLLM()
        table = '{"planning:high": {"model": "planner", "max_tokens": 9000}}'
        asyncio.run(_orchestrator(llm, table).submit_task("Deploy the service to production"))
        # First call: the planning cell. Second call: the plan was rejected
        # (a malformed result), so it re-asks at half the budget, same model.
        assert llm.calls[0]["max_tokens"] == 9000
        assert llm.calls[1]["max_tokens"] == 4500
        assert llm.calls[1]["model"] == "planner"


# ── worker: failure actions and route recording ──────────────────


def _worker(router) -> Worker:
    return Worker(engine=None, sandbox_config=SandboxConfig(), model_router=router)


def _failing_sandbox(error: str, counter: list[int]):
    async def _fail(*args, **kwargs):
        counter[0] += 1
        return SubtaskResult(
            subtask_id="st-route-1",
            success=False,
            summary=error,
            error=error,
        )

    return _fail


def _run_with_recorder(worker: Worker, subtask: Subtask, error: str):
    """Run one subtask whose sandbox always fails with ``error``.

    Returns (call_count, events) where events are the (type, data) pairs
    the worker published.
    """
    events: list[tuple[str, dict]] = []
    counter = [0]

    async def _record(event_type, task_id=None, subtask_id=None, data=None, **kwargs):
        events.append((event_type, data or {}))

    async def _instant_sleep(*args, **kwargs):
        return None

    worker._publish_event = _record  # type: ignore[assignment]
    with patch.object(worker, "_execute_in_sandbox", side_effect=_failing_sandbox(error, counter)):
        with patch("ultimate_coders.agent.worker.asyncio.sleep", side_effect=_instant_sleep):
            result = asyncio.run(worker.execute_subtask(subtask))
    return counter[0], events, result


class TestWorkerFailureActions:
    def test_schema_failure_gets_its_retries_bounded_by_the_loop(self):
        # A 400 is a lightweight repair, not a permanent wall: the worker
        # retries (bounded by MAX_RETRIES and the evidence gate) instead of
        # failing on the first occurrence.
        worker = _worker(ModelRouter(RoutingTable()))
        subtask = _subtask("Fix the login bug")
        count, events, _ = _run_with_recorder(worker, subtask, "400 bad request: tool schema")
        assert count == worker.MAX_RETRIES
        failed = [d for t, d in events if t == "subtask_failed"]
        assert failed and failed[0]["failure_action"] == "lightweight_repair"

    def test_auth_failure_still_fails_once(self):
        worker = _worker(ModelRouter(RoutingTable()))
        subtask = _subtask("Fix the login bug")
        count, events, _ = _run_with_recorder(worker, subtask, "401 invalid_api_key")
        assert count == 1
        failed = [d for t, d in events if t == "subtask_failed"]
        assert failed and failed[0]["failure_action"] == "escalate_gateway"

    def test_transient_failure_retries(self):
        worker = _worker(ModelRouter(RoutingTable()))
        subtask = _subtask("Fix the login bug")
        count, events, _ = _run_with_recorder(worker, subtask, "503 system is busy")
        assert count == worker.MAX_RETRIES
        retries = [d for t, d in events if t == "subtask_retry"]
        assert retries and retries[0]["failure_action"] == "retry"

    def test_no_table_records_no_route_field(self):
        worker = _worker(ModelRouter(RoutingTable()))
        subtask = _subtask("Fix the login bug")
        _, events, _ = _run_with_recorder(worker, subtask, "503 system is busy")
        started = [d for t, d in events if t == "subtask_started"]
        assert started and "route" not in started[0]

    def test_configured_table_records_the_route_on_started(self):
        table = RoutingTable.from_env(
            '{"implementation:medium": {"model": "m", "max_tokens": 2048}}'
        )
        worker = _worker(ModelRouter(table))
        subtask = _subtask("Fix the login bug")
        _, events, _ = _run_with_recorder(worker, subtask, "503 system is busy")
        started = [d for t, d in events if t == "subtask_started"]
        assert started, "subtask_started was not published"
        route = started[0]["route"]
        assert route["stage"] == Stage.IMPLEMENTATION.value
        assert route["risk"] == RiskLevel.MEDIUM.value
        assert route["model"] == "m"
        assert route["budget"]["max_tokens"] == 2048

    def test_retry_event_records_the_recovery_route(self):
        table = RoutingTable.from_env('{"implementation:medium": {"max_tokens": 4096}}')
        worker = _worker(ModelRouter(table))
        subtask = _subtask("Fix the login bug")
        _, events, _ = _run_with_recorder(worker, subtask, "400 bad request")
        # The failed-attempt event (published before each retry) carries the
        # route the next attempt will run at: recovery stage, half budget.
        failed = [d for t, d in events if t == "subtask_failed"]
        assert failed, "no failed event was published"
        assert failed[0]["route"]["stage"] == Stage.RECOVERY.value
        assert failed[0]["route"]["budget"]["max_tokens"] == 2048

    def test_exception_path_uses_the_same_classification(self):
        worker = _worker(ModelRouter(RoutingTable()))
        subtask = _subtask("Fix the login bug")

        async def _boom(*args, **kwargs):
            raise RuntimeError("403 forbidden by policy")

        events: list[tuple[str, dict]] = []

        async def _record(event_type, task_id=None, subtask_id=None, data=None, **kwargs):
            events.append((event_type, data or {}))

        async def _instant_sleep(*args, **kwargs):
            return None

        worker._publish_event = _record  # type: ignore[assignment]
        with patch.object(worker, "_execute_in_sandbox", side_effect=_boom):
            with patch("ultimate_coders.agent.worker.asyncio.sleep", side_effect=_instant_sleep):
                result = asyncio.run(worker.execute_subtask(subtask))
        assert result.success is False
        assert not any(t == "subtask_retry" for t, _ in events)


def test_env_table_name_is_stable():
    assert ENV_ROUTE_TABLE == "UC_ROUTE_TABLE"


# ── worker: the routed risk reaches the context compiler ───────────


class TestWorkerContextRiskRouting:
    """Integration: ``compile_context`` sees the routed risk, not a constant."""

    def _run_capturing(self, description: str, caps: tuple[str, ...] = ()) -> dict:
        worker = _worker(ModelRouter(RoutingTable()))
        subtask = _subtask(description, caps=caps)
        captured: dict = {}

        def _capture(items, *, budget_tokens, risk, now=None):
            captured["risk"] = risk
            captured["budget_tokens"] = budget_tokens
            return compile_context(items, budget_tokens=budget_tokens, risk=risk)

        async def _ok(*args, **kwargs):
            return SubtaskResult(subtask_id="st-route-1", success=True, summary="ok")

        async def _record(event_type, task_id=None, subtask_id=None, data=None, **kwargs):
            return None

        async def _instant_sleep(*args, **kwargs):
            return None

        worker._publish_event = _record  # type: ignore[assignment]
        with patch.object(worker, "_execute_in_sandbox", side_effect=_ok):
            with patch("ultimate_coders.agent.worker.asyncio.sleep", side_effect=_instant_sleep):
                with patch("ultimate_coders.agent.worker.compile_context", side_effect=_capture):
                    asyncio.run(worker.execute_subtask(subtask))
        return captured

    def test_high_risk_subtask_compiles_context_at_high_risk(self):
        captured = self._run_capturing("Deploy the service to production")
        assert captured["risk"] == "high"
        assert captured["budget_tokens"] > 0

    def test_ordinary_subtask_compiles_context_at_medium_risk(self):
        captured = self._run_capturing("Fix the login bug")
        assert captured["risk"] == "medium"

    def test_read_only_review_compiles_context_at_low_risk(self):
        captured = self._run_capturing("Review the auth flow for gaps", caps=("review",))
        assert captured["risk"] == "low"
