"""ExecCritic: split test generation from source repair.

ExecCritic (#14) separates two roles that a coding agent usually mixes:

* :class:`TestGenNode` turns a task's candidate tests into a frozen, hidden
  :class:`TestArtifact`.  Generation is *gold-free*: it only sees the base
  tree and the tests, never a reference patch.  A valid artifact must carry a
  non-empty test patch, a runnable command, a JSON test contract, and evidence
  that the tests fail on the clean base (otherwise they cannot prove a fix).
* :class:`SourceRepairNode` may only edit source.  Test files are protected by
  construction -- a repair patch that touches them raises
  :class:`ProtectedTestError` -- and reviewer feedback is capped at 6000
  characters before it can reach the next attempt.

Generation can fail in two very different ways, and callers must not treat
them alike: ``hard_fail`` means the candidate tests are unusable, while
``inconclusive_infra`` means the environment could not run the clean-base
check (a retry or a different runner might succeed).

The module is opt-in and default-off (``UC_EXEC_CRITIC``) and uses the
standard library only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "EXEC_CRITIC_ENV",
    "MAX_FEEDBACK_CHARS",
    "PROTECTED_TEST_PATTERNS",
    "STATUS_HARD_FAIL",
    "STATUS_INCONCLUSIVE_INFRA",
    "STATUS_OK",
    "ExecCriticError",
    "ProtectedTestError",
    "RepairResult",
    "SourceRepairNode",
    "TestArtifact",
    "TestGenNode",
    "exec_critic_enabled",
    "is_protected_test_path",
]

EXEC_CRITIC_ENV = "UC_EXEC_CRITIC"
MAX_FEEDBACK_CHARS = 6000

STATUS_OK = "ok"
STATUS_HARD_FAIL = "hard_fail"
STATUS_INCONCLUSIVE_INFRA = "inconclusive_infra"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

PROTECTED_TEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]*$"),
    re.compile(r"(^|/)[^/]*_test\.[A-Za-z0-9]+$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"(^|/)pytest\.ini$"),
    re.compile(r"(^|/)tox\.ini$"),
)

_PATCH_PATH_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:[ab]/)?(\S+)")


def exec_critic_enabled() -> bool:
    """Feature flag: ExecCritic is enabled only when ``UC_EXEC_CRITIC`` is truthy."""

    return os.environ.get(EXEC_CRITIC_ENV, "").strip().lower() in _TRUTHY


class ExecCriticError(ValueError):
    """Raised when an ExecCritic input is structurally invalid."""


class ProtectedTestError(ExecCriticError):
    """Raised when a source-repair patch attempts to modify protected test files."""


@dataclass(frozen=True)
class TestArtifact:
    """A validated, frozen test artifact produced by :class:`TestGenNode`."""

    __test__ = False  # not a pytest test class despite the TestArtifact name

    test_patch: str
    test_command: str
    test_contract: dict[str, Any]
    base_clean_fail: bool
    hidden: bool = True
    frozen: bool = True
    status: str = STATUS_OK
    reason: str = ""
    task_id: str = ""

    def __post_init__(self) -> None:
        if self.status not in {STATUS_OK, STATUS_HARD_FAIL, STATUS_INCONCLUSIVE_INFRA}:
            raise ExecCriticError(f"unknown TestArtifact status: {self.status!r}")
        object.__setattr__(self, "test_patch", str(self.test_patch or ""))
        object.__setattr__(self, "test_command", str(self.test_command or ""))
        object.__setattr__(self, "test_contract", dict(self.test_contract or {}))
        object.__setattr__(self, "base_clean_fail", bool(self.base_clean_fail))
        object.__setattr__(self, "hidden", bool(self.hidden))
        object.__setattr__(self, "frozen", bool(self.frozen))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "task_id", str(self.task_id or ""))

    @property
    def ok(self) -> bool:
        """True when the artifact is usable."""

        return self.status == STATUS_OK

    @property
    def is_hidden_and_frozen(self) -> bool:
        """True when the artifact keeps both test protections enabled."""

        return self.hidden and self.frozen

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping of this artifact."""

        return {
            "task_id": self.task_id,
            "test_patch": self.test_patch,
            "test_command": self.test_command,
            "test_contract": dict(self.test_contract),
            "base_clean_fail": self.base_clean_fail,
            "hidden": self.hidden,
            "frozen": self.frozen,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RepairResult:
    """Outcome of one source-only repair attempt."""

    status: str
    source_patch: str
    feedback_chars: int
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in {STATUS_OK, STATUS_HARD_FAIL, STATUS_INCONCLUSIVE_INFRA}:
            raise ExecCriticError(f"unknown RepairResult status: {self.status!r}")
        object.__setattr__(self, "source_patch", str(self.source_patch or ""))
        object.__setattr__(self, "feedback_chars", max(0, int(self.feedback_chars)))
        object.__setattr__(self, "reason", str(self.reason or ""))

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


BaseRunner = Callable[[Any, str, str], Any]


class TestGenNode:
    """Generate and validate a hidden, frozen test artifact.

    ``base_runner`` is an optional injected callable ``(base_tree, test_patch,
    test_command) -> bool | mapping`` used to prove the tests fail on the clean
    base.  Without a runner the node trusts an explicit ``base_clean_fail``
    field on ``base_tree`` or the candidate.  A runner that raises an
    environment-shaped exception yields ``inconclusive_infra``; bad test
    content always yields ``hard_fail``.
    """

    __test__ = False  # not a pytest test class despite the TestGenNode name

    def __init__(self, base_runner: BaseRunner | None = None) -> None:
        self.base_runner = base_runner

    def generate(
        self,
        task_id: str,
        base_tree: Any,
        candidate_tests: Any,
    ) -> TestArtifact:
        """Validate ``candidate_tests`` against the clean base and return an artifact."""

        try:
            candidate = _as_mapping(candidate_tests)
        except ExecCriticError as exc:
            return TestArtifact(
                test_patch="",
                test_command="",
                test_contract={},
                base_clean_fail=False,
                status=STATUS_HARD_FAIL,
                reason=str(exc),
                task_id=str(task_id or ""),
            )
        test_patch = str(_field(candidate, "test_patch") or "").strip()
        test_command = str(_field(candidate, "test_command") or "").strip()
        contract_value = _field(candidate, "test_contract", _field(candidate, "contract"))
        hidden = bool(_field(candidate, "hidden", True))
        frozen = bool(_field(candidate, "frozen", True))

        if not test_patch:
            return self._failed(task_id, STATUS_HARD_FAIL, "test_patch must be non-empty")
        if not test_command:
            return self._failed(task_id, STATUS_HARD_FAIL, "test_command must be non-empty")
        try:
            contract = _parse_contract(contract_value)
        except ExecCriticError as exc:
            return self._failed(task_id, STATUS_HARD_FAIL, str(exc))
        if not hidden or not frozen:
            return self._failed(
                task_id,
                STATUS_HARD_FAIL,
                "generated tests must be hidden and frozen",
            )
        if _candidate_mentions_gold(candidate):
            return self._failed(
                task_id,
                STATUS_HARD_FAIL,
                "gold-free generation: candidate tests may not carry a reference patch",
            )

        declared_clean_fail = _clean_fail_evidence(base_tree, candidate)
        if self.base_runner is not None:
            try:
                result = self.base_runner(base_tree, test_patch, test_command)
            except Exception as exc:  # noqa: BLE001 - infra errors are classified, not swallowed
                return self._failed(
                    task_id,
                    STATUS_INCONCLUSIVE_INFRA,
                    f"clean-base run failed in infrastructure: {type(exc).__name__}: {exc}",
                )
            clean_fail = _interpret_runner_result(result)
            if clean_fail is None:
                return self._failed(
                    task_id,
                    STATUS_INCONCLUSIVE_INFRA,
                    "clean-base runner returned no conclusive result",
                )
        else:
            clean_fail = declared_clean_fail
        if clean_fail is None:
            return self._failed(
                task_id,
                STATUS_INCONCLUSIVE_INFRA,
                "no clean-base failure evidence available",
            )
        if not clean_fail:
            return self._failed(
                task_id,
                STATUS_HARD_FAIL,
                "tests must fail on the clean base",
            )
        return TestArtifact(
            test_patch=test_patch,
            test_command=test_command,
            test_contract=contract,
            base_clean_fail=True,
            hidden=True,
            frozen=True,
            status=STATUS_OK,
            task_id=str(task_id or ""),
        )

    def _failed(self, task_id: str, status: str, reason: str) -> TestArtifact:
        return TestArtifact(
            test_patch="",
            test_command="",
            test_contract={},
            base_clean_fail=False,
            status=status,
            reason=reason,
            task_id=str(task_id or ""),
        )


class SourceRepairNode:
    """Repair source only, with protected tests and bounded reviewer feedback."""

    def __init__(self, max_feedback_chars: int = MAX_FEEDBACK_CHARS) -> None:
        if max_feedback_chars < 0:
            raise ValueError("max_feedback_chars must be >= 0")
        self.max_feedback_chars = int(max_feedback_chars)

    def repair(
        self,
        task_id: str,
        source_patch: str = "",
        *,
        test_patch: str = "",
        feedback: str = "",
        previous_patch: str = "",
    ) -> RepairResult:
        """Produce a cumulative source patch or fail hard.

        ``test_patch`` is accepted only so the caller can show what is
        protected; any attempt to include test files in ``source_patch``
        raises :class:`ProtectedTestError`.  ``feedback`` is truncated to the
        configured cap (6000 characters by default) and its effective length
        is reported in :attr:`RepairResult.feedback_chars`.
        """

        del task_id  # kept in the signature for audit correlation by callers
        patch = str(source_patch or "").strip()
        if not patch:
            return RepairResult(
                status=STATUS_HARD_FAIL,
                source_patch="",
                feedback_chars=0,
                reason="source_patch must be non-empty",
            )
        protected = _protected_paths_in_patch(patch)
        if protected:
            raise ProtectedTestError(
                "source repair may not modify protected test files: " + ", ".join(sorted(protected))
            )
        if test_patch:
            # Test content is not applicable, and a repair patch must never
            # embed it; tolerate callers that pass it purely for contrast.
            embedded = _protected_paths_in_patch(str(test_patch))
            if embedded and _protected_paths_in_patch(patch) & embedded:
                raise ProtectedTestError(
                    "source repair duplicates a protected test patch: "
                    + ", ".join(sorted(embedded))
                )
        feedback_text = str(feedback or "")
        feedback_chars = min(len(feedback_text), self.max_feedback_chars)
        bounded_feedback = feedback_text[: self.max_feedback_chars]
        del bounded_feedback
        cumulative = _cumulative_patch(previous_patch, patch)
        return RepairResult(
            status=STATUS_OK,
            source_patch=cumulative,
            feedback_chars=feedback_chars,
        )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ExecCriticError("candidate_tests must be a mapping")


def _field(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _parse_contract(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        contract = dict(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ExecCriticError("test_contract JSON is required")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExecCriticError(f"test_contract is not valid JSON: {exc.msg}") from exc
        if not isinstance(parsed, Mapping):
            raise ExecCriticError("test_contract must be a JSON object")
        contract = dict(parsed)
    else:
        raise ExecCriticError("test_contract JSON is required")
    if not contract:
        raise ExecCriticError("test_contract must not be empty")
    try:
        json.dumps(contract)
    except (TypeError, ValueError) as exc:
        raise ExecCriticError("test_contract must be JSON-serializable") from exc
    return contract


def _candidate_mentions_gold(candidate: Mapping[str, Any]) -> bool:
    for key in ("gold_patch", "reference_patch", "solution_patch", "gold"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value not in (None, "", False, (), [], {}):
            return True
    return False


def _clean_fail_evidence(base_tree: Any, candidate: Mapping[str, Any]) -> bool | None:
    for source in (base_tree, candidate):
        if source is None:
            continue
        value = _field(source, "base_clean_fail", None)
        if value is None:
            value = _field(source, "clean_fail", None)
        if value is not None:
            return bool(value)
    return None


def _interpret_runner_result(result: Any) -> bool | None:
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        for key in ("base_clean_fail", "clean_fail", "failed"):
            if key in result:
                return bool(result[key])
        status = str(result.get("status", "")).strip().lower()
        if status in {"inconclusive_infra", "infra", "error"}:
            return None
    return None


def _cumulative_patch(previous_patch: str, source_patch: str) -> str:
    previous = str(previous_patch or "").strip()
    current = str(source_patch or "").strip()
    if not previous:
        return current
    if not current:
        return previous
    return previous.rstrip() + "\n" + current.lstrip()


def _protected_paths_in_patch(patch: str) -> set[str]:
    paths: set[str] = set()
    for raw_line in str(patch or "").splitlines():
        match = _PATCH_PATH_RE.match(raw_line.strip())
        if not match:
            continue
        path = match.group(1).strip()
        if path == "/dev/null":
            continue
        if path.startswith("b/") or path.startswith("a/"):
            path = path[2:]
        if _is_protected_test_path(path):
            paths.add(path)
    return paths


def _is_protected_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return any(pattern.search(normalized) for pattern in PROTECTED_TEST_PATTERNS)


def is_protected_test_path(path: str) -> bool:
    """Return whether *path* is part of the protected test surface."""

    return _is_protected_test_path(path)
