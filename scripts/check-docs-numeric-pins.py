#!/usr/bin/env python3
"""Hard-fail docs↔numeric-pin reconciliation (D10 / T21).

Reads ``docs/numeric-pins.yaml`` and asserts every pin's ``must_contain``
substring still appears in each listed surface. Separately scans
``uplift_ban_surfaces`` for percentage tokens; any ``%`` not covered by an
``uplift_allowed`` entry with ``eval_role: report`` fails closed.

This guard never starts a live stack. Refreshing pins from a remeasure is
ticket 22 (``scripts/verify-sse-latency.py``), not this checker.

Exit code IS the verdict (unlike advisory ``check-spec-refs.py --audit``).

Judgments (separate messages for ablation):

  1. manifest-load     — file exists, parses, and lists at least one pin
  2. surface-missing   — a listed path is absent on disk
  3. pin-drift         — must_contain not found in the surface text
  4. uplift-undocumented — ``%`` on a ban surface is not allowlisted
  5. uplift-provenance — an allowlist entry lacks eval_role=report
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = REPO / "docs" / "numeric-pins.yaml"
MIN_PINS = 1

# Percentage token on README-facing prose. Captures the numeric stem so an
# allowlist can name either ``40%`` or ``40``.
PCT_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*%")


def load_manifest(path: pathlib.Path) -> tuple[dict | None, list[str]]:
    """Parse the manifest, or return (None, problems)."""
    problems: list[str] = []
    if not path.is_file():
        problems.append(f"manifest-load: missing {path.relative_to(REPO)}")
        return None, problems
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        problems.append(f"manifest-load: YAML error: {exc}")
        return None, problems
    if not isinstance(data, dict):
        problems.append("manifest-load: root must be a mapping")
        return None, problems
    pins = data.get("pins")
    if not isinstance(pins, list) or len(pins) < MIN_PINS:
        problems.append(
            f"manifest-load: expected >= {MIN_PINS} pin(s), got "
            f"{0 if not isinstance(pins, list) else len(pins)}"
        )
        return None, problems
    return data, problems


def check_pins(data: dict, problems: list[str]) -> int:
    """Validate each pin surface; return how many pin assertions ran."""
    checked = 0
    for pin in data["pins"]:
        if not isinstance(pin, dict):
            problems.append("manifest-load: pin entry is not a mapping")
            continue
        pin_id = pin.get("id") or "<unnamed>"
        surfaces = pin.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces:
            problems.append(f"manifest-load: pin {pin_id!r} has no surfaces")
            continue
        for surface in surfaces:
            if not isinstance(surface, dict):
                problems.append(
                    f"manifest-load: pin {pin_id!r} surface is not a mapping"
                )
                continue
            rel = surface.get("path")
            needle = surface.get("must_contain")
            if not isinstance(rel, str) or not isinstance(needle, str):
                problems.append(
                    f"manifest-load: pin {pin_id!r} surface needs path + "
                    "must_contain strings"
                )
                continue
            path = REPO / rel
            if not path.is_file():
                problems.append(f"surface-missing: {rel} (pin {pin_id})")
                continue
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            checked += 1
            if needle not in text:
                problems.append(
                    f"pin-drift: {rel} missing {needle!r} (pin {pin_id})"
                )
    return checked


def allowed_pct_tokens(data: dict, problems: list[str]) -> set[str]:
    """Tokens that may appear as percentages on ban surfaces."""
    allowed: set[str] = set()
    raw = data.get("uplift_allowed") or []
    if not isinstance(raw, list):
        problems.append("manifest-load: uplift_allowed must be a list")
        return allowed
    for entry in raw:
        if not isinstance(entry, dict):
            problems.append("manifest-load: uplift_allowed entry not a mapping")
            continue
        role = entry.get("eval_role")
        if role != "report":
            label = entry.get("id") or entry.get("token") or entry
            problems.append(
                f"uplift-provenance: {label!r} requires eval_role=report "
                f"(got {role!r})"
            )
            continue
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            problems.append(
                "manifest-load: uplift_allowed entry needs a non-empty token"
            )
            continue
        # Accept ``40%`` or ``40`` in the allowlist.
        stem = token.rstrip().rstrip("%").strip()
        if stem:
            allowed.add(stem)
    return allowed


def check_uplift_ban(data: dict, problems: list[str]) -> int:
    """Scan ban surfaces for undocumented ``%``; return match count seen."""
    surfaces = data.get("uplift_ban_surfaces") or []
    if not isinstance(surfaces, list):
        problems.append("manifest-load: uplift_ban_surfaces must be a list")
        return 0
    allowed = allowed_pct_tokens(data, problems)
    seen = 0
    for rel in surfaces:
        if not isinstance(rel, str):
            problems.append("manifest-load: uplift_ban_surfaces entry not a string")
            continue
        path = REPO / rel
        if not path.is_file():
            problems.append(f"surface-missing: {rel} (uplift ban surface)")
            continue
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        for match in PCT_RE.finditer(text):
            seen += 1
            stem = match.group(1)
            if stem not in allowed:
                problems.append(
                    f"uplift-undocumented: {rel} has {match.group(0)!r} "
                    "without report-role allowlist entry"
                )
    return seen


def main() -> int:
    problems: list[str] = []
    data, load_problems = load_manifest(MANIFEST)
    problems.extend(load_problems)
    checked = 0
    if data is not None:
        checked = check_pins(data, problems)
        check_uplift_ban(data, problems)

    print(
        f"docs numeric pins: {checked} pin assertion(s) checked "
        f"from {MANIFEST.relative_to(REPO)}"
    )
    if problems:
        print("FAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("docs-numeric-pins check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
