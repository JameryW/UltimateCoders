"""Tests for the step condition expression evaluator.

Covers the recursive-descent parser in step_condition.py:
  - Empty condition → True (always run)
  - prev.success (with/without prev, success/fail)
  - prev.files.contains("x")
  - prev.summary.contains("x")
  - Logical operators: &&, ||, !
  - Equality: ==, !=
  - Parenthesized expressions
  - Parse errors → ConditionError
  - Step 0 (prev=None → prev.* = False)
"""

from __future__ import annotations

import pytest
from ultimate_coders.agent.sandbox import AgentOutput
from ultimate_coders.agent.step_condition import ConditionError, evaluate
from ultimate_coders.agent.types import ChangeType, FileChange


def _prev_success(summary="ok", files=None) -> AgentOutput:
    """Build a successful AgentOutput with optional file changes."""
    return AgentOutput(summary=summary, file_changes=files or [], success=True)


def _prev_failed(summary="boom", files=None) -> AgentOutput:
    """Build a failed AgentOutput."""
    return AgentOutput(summary=summary, file_changes=files or [], success=False)


def _fc(path: str) -> FileChange:
    return FileChange(file_path=path, change_type=ChangeType.MODIFIED, diff="")


# ── Empty condition = always run ─────────────────────────────────


class TestEmptyCondition:
    def test_empty_string_returns_true(self):
        assert evaluate("", None) is True
        assert evaluate("", _prev_success()) is True

    def test_whitespace_only_returns_true(self):
        assert evaluate("   ", None) is True
        assert evaluate("\t\n", _prev_success()) is True

    def test_none_condition_treated_as_empty(self):
        # condition is str="", so this is the same as empty.
        assert evaluate("", None) is True


# ── prev.success ────────────────────────────────────────────────


class TestPrevSuccess:
    def test_prev_success_true_when_prev_succeeded(self):
        assert evaluate("prev.success", _prev_success()) is True

    def test_prev_success_false_when_prev_failed(self):
        assert evaluate("prev.success", _prev_failed()) is False

    def test_prev_success_false_when_no_prev(self):
        """Step 0 has no predecessor → prev.success = False."""
        assert evaluate("prev.success", None) is False


# ── prev.files.contains ────────────────────────────────────────


class TestPrevFilesContains:
    def test_contains_true_when_file_path_matches(self):
        prev = _prev_success(files=[_fc("src/main.rs"), _fc("src/lib.rs")])
        assert evaluate('prev.files.contains("src/main.rs")', prev) is True

    def test_contains_true_for_substring(self):
        prev = _prev_success(files=[_fc("src/auth/middleware.ts")])
        assert evaluate('prev.files.contains("src/auth/")', prev) is True

    def test_contains_false_when_no_match(self):
        prev = _prev_success(files=[_fc("src/main.rs")])
        assert evaluate('prev.files.contains("config/")', prev) is False

    def test_contains_false_when_no_prev(self):
        assert evaluate('prev.files.contains("anything")', None) is False

    def test_contains_false_when_empty_files(self):
        prev = _prev_success(files=[])
        assert evaluate('prev.files.contains("x")', prev) is False

    def test_contains_checks_all_files(self):
        prev = _prev_success(files=[_fc("a.rs"), _fc("b/deploy.yaml")])
        assert evaluate('prev.files.contains("deploy")', prev) is True


# ── prev.summary.contains ───────────────────────────────────────


class TestPrevSummaryContains:
    def test_contains_true_when_summary_has_keyword(self):
        prev = _prev_success(summary="Found 3 issues in the code")
        assert evaluate('prev.summary.contains("issues")', prev) is True

    def test_contains_false_when_summary_lacks_keyword(self):
        prev = _prev_success(summary="All good, no issues found")
        assert evaluate('prev.summary.contains("error")', prev) is False

    def test_contains_false_when_no_prev(self):
        assert evaluate('prev.summary.contains("x")', None) is False

    def test_contains_empty_string(self):
        """contains("") — empty needle is always a substring."""
        prev = _prev_success(summary="anything")
        assert evaluate('prev.summary.contains("")', prev) is True


# ── Literals: true / false ──────────────────────────────────────


