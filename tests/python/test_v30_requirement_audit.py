"""Tests for the acceptance-criteria audit (#89) wired into the worker."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from ultimate_coders.agent.requirement_audit import (
    MAX_REQUIREMENTS,
    RequirementAudit,
    audit_requirements,
    extract_requirements,
    keywords,
    requirement_audit_enabled,
)
from ultimate_coders.agent.types import FileChange
from ultimate_coders.agent.worker import Worker


def _flags(monkeypatch: pytest.MonkeyPatch, *, audit: str = "1", tracker: str = "1") -> None:
    monkeypatch.setenv("UC_REQUIREMENT_AUDIT", audit)
    monkeypatch.setenv("UC_REQUIREMENT_TRACKER", tracker)


class TestFlag:
    def test_disabled_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UC_REQUIREMENT_AUDIT", raising=False)
        monkeypatch.delenv("UC_REQUIREMENT_TRACKER", raising=False)
        assert requirement_audit_enabled() is False

    def test_requires_the_tracker_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch, tracker="")
        assert requirement_audit_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        _flags(monkeypatch, audit=value)
        assert requirement_audit_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "no", "off"])
    def test_falsey_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        _flags(monkeypatch, audit=value)
        assert requirement_audit_enabled() is False


class TestExtractRequirements:
    def test_reads_bullet_and_ordinal_markers(self) -> None:
        description = (
            "Fix the cache layer.\n"
            "- Invalidate the cache on write\n"
            "* Emit metrics for misses\n"
            "1. Keep the public API stable\n"
            "2) Document the new flag\n"
        )
        requirements = extract_requirements(description)
        assert [item.text for item in requirements] == [
            "Invalidate the cache on write",
            "Emit metrics for misses",
            "Keep the public API stable",
            "Document the new flag",
        ]
        assert [item.requirement_id for item in requirements] == ["R1", "R2", "R3", "R4"]

    def test_ignores_prose_and_short_or_duplicate_bullets(self) -> None:
        description = (
            "The handler must stop retrying.\n"
            "- - \n"
            "- Use the new store\n"
            "- use the NEW store\n"
        )
        requirements = extract_requirements(description)
        assert [item.text for item in requirements] == ["Use the new store"]

    def test_strips_markdown_decoration(self) -> None:
        requirements = extract_requirements("- **Return** `None` on miss\n")
        assert requirements[0].text == "Return None on miss"

    def test_caps_the_list(self) -> None:
        description = "\n".join(f"- requirement number {index}" for index in range(20))
        assert len(extract_requirements(description)) == MAX_REQUIREMENTS
        assert len(extract_requirements(description, max_items=3)) == 3

    def test_rejects_non_positive_cap(self) -> None:
        with pytest.raises(ValueError):
            extract_requirements("- x", max_items=0)

    def test_empty_description(self) -> None:
        assert extract_requirements("") == ()
        assert extract_requirements("only prose here") == ()


class TestKeywords:
    def test_drops_short_words_and_stopwords(self) -> None:
        assert keywords("Add the cache invalidation to the store") == (
            "cache",
            "invalidation",
            "store",
        )

    def test_dedupes_and_caps(self) -> None:
        text = "cache cache cache " + " ".join(f"token{index}" for index in range(10))
        found = keywords(text)
        assert found[0] == "cache"
        assert len(found) <= 6
        assert len(set(found)) == len(found)


class TestAuditRequirements:
    def test_returns_none_without_bullets(self) -> None:
        assert audit_requirements("just prose") is None
        assert audit_requirements("") is None

    def test_verifies_when_every_keyword_is_present(self) -> None:
        audit = audit_requirements(
            "- Invalidate the cache on write",
            patch_text="+def invalidate(self): self.cache.clear()",
        )
        assert audit is not None
        assert audit.complete is True
        assert audit.verified == ("R1",)
        assert audit.unverified == ()
        assert audit.coverage == pytest.approx(1.0)

    def test_unverified_when_a_keyword_is_missing(self) -> None:
        audit = audit_requirements(
            "- Invalidate the cache and emit metrics",
            patch_text="+def invalidate(self): self.cache.clear()",
        )
        assert audit is not None
        assert audit.complete is False
        assert audit.verified == ()
        assert audit.coverage == pytest.approx(0.0)
        assert [item.text for item in audit.unverified_requirements] == [
            "Invalidate the cache and emit metrics"
        ]

    def test_partial_coverage(self) -> None:
        audit = audit_requirements(
            "- Invalidate the cache\n- Emit metrics for misses\n",
            patch_text="+def invalidate(self): self.cache.clear()",
        )
        assert audit is not None
        assert len(audit.verified) == 1
        assert len(audit.unverified) == 1
        assert audit.coverage == pytest.approx(0.5)
        assert audit.complete is False

    def test_commands_and_test_output_are_evidence(self) -> None:
        audit = audit_requirements(
            "- Cover the retry path with a regression test\n",
            commands_run=("pytest -q tests/test_retry.py",),
            test_output="tests/test_retry.py::test_regression_path PASSED",
        )
        assert audit is not None
        assert audit.complete is True

    def test_to_dict_is_json_ready(self) -> None:
        audit = audit_requirements("- Invalidate the cache", patch_text="invalidate cache")
        assert isinstance(audit, RequirementAudit)
        encoded = json.dumps(audit.to_dict())
        decoded = json.loads(encoded)
        assert decoded["total"] == 1
        assert decoded["complete"] is True
        assert decoded["verified"] == 1
        assert decoded["requirements"][0]["status"] == "verified"


def _subtask(description: str) -> SimpleNamespace:
    return SimpleNamespace(description=description)


def _output(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "summary": "invalidated the cache",
        "file_changes": [
            FileChange(file_path="src/cache.py", diff="+def invalidate(self): pass")
        ],
        "commands_run": ["pytest -q tests/test_cache.py"],
        "test_output": "1 passed",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestWorkerWiring:
    def test_reports_coverage_for_matching_patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        event = Worker._verify_output_requirements(
            None,  # type: ignore[arg-type]
            _subtask("- Invalidate the cache on write"),
            _output(),  # type: ignore[arg-type]
        )
        assert event is not None
        assert event["phase"] == "requirement audit"
        assert event["requirement_total"] == 1
        assert event["requirement_verified"] == 1
        assert event["requirement_complete"] is True
        assert event["requirement_coverage"] == pytest.approx(1.0)
        assert event["requirement_unverified"] == []

    def test_lists_unverified_bullets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        event = Worker._verify_output_requirements(
            None,  # type: ignore[arg-type]
            _subtask("- Invalidate the cache\n- Emit metrics for misses\n"),
            _output(),  # type: ignore[arg-type]
        )
        assert event is not None
        assert event["requirement_total"] == 2
        assert event["requirement_verified"] == 1
        assert event["requirement_complete"] is False
        assert event["requirement_unverified"] == ["Emit metrics for misses"]

    def test_disabled_flag_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch, audit="0")
        event = Worker._verify_output_requirements(
            None,  # type: ignore[arg-type]
            _subtask("- Invalidate the cache"),
            _output(),  # type: ignore[arg-type]
        )
        assert event is None

    def test_description_without_bullets_emits_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flags(monkeypatch)
        event = Worker._verify_output_requirements(
            None,  # type: ignore[arg-type]
            _subtask("please fix the cache layer"),
            _output(),  # type: ignore[arg-type]
        )
        assert event is None

    def test_empty_description_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        event = Worker._verify_output_requirements(
            None,  # type: ignore[arg-type]
            _subtask("   "),
            _output(),  # type: ignore[arg-type]
        )
        assert event is None

    def test_malformed_output_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flags(monkeypatch)
        broken = SimpleNamespace(summary="x", file_changes=None, commands_run=None)
        event = Worker._verify_output_requirements(
            None,  # type: ignore[arg-type]
            _subtask("- Invalidate the cache"),
            broken,  # type: ignore[arg-type]
        )
        assert event is None or isinstance(event, dict)
