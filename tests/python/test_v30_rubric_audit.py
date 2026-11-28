"""Tests for the V29 #138 repository-grounded rubric wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ultimate_coders.agent import rubric_audit as ra
from ultimate_coders.agent.rubric_audit import (
    RubricChecks,
    audit_attempt,
    build_rubric,
    checks_for,
    rubric_audit_enabled,
)

PATCH = """diff --git a/src/cache.py b/src/cache.py
--- a/src/cache.py
+++ b/src/cache.py
@@ -10,3 +10,4 @@ def read(key):
-    return store[key]
+    entry = store[key]
+    return entry.value
"""

TEST_PATCH = """--- a/tests/test_cache.py
+++ b/tests/test_cache.py
@@ -1,2 +1,3 @@
 def test_read():
-    assert read("a") == 1
+    assert read("a") == 2
+    assert read("b") == 3
"""


class TestFlag:
    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ra.RUBRIC_AUDIT_ENV, raising=False)
        monkeypatch.setenv("UC_REPO_RUBRIC_SCORER", "1")
        assert rubric_audit_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_truthy(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(ra.RUBRIC_AUDIT_ENV, value)
        monkeypatch.setenv("UC_REPO_RUBRIC_SCORER", "true")
        assert rubric_audit_enabled() is True

    def test_requires_the_scorer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ra.RUBRIC_AUDIT_ENV, "1")
        monkeypatch.delenv("UC_REPO_RUBRIC_SCORER", raising=False)
        assert rubric_audit_enabled() is False


class TestRubric:
    def test_one_item_per_dimension(self) -> None:
        rubric = build_rubric("abc123")
        assert [item.item_id for item in rubric.items] == [
            "correctness",
            "test_coverage",
            "compatibility",
            "security",
            "maintainability",
        ]
        assert [item.required for item in rubric.items] == [True, False, False, False, False]
        assert rubric.commit == "abc123"

    def test_empty_commit_defaults(self) -> None:
        assert build_rubric("").commit == "attempt"


class TestChecks:
    def test_counts_and_paths(self) -> None:
        checks = checks_for(diff=PATCH, paths=("src/cache.py",), description="fix the cache")
        assert checks.added_lines == 2
        assert checks.removed_lines == 1
        assert checks.paths == ("src/cache.py",)
        assert checks.touches_tests is False
        assert checks.fix_requested is True
        assert checks.observed_fix is False

    def test_paths_are_derived_from_the_diff(self) -> None:
        checks = checks_for(diff=PATCH)
        assert checks.paths == ("src/cache.py",)

    def test_paths_are_deduplicated_and_normalised(self) -> None:
        checks = checks_for(diff=PATCH, paths=("src/cache.py", "src\\cache.py"))
        assert checks.paths == ("src/cache.py",)

    def test_test_path_detection(self) -> None:
        checks = checks_for(diff=TEST_PATCH)
        assert checks.touches_tests is True

    def test_constraint_respected_and_violated(self) -> None:
        inside = checks_for(diff=PATCH, constraints=("src/",))
        assert inside.constraints_declared is True
        assert inside.constraints_respected is True
        outside = checks_for(diff=PATCH, constraints=("docs/",))
        assert outside.constraints_respected is False

    def test_no_constraints_is_not_a_violation(self) -> None:
        checks = checks_for(diff=PATCH)
        assert "compatibility" in checks.features()

    def test_hazards_are_detected_on_added_lines_only(self) -> None:
        removed_hazard = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-eval(payload)\n+payload\n"
        assert checks_for(diff=removed_hazard).hazards == ()
        added_hazard = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-value\n+eval(payload)\n"
        assert checks_for(diff=added_hazard).hazards == ("eval",)

    def test_marker_debt_and_bare_except(self) -> None:
        dirty = (
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1,3 @@\n"
            "-value\n+value = 1  # TODO tidy\n+try:\n+    pass\n+except:\n+    pass\n"
        )
        checks = checks_for(diff=dirty)
        assert checks.added_markers == 1
        assert checks.bare_excepts == 1
        assert "maintainability" not in checks.features()

    def test_observed_fix_needs_passing_tests_without_failures(self) -> None:
        assert checks_for(diff=PATCH, tests_passed=3).observed_fix is True
        assert checks_for(diff=PATCH, tests_passed=3, tests_failed=1).observed_fix is False
        assert checks_for(diff=PATCH).observed_fix is False

    def test_features_only_mark_passed_checks(self) -> None:
        checks = checks_for(diff=PATCH, constraints=("docs/",))
        features = checks.features()
        assert "correctness" in features
        assert "compatibility" not in features

    def test_to_dict_is_bounded(self) -> None:
        many = tuple(f"src/f{index}.py" for index in range(40))
        checks = RubricChecks(
            added_lines=1,
            removed_lines=0,
            paths=many,
            touches_tests=False,
            fix_requested=False,
            observed_fix=False,
            constraints_respected=True,
            constraints_declared=False,
            hazards=(),
            added_markers=0,
            bare_excepts=0,
        )
        assert len(checks.to_dict()["paths"]) == ra.MAX_PATH_LIST


class TestAudit:
    def test_clean_patch_scores_well_but_not_passed(self) -> None:
        audit = audit_attempt(
            diff=PATCH, paths=("src/cache.py",), description="fix the cache"
        )
        assert audit.score.weighted_score > 0.6
        assert audit.score.passed is False  # no observed tests
        event = audit.to_event()
        assert event["rubric_weighted_ok"] is True
        assert event["rubric_passed"] is False
        assert event["rubric_advisory"] is True
        assert event["phase"] == "rubric audit"

    def test_observed_tests_can_pass_the_rubric(self) -> None:
        audit = audit_attempt(
            diff=PATCH,
            paths=("src/cache.py",),
            description="fix the cache",
            tests_passed=4,
        )
        assert audit.score.passed is True
        assert audit.to_event()["rubric_observed_fix"] is True

    def test_failed_tests_block_the_pass(self) -> None:
        audit = audit_attempt(
            diff=PATCH,
            paths=("src/cache.py",),
            description="fix the cache",
            tests_passed=4,
            tests_failed=1,
        )
        assert audit.score.passed is False

    def test_deletion_only_patch_misses_correctness(self) -> None:
        deletion = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +0,0 @@\n-a = 1\n-b = 2\n"
        audit = audit_attempt(diff=deletion, description="clean up")
        assert "correctness" in audit.score.missing_required
        assert audit.to_event()["rubric_missing"] == ["correctness"]

    def test_hazard_is_reported(self) -> None:
        risky = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-value\n+subprocess.run(cmd, shell=True)\n"
        event = audit_attempt(diff=risky, description="run the tool").to_event()
        assert event["rubric_hazards"] == ["shell=True"]

    def test_event_carries_no_patch_text(self) -> None:
        event = audit_attempt(diff=PATCH, description="fix the cache").to_event()
        assert "store[key]" not in repr(event)
        assert set(event) <= {
            "phase",
            "percent",
            "rubric_score",
            "rubric_passed",
            "rubric_weighted_ok",
            "rubric_satisfied",
            "rubric_missing",
            "rubric_hazards",
            "rubric_observed_fix",
            "rubric_advisory",
        }

    def test_patch_id_is_recorded(self) -> None:
        audit = audit_attempt(diff=PATCH, patch_id="sub-7:attempt")
        assert audit.score.patch_id == "sub-7:attempt"

    def test_empty_patch_is_scored_not_raised(self) -> None:
        audit = audit_attempt(diff="", description="")
        # An empty patch cannot violate anything, so only the "did something"
        # checks fail; the score stays low because correctness is required.
        assert audit.score.satisfied == ("compatibility", "security", "maintainability")
        assert "correctness" in audit.score.missing_required
        assert audit.score.weighted_score == pytest.approx(1.5 / 3.3)


class TestWorkerWiring:
    def _worker(self):
        from ultimate_coders.agent.worker import Worker

        return Worker.__new__(Worker)

    def _subtask(self, description: str = "fix the cache", constraints=("src/",)):
        return SimpleNamespace(
            id="sub-1",
            description=description,
            file_constraints=list(constraints),
        )

    def _output(self, diff: str = PATCH):
        return SimpleNamespace(
            file_changes=[
                SimpleNamespace(file_path="src/cache.py", diff=diff, change_type="modified")
            ]
        )

    def test_event_is_emitted_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ra.RUBRIC_AUDIT_ENV, "1")
        monkeypatch.setenv("UC_REPO_RUBRIC_SCORER", "1")
        event = self._worker()._rubric_audit_event(self._subtask(), self._output())
        assert event is not None
        assert event["phase"] == "rubric audit"
        assert event["rubric_weighted_ok"] is True

    def test_disabled_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ra.RUBRIC_AUDIT_ENV, raising=False)
        assert self._worker()._rubric_audit_event(self._subtask(), self._output()) is None

    def test_observed_delta_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ra.RUBRIC_AUDIT_ENV, "1")
        monkeypatch.setenv("UC_REPO_RUBRIC_SCORER", "1")
        test_event = {
            "test_execution_status": "ok",
            "f2p": ["tests/test_cache.py::test_read"],
            "p2p": ["tests/test_cache.py::test_write"],
            "broken": [],
        }
        event = self._worker()._rubric_audit_event(
            self._subtask(), self._output(), test_event
        )
        assert event["rubric_observed_fix"] is True
        assert event["rubric_passed"] is True

    def test_error_delta_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ra.RUBRIC_AUDIT_ENV, "1")
        monkeypatch.setenv("UC_REPO_RUBRIC_SCORER", "1")
        event = self._worker()._rubric_audit_event(
            self._subtask(), self._output(), {"test_execution_status": "error"}
        )
        assert event["rubric_observed_fix"] is False

    def test_constraints_from_the_subtask_are_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ra.RUBRIC_AUDIT_ENV, "1")
        monkeypatch.setenv("UC_REPO_RUBRIC_SCORER", "1")
        event = self._worker()._rubric_audit_event(
            self._subtask(constraints=("docs/",)), self._output()
        )
        assert "compatibility" not in event["rubric_satisfied"]