class TestLiterals:
    def test_true_literal(self):
        assert evaluate("true", None) is True

    def test_false_literal(self):
        assert evaluate("false", None) is False

    def test_true_with_prev(self):
        assert evaluate("true", _prev_success()) is True


# ── Logical NOT ─────────────────────────────────────────────────


class TestNot:
    def test_not_true_is_false(self):
        assert evaluate("!true", None) is False

    def test_not_false_is_true(self):
        assert evaluate("!false", None) is True

    def test_not_prev_success_when_no_prev(self):
        """!prev.success = True when there's no predecessor (step 0)."""
        assert evaluate("!prev.success", None) is True

    def test_not_prev_success_when_prev_succeeded(self):
        assert evaluate("!prev.success", _prev_success()) is False

    def test_double_not(self):
        assert evaluate("!!true", None) is True
        assert evaluate("!!false", None) is False

    def test_not_with_parens(self):
        assert evaluate("!(true)", None) is False
        assert evaluate("!(false)", None) is True


# ── Logical AND ─────────────────────────────────────────────────


class TestAnd:
    def test_true_and_true(self):
        assert evaluate("true && true", None) is True

    def test_true_and_false(self):
        assert evaluate("true && false", None) is False

    def test_prev_success_and_files_contains(self):
        prev = _prev_success(
            summary="ok", files=[_fc("src/main.rs")]
        )
        assert evaluate('prev.success && prev.files.contains("src/")', prev) is True

    def test_and_short_circuit_false_left(self):
        prev = _prev_failed()
        assert evaluate('prev.success && prev.files.contains("x")', prev) is False


# ── Logical OR ──────────────────────────────────────────────────


class TestOr:
    def test_true_or_false(self):
        assert evaluate("true || false", None) is True

    def test_false_or_false(self):
        assert evaluate("false || false", None) is False

    def test_prev_success_or_files_contains(self):
        prev = _prev_failed(files=[_fc("src/deploy.yaml")])
        assert evaluate('prev.success || prev.files.contains("deploy")', prev) is True

    def test_or_short_circuit_true_left(self):
        prev = _prev_success()
        assert evaluate('prev.success || prev.files.contains("nonexistent")', prev) is True


# ── Equality == / != ────────────────────────────────────────────


class TestEquality:
    def test_prev_success_equals_true_when_succeeded(self):
        assert evaluate("prev.success == true", _prev_success()) is True

    def test_prev_success_equals_true_when_failed(self):
        assert evaluate("prev.success == true", _prev_failed()) is False

    def test_prev_success_equals_false_when_no_prev(self):
        assert evaluate("prev.success == false", None) is True

    def test_prev_success_not_equals_true_when_failed(self):
        assert evaluate("prev.success != true", _prev_failed()) is True

    def test_prev_success_not_equals_false_when_succeeded(self):
        assert evaluate("prev.success != false", _prev_success()) is True

    def test_true_equals_true(self):
        assert evaluate("true == true", None) is True

    def test_false_equals_false(self):
        assert evaluate("false == false", None) is True

    def test_true_not_equals_false(self):
        assert evaluate("true != false", None) is True


# ── Parenthesized expressions ───────────────────────────────────


class TestParentheses:
    def test_simple_parens(self):
        assert evaluate("(true)", None) is True

    def test_nested_parens(self):
        assert evaluate("((true))", None) is True

    def test_parens_with_and_or(self):
        # false || (true && false) → false
        assert evaluate("false || (true && false)", None) is False

    def test_parens_change_precedence(self):
        # true || false && false → true (&& binds tighter)
        # (true || false) && false → false (parens override)
        assert evaluate("true || false && false", None) is True
        assert evaluate("(true || false) && false", None) is False

    def test_parens_with_not(self):
        assert evaluate("!(false || false)", None) is True


# ── Whitespace tolerance ────────────────────────────────────────


