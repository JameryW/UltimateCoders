"""Tests for the v14 RepoNorm policy extraction and SWE-CC audit (default-off)."""

from __future__ import annotations

import pytest
from ultimate_coders.search.reponorm_compliance import (
    REPONORM_COMPLIANCE_ENV,
    SWE_CC_MACHINE_CHECKABLE_POLICIES,
    ComplianceRuleError,
    NormCategory,
    NormPackage,
    NormRule,
    NormSeverity,
    RepoNormCompliance,
    RepoNormExtractor,
    reponorm_compliance_enabled,
)


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPONORM_COMPLIANCE_ENV, raising=False)
    assert reponorm_compliance_enabled() is False
    monkeypatch.setenv(REPONORM_COMPLIANCE_ENV, "yes")
    assert reponorm_compliance_enabled() is True


def test_policy_classes_match_the_swe_cc_finding() -> None:
    assert SWE_CC_MACHINE_CHECKABLE_POLICIES == 823


def test_extractor_distils_policy_cues_and_categories() -> None:
    package = RepoNormExtractor().extract(
        "demo",
        docs={
            "CONTRIBUTING.md": (
                "All public APIs must keep their signatures stable.\n"
                "Never commit secrets or tokens.\n"
                "Use tabs for formatting.\n"
            )
        },
        history=("Tests must cover every new branch.",),
    )
    assert len(package.rules) == 3
    assert [rule.category for rule in package.rules] == [
        NormCategory.API,
        NormCategory.SECURITY,
        NormCategory.TESTING,
    ]
    assert [rule.severity for rule in package.rules] == [
        NormSeverity.REQUIRED,
        NormSeverity.BLOCKING,
        NormSeverity.REQUIRED,
    ]
    assert package.rules[0].source == "CONTRIBUTING.md"
    assert package.rules[2].source == "git-history"
    assert package.machine_checkable_count() == 0


def test_extractor_ignores_lines_without_policy_cues() -> None:
    package = RepoNormExtractor().extract(
        "demo", docs={"README.md": "Install the package.\nRun the tests.\n"}
    )
    assert package.rules == ()


def test_forbidden_pattern_is_audited() -> None:
    rule = NormRule(
        rule_id="demo:001",
        category=NormCategory.STYLE,
        text="do not use print",
        severity=NormSeverity.BLOCKING,
        forbidden_pattern=r"\bprint\(",
    )
    report = RepoNormCompliance().audit(
        "+ print('debug')\n+ return 1\n", NormPackage("demo", (rule,))
    )
    assert report.compliant is False
    assert report.checked == 1
    assert report.blocking_violations[0].rule_id == "demo:001"
    assert "1:" in report.violations[0].evidence


def test_required_pattern_must_be_present() -> None:
    rule = NormRule(
        rule_id="demo:002",
        category=NormCategory.TESTING,
        text="tests must run under pytest",
        required_pattern=r"pytest",
    )
    report = RepoNormCompliance().audit("+ value = 1\n", NormPackage("demo", (rule,)))
    assert report.compliant is False
    assert "requires" in report.violations[0].message


def test_compliant_patch_passes_both_kinds_of_rule() -> None:
    rules = (
        NormRule(
            rule_id="demo:003",
            category=NormCategory.STYLE,
            text="do not use print",
            forbidden_pattern=r"\bprint\(",
        ),
        NormRule(
            rule_id="demo:004",
            category=NormCategory.TESTING,
            text="use pytest",
            required_pattern=r"pytest",
        ),
    )
    report = RepoNormCompliance().audit(
        "+ pytest tests/python/test_demo.py\n", NormPackage("demo", rules)
    )
    assert report.compliant is True
    assert report.checked == 2
    assert report.violations == ()


def test_rules_without_patterns_are_skipped_not_counted() -> None:
    rule = NormRule(
        rule_id="demo:005",
        category=NormCategory.DOCUMENTATION,
        text="must document public API",
    )
    assert rule.machine_checkable is False
    report = RepoNormCompliance().audit("+ anything\n", NormPackage("demo", (rule,)))
    assert report.checked == 0
    assert report.skipped == 1
    assert report.compliant is True


def test_invalid_patterns_are_rejected() -> None:
    with pytest.raises(ComplianceRuleError):
        NormRule(
            rule_id="bad",
            category=NormCategory.OTHER,
            text="bad regex",
            forbidden_pattern="[",
        )


def test_duplicate_rule_ids_are_rejected() -> None:
    rules = (
        NormRule("same", NormCategory.API, "one"),
        NormRule("same", NormCategory.API, "two"),
    )
    with pytest.raises(ComplianceRuleError):
        NormPackage("demo", rules)


def test_category_filter_returns_a_stable_tuple() -> None:
    rules = (
        NormRule("a", NormCategory.TESTING, "test one"),
        NormRule("b", NormCategory.STYLE, "style one"),
        NormRule("c", NormCategory.TESTING, "test two"),
    )
    package = NormPackage("demo", rules)
    assert [rule.rule_id for rule in package.by_category(NormCategory.TESTING)] == ["a", "c"]
