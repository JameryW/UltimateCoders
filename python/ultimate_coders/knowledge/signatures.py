"""Stable failure signatures for the knowledge layer.

A failure signature is the comparison key behind the "repeated failure
signature rate" metric and behind failure claims. Raw error text contains
volatile tokens (absolute paths, commit SHAs, line numbers, ANSI colour), so
two genuinely equal failures would otherwise look different. Normalization
rewrites only what the worker already observed - it never classifies, infers or
invents a root cause (principle A: facts versus interpretations).

The normalization is deliberately conservative and deterministic:

* ANSI escape sequences are stripped;
* a multi-line traceback is reduced to its final exception line;
* absolute paths, UUIDs, long hex tokens and standalone integers are replaced
  by stable placeholders;
* whitespace is collapsed and the result is bounded to
  :data:`MAX_SIGNATURE_CHARS`.

Callers keep the raw excerpt separately when they need it; this module returns
the comparison signature only.
"""

from __future__ import annotations

import re
from typing import Any

from ultimate_coders.knowledge.model import safe_text

MAX_INPUT_CHARS = 4000
MAX_SIGNATURE_CHARS = 200

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TRACEBACK_MARKER = "Traceback (most recent call last)"
#: Windows drive paths, UNC paths and POSIX absolute paths. The pattern stops
#: at whitespace and at common quoting/bracketing characters so trailing
#: ``:line`` suffixes and punctuation stay visible for the number pass.
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/)[^\s'\"`()\[\]{}]+")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_RE = re.compile(r"\b0[xX][0-9a-fA-F]+\b|\b[0-9a-fA-F]{7,64}\b")
_NUMBER_RE = re.compile(r"\b\d+\b")
_WHITESPACE_RE = re.compile(r"\s+")
#: Final line of a traceback (``ValueError: bad value`` / ``KeyError``).
_EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit)\b")


def _message_core(text: str) -> str:
    """Pick the line that carries the failure message (traceback-aware)."""

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    if _TRACEBACK_MARKER in text:
        for line in reversed(lines):
            candidate = line.lstrip()
            if _EXCEPTION_LINE_RE.match(candidate):
                return candidate
        return lines[-1].lstrip()
    return lines[0].lstrip()


def _replace_volatile_tokens(line: str) -> str:
    """Replace paths, ids and numbers with stable placeholders."""

    line = _PATH_RE.sub("<path>", line)
    line = _UUID_RE.sub("<id>", line)
    line = _HEX_RE.sub("<hex>", line)
    line = _NUMBER_RE.sub("<n>", line)
    return line


def normalize_failure_signature(value: Any) -> str:
    """Return a deterministic, bounded comparison signature for a failure.

    ``value`` may be ``None``, a string or any object (it is converted with
    :func:`safe_text`). Empty or all-volatile input yields ``""`` rather than a
    fabricated generic signature, because "unknown failure" must stay visibly
    unknown in the metric.
    """

    text = safe_text(value, limit=MAX_INPUT_CHARS)
    if not text:
        return ""
    text = _ANSI_ESCAPE_RE.sub("", text)
    core = _message_core(text)
    if not core:
        return ""
    normalized = _replace_volatile_tokens(core)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    if not normalized or normalized in {"<path>", "<id>", "<hex>", "<n>"}:
        return ""
    return safe_text(normalized, limit=MAX_SIGNATURE_CHARS).strip()
