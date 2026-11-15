"""Tests for immutable Mandate envelopes and narrowing-only delegation."""

from __future__ import annotations

import dataclasses

import pytest
from ultimate_coders.architecture.mandate import (
    Mandate,
    MandateError,
    canonical_json,
    digest,
    mandate_envelope_enabled,
    narrow,
)


def _parent(**overrides: object) -> Mandate:
    payload: dict[str, object] = {
        "mandate_id": "parent-1",
        "subject": "task-42",
        "capabilities": ("read", "write", "test"),
        "resources": ("repo", "cache"),
        "max_attempts": 5,
        "budget_tokens": 50000,
        "expires_at": 1000.0,
        "metadata": {"tier": "root"},
    }
    payload.update(overrides)
    return Mandate.from_dict(payload)


def test_digest_is_stable_across_key_order() -> None:
    first = Mandate(
        mandate_id="m1",
        subject="task-1",
        capabilities=("read",),
        metadata={"b": 2, "a": 1},
    )
    second = Mandate(
        mandate_id="m1",
        subject="task-1",
        capabilities=("read",),
        metadata={"a": 1, "b": 2},
    )
    third = Mandate(
        mandate_id="m1",
        subject="task-1",
        capabilities=("read",),
        budget_tokens=10,
        metadata={"a": 1, "b": 2},
    )
    assert first.digest() == second.digest()
    assert first.digest() != third.digest()
    assert first.digest() == digest(first.to_dict())
    assert canonical_json(first.to_dict()) == canonical_json(second.to_dict())
    assert len(first.digest()) == 64


def test_dict_round_trip_preserves_envelope() -> None:
    mandate = _parent()
    row = mandate.to_dict()
    assert row["capabilities"] == ["read", "write", "test"]
    assert row["resources"] == ["repo", "cache"]
    assert row["expires_at"] == 1000.0
    restored = Mandate.from_dict(row)
    assert restored == mandate
    assert restored.digest() == mandate.digest()


def test_from_dict_defaults_optional_fields() -> None:
    mandate = Mandate.from_dict(
        {"mandate_id": "m1", "subject": "task-1", "capabilities": ["read"]}
    )
    assert mandate.resources == ()
    assert mandate.max_attempts == 3
    assert mandate.budget_tokens == 20000
    assert mandate.expires_at == 0.0
    assert mandate.parent_id is None


def test_is_expired_only_when_set() -> None:
    never = Mandate(mandate_id="m1", subject="s", capabilities=("read",))
    assert never.is_expired(1e12) is False

    finite = _parent()
    assert finite.is_expired(999.999) is False
    assert finite.is_expired(1000.0) is True
    assert finite.is_expired(2000.0) is True


def test_allows_capabilities_and_resources() -> None:
    mandate = _parent()
    assert mandate.allows("read") is True
    assert mandate.allows("read", "repo") is True
    assert mandate.allows("read", "elsewhere") is False
    assert mandate.allows("deploy") is False
    assert mandate.allows("") is False


def test_narrow_inherits_parent_and_links_ids() -> None:
    parent = _parent()
    child = narrow(parent, mandate_id="child-1")
    assert child.mandate_id == "child-1"
    assert child.parent_id == "parent-1"
    assert child.subject == parent.subject
    assert child.capabilities == parent.capabilities
    assert child.resources == parent.resources
    assert child.max_attempts == 3
    assert child.budget_tokens == 20000
    assert child.expires_at == parent.expires_at
    assert child.metadata == {"tier": "root"}
    assert child is not parent
    assert parent.mandate_id == "parent-1"  # parent untouched


def test_narrow_result_is_immutable() -> None:
    child = narrow(_parent(), mandate_id="child-1", max_attempts=2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        child.max_attempts = 9  # type: ignore[misc]


def test_narrow_rejects_capability_widening() -> None:
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="child-1", capabilities=("read", "deploy"))


def test_narrow_rejects_resource_widening() -> None:
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="child-1", resources=("repo", "prod"))


def test_narrow_rejects_budget_growth() -> None:
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="child-1", max_attempts=6)
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="child-1", budget_tokens=50001)


def test_narrow_accepts_shrinking_budgets() -> None:
    child = narrow(
        _parent(),
        mandate_id="child-1",
        capabilities=("read",),
        resources=("repo",),
        max_attempts=1,
        budget_tokens=100,
        expires_at=900.0,
    )
    assert child.capabilities == ("read",)
    assert child.max_attempts == 1
    assert child.budget_tokens == 100
    assert child.expires_at == 900.0


def test_narrow_rejects_expiry_beyond_finite_parent() -> None:
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="child-1", expires_at=1000.5)


def test_narrow_inherits_parent_expiry_when_child_has_none() -> None:
    child = narrow(_parent(), mandate_id="child-1")
    assert child.expires_at == 1000.0
    endless_parent = _parent(expires_at=0.0)
    assert narrow(endless_parent, mandate_id="child-1").expires_at == 0.0
    assert narrow(endless_parent, mandate_id="child-1", expires_at=42.0).expires_at == 42.0


def test_narrow_rejects_subject_change() -> None:
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="child-1", subject="task-99")
    child = narrow(_parent(), mandate_id="child-1", subject="task-42")
    assert child.subject == "task-42"


def test_narrow_rejects_invalid_arguments() -> None:
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="")
    with pytest.raises(MandateError):
        narrow("not-a-mandate", mandate_id="child-1")  # type: ignore[arg-type]
    with pytest.raises(MandateError):
        narrow(_parent(), mandate_id="child-1", expires_at=-1.0)


def test_narrow_metadata_is_independent_of_parent() -> None:
    parent = _parent()
    child = narrow(parent, mandate_id="child-1", metadata={"tier": "child"})
    assert child.metadata == {"tier": "child"}
    assert parent.metadata == {"tier": "root"}
    inherited = narrow(parent, mandate_id="child-2")
    assert inherited.metadata == {"tier": "root"}
    assert inherited.metadata is not parent.metadata


def test_nested_narrowing_cannot_widen_grandparent_limits() -> None:
    parent = _parent()
    child = narrow(parent, mandate_id="child-1", capabilities=("read",))
    with pytest.raises(MandateError):
        narrow(child, mandate_id="grandchild-1", capabilities=("read", "write"))
    grandchild = narrow(child, mandate_id="grandchild-1")
    assert grandchild.capabilities == ("read",)
    assert grandchild.parent_id == "child-1"
    assert grandchild.subject == "task-42"


def test_mandate_error_is_value_error() -> None:
    assert issubclass(MandateError, ValueError)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("on", True),
        ("0", False),
        ("no", False),
    ],
)
def test_mandate_envelope_enabled_reads_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("UC_MANDATE_ENVELOPE", value)
    assert mandate_envelope_enabled() is expected


def test_mandate_envelope_enabled_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UC_MANDATE_ENVELOPE", raising=False)
    assert mandate_envelope_enabled() is False
