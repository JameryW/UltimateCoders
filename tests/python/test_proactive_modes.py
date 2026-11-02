"""Proactive mode parsing is fail-closed and mirrors the gateway enum (PR06).

The acceptance pinned here: the default is ``off`` and it evaluates nothing; an
unknown ``UC_PROACTIVE_MODE`` is ``off`` with a warning (never a valid mode, so
a typo cannot grant autonomy); and the capability matrix matches the gateway's
``ProactiveMode`` (``evaluates`` / ``allows_proposal`` / ``allows_execution``).
"""

from __future__ import annotations

import logging

import pytest
from ultimate_coders.proactive.modes import (
    DEFAULT_PROACTIVE_MODE,
    ENV_PROACTIVE_MODE,
    PROACTIVE_MODES,
    ProactiveMode,
    parse_proactive_mode,
    proactive_mode_from_env,
)

_MODES_LOGGER = "ultimate_coders.proactive.modes"


def test_default_is_off_and_evaluates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PROACTIVE_MODE, raising=False)
    mode = proactive_mode_from_env()
    assert DEFAULT_PROACTIVE_MODE == "off"
    assert mode is ProactiveMode.OFF
    assert mode.evaluates() is False
    assert mode.allows_proposal() is False
    assert mode.allows_execution() is False


def test_unset_env_is_read_from_the_given_mapping() -> None:
    assert proactive_mode_from_env({}) is ProactiveMode.OFF
    assert proactive_mode_from_env({ENV_PROACTIVE_MODE: "observe"}) is ProactiveMode.OBSERVE


def test_empty_env_value_is_off_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ENV_PROACTIVE_MODE, "")
    with caplog.at_level(logging.WARNING, logger=_MODES_LOGGER):
        assert proactive_mode_from_env() is ProactiveMode.OFF
    assert caplog.records == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("off", ProactiveMode.OFF),
        ("disabled", ProactiveMode.OFF),
        ("", ProactiveMode.OFF),
        ("observe", ProactiveMode.OBSERVE),
        ("  PROPOSE  ", ProactiveMode.PROPOSE),
        ("Execute", ProactiveMode.EXECUTE),
        (ProactiveMode.OBSERVE, ProactiveMode.OBSERVE),
    ],
)
def test_known_tokens_parse(raw: object, expected: ProactiveMode) -> None:
    assert parse_proactive_mode(raw) is expected


@pytest.mark.parametrize(
    "raw",
    ["proposee", "on", "true", "1", "exec", "observe!", "auto", None, 3, ["off"]],
)
def test_unknown_tokens_do_not_parse(raw: object) -> None:
    assert parse_proactive_mode(raw) is None


def test_unknown_env_value_falls_back_to_off_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ENV_PROACTIVE_MODE, "proposee")
    with caplog.at_level(logging.WARNING, logger=_MODES_LOGGER):
        mode = proactive_mode_from_env()
    assert mode is ProactiveMode.OFF
    assert mode.evaluates() is False
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings, "an unknown mode must be reported, not silently dropped"
    assert any("proposee" in record.getMessage() for record in warnings)


@pytest.mark.parametrize(
    "mode, evaluates, proposes, executes",
    [
        (ProactiveMode.OFF, False, False, False),
        (ProactiveMode.OBSERVE, True, False, False),
        (ProactiveMode.PROPOSE, True, True, False),
        (ProactiveMode.EXECUTE, True, True, True),
    ],
)
def test_mode_capability_matrix(
    mode: ProactiveMode, evaluates: bool, proposes: bool, executes: bool
) -> None:
    assert mode.evaluates() is evaluates
    assert mode.allows_proposal() is proposes
    assert mode.allows_execution() is executes


def test_wire_tokens_match_the_gateway_enum() -> None:
    assert [mode.value for mode in PROACTIVE_MODES] == [
        "off",
        "observe",
        "propose",
        "execute",
    ]
    assert str(ProactiveMode.PROPOSE) == "propose"
    assert ProactiveMode("execute") is ProactiveMode.EXECUTE
