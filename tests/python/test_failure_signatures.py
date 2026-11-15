"""Tests for normalized failure signatures (knowledge/signatures.py)."""

from __future__ import annotations

from ultimate_coders.knowledge.signatures import (
    MAX_SIGNATURE_CHARS,
    normalize_failure_signature,
)


def test_empty_and_volatile_only_inputs_stay_unknown() -> None:
    assert normalize_failure_signature(None) == ""
    assert normalize_failure_signature("") == ""
    assert normalize_failure_signature("   \n\t ") == ""
    # A bare number carries no comparable semantics.
    assert normalize_failure_signature("404") == ""


def test_normalization_is_deterministic() -> None:
    value = "build failed: cannot link target"
    assert normalize_failure_signature(value) == normalize_failure_signature(value)


def test_absolute_paths_and_line_numbers_are_collapsed() -> None:
    first = normalize_failure_signature(r"Error: cannot open C:\tmp\a.py:12")
    second = normalize_failure_signature(r"Error: cannot open C:\tmp\b.py:99")
    assert first == second == "Error: cannot open <path>"
    posix = normalize_failure_signature("/home/user/project/app.py:7: boom")
    assert "/home/user/project/app.py" not in posix
    assert "<path>" in posix


def test_hex_tokens_shas_and_uuids_are_collapsed() -> None:
    first = normalize_failure_signature("ValueError: bad token 3f2a9c1b")
    second = normalize_failure_signature("ValueError: bad token aa11bb22")
    assert first == second == "ValueError: bad token <hex>"
    uuid_first = normalize_failure_signature(
        "KeyError: session 123e4567-e89b-12d3-a456-426614174000 expired"
    )
    uuid_second = normalize_failure_signature(
        "KeyError: session 223e4567-e89b-12d3-a456-426614174999 expired"
    )
    assert uuid_first == uuid_second
    assert "<id>" in uuid_first
    assert normalize_failure_signature("oom at 0x7f3a2b1c") == "oom at <hex>"


def test_traceback_is_reduced_to_the_final_exception_line() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/x.py", line 3, in <module>\n'
        "    f()\n"
        "ValueError: bad value\n"
    )
    assert normalize_failure_signature(text) == "ValueError: bad value"


def test_ansi_colour_is_stripped() -> None:
    text = "\x1b[31mError: boom\x1b[0m"
    assert normalize_failure_signature(text) == "Error: boom"


def test_different_failures_keep_different_signatures() -> None:
    first = normalize_failure_signature("ValueError: unexpected value")
    second = normalize_failure_signature("KeyError: missing key")
    assert first != second


def test_multiline_non_traceback_uses_the_first_line() -> None:
    text = "AssertionError: expected 3 got 4\nat step two\nat step three"
    signature = normalize_failure_signature(text)
    assert signature == "AssertionError: expected <n> got <n>"


def test_signature_is_bounded() -> None:
    text = "Error: " + "x" * 5000
    signature = normalize_failure_signature(text)
    assert len(signature) <= MAX_SIGNATURE_CHARS
    assert signature.endswith("...")