class TestWhitespace:
    def test_spaces_around_operators(self):
        assert evaluate("  prev.success  ", _prev_success()) is True

    def test_spaces_in_complex_expr(self):
        prev = _prev_success(summary="ok", files=[_fc("src/main.rs")])
        assert evaluate(
            "  prev.success   &&   prev.files.contains(\"src/\")  ",
            prev,
        ) is True

    def test_no_spaces_around_and(self):
        assert evaluate("true&&true", None) is True

    def test_no_spaces_around_or(self):
        assert evaluate("false||true", None) is True

    def test_no_spaces_around_not(self):
        assert evaluate("!false", None) is True

    def test_no_spaces_around_equals(self):
        assert evaluate("true==true", None) is True

    def test_spaces_inside_contains_parens(self):
        prev = _prev_success(summary="hello world")
        assert evaluate('prev.summary.contains( "hello" )', prev) is True


# ── Complex expressions ────────────────────────────────────────


class TestComplexExpressions:
    def test_skip_revise_when_cr_passed(self):
        """Realistic: skip revise when CR succeeded and found nothing wrong."""
        prev = _prev_success(summary="Code review: all clean")
        condition = '!prev.success || prev.summary.contains("error")'
        assert evaluate(condition, prev) is False

    def test_run_revise_when_cr_found_errors(self):
        prev = _prev_failed(summary="CR: found 3 errors in the code")
        condition = '!prev.success || prev.summary.contains("error")'
        assert evaluate(condition, prev) is True

    def test_deploy_only_if_deploy_files_changed(self):
        prev = _prev_success(files=[_fc("deploy/k8s.yaml")])
        condition = 'prev.files.contains("deploy/")'
        assert evaluate(condition, prev) is True

    def test_combined_success_and_file_check(self):
        prev = _prev_success(
            summary="done", files=[_fc("src/main.rs")]
        )
        assert evaluate(
            'prev.success && prev.files.contains("src/") && prev.summary.contains("done")',
            prev,
        ) is True

    def test_negated_contains(self):
        prev = _prev_success(summary="clean")
        assert evaluate('!prev.summary.contains("error")', prev) is True


# ── Step 0 (no prev) ────────────────────────────────────────────


class TestStep0NoPrev:
    def test_prev_success_false(self):
        assert evaluate("prev.success", None) is False

    def test_not_prev_success_true(self):
        assert evaluate("!prev.success", None) is True

    def test_prev_files_contains_false(self):
        assert evaluate('prev.files.contains("x")', None) is False

    def test_prev_summary_contains_false(self):
        assert evaluate('prev.summary.contains("x")', None) is False

    def test_prev_success_equals_false(self):
        assert evaluate("prev.success == false", None) is True

    def test_complex_no_prev(self):
        assert evaluate("!prev.success || true", None) is True


# ── Parse errors → ConditionError ───────────────────────────────


class TestParseErrors:
    def test_unknown_identifier_raises(self):
        with pytest.raises(ConditionError):
            evaluate("foobar", None)

    def test_unmatched_open_paren_raises(self):
        with pytest.raises(ConditionError):
            evaluate("(true", None)

    def test_unmatched_close_paren_raises(self):
        with pytest.raises(ConditionError):
            evaluate("true)", None)

    def test_trailing_operator_raises(self):
        with pytest.raises(ConditionError):
            evaluate("true &&", None)

    def test_leading_operator_raises(self):
        with pytest.raises(ConditionError):
            evaluate("&& true", None)

    def test_dangling_equals_raises(self):
        with pytest.raises(ConditionError):
            evaluate("prev.success ==", None)

    def test_double_and_raises(self):
        with pytest.raises(ConditionError):
            evaluate("true && && true", None)

    def test_contains_without_paren_raises(self):
        with pytest.raises(ConditionError):
            evaluate('prev.files.contains "x"', None)

    def test_contains_without_string_raises(self):
        with pytest.raises(ConditionError):
            evaluate("prev.files.contains(x)", None)

    def test_unterminated_string_in_contains_raises(self):
        with pytest.raises(ConditionError):
            evaluate('prev.files.contains("unterminated)', None)

    def test_unexpected_character_raises(self):
        with pytest.raises(ConditionError):
            evaluate("@#$", None)

    def test_trailing_tokens_raises(self):
        with pytest.raises(ConditionError):
            evaluate("true false", None)

    def test_prev_files_without_contains_raises(self):
        with pytest.raises(ConditionError):
            evaluate("prev.files", None)

    def test_prev_summary_without_contains_raises(self):
        with pytest.raises(ConditionError):
            evaluate("prev.summary", None)
