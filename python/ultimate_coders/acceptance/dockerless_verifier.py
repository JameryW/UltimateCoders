"""Dockerless agentic verifier: a fast, deterministic Oracle fallback.

The Dockerless verifier (#18) is a hot spare for the container-backed Oracle.
When Docker is unavailable -- or too slow for a first-pass decision -- this
verifier inspects the patch against a supplied repository index and call-chain
record and scores three evidence-backed checks:

* signature compatibility (changed signatures still match callers),
* lock release path (acquired locks are released on every path),
* boundary conditions (known edge cases from the index are addressed).

It is *not* a test runner and never executes patch code.  The repo index and
call chain are deterministic records produced by earlier analysis, so the same
inputs always produce the same score.  Score is 0-100 and passes at a
configurable threshold (default 70).

Everything is opt-in and default-off (``UC_DOCKERLESS_VERIFIER``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "DEFAULT_PASS_THRESHOLD",
    "DOCKERLESS_VERIFIER_ENV",
    "DockerlessAgenticVerifier",
    "DockerlessReport",
    "PatchSignal",
    "dockerless_verifier_enabled",
]

DOCKERLESS_VERIFIER_ENV = "UC_DOCKERLESS_VERIFIER"
DEFAULT_PASS_THRESHOLD = 70
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def dockerless_verifier_enabled() -> bool:
    """Feature flag: enabled only when ``UC_DOCKERLESS_VERIFIER`` is truthy."""

    return os.environ.get(DOCKERLESS_VERIFIER_ENV, "").strip().lower() in _TRUTHY


class PatchSignal(str, Enum):
    """The deterministic checks the Dockerless verifier can report."""

    SIGNATURE_COMPATIBILITY = "signature_compatibility"
    LOCK_RELEASE = "lock_release"
    BOUNDARY_CONDITIONS = "boundary_conditions"


_SIGNAL_ORDER: tuple[PatchSignal, ...] = (
    PatchSignal.SIGNATURE_COMPATIBILITY,
    PatchSignal.LOCK_RELEASE,
    PatchSignal.BOUNDARY_CONDITIONS,
)


@dataclass(frozen=True)
class DockerlessReport:
    """Outcome of one Dockerless verification pass."""

    score: int
    passed: bool
    missing_evidence: tuple[str, ...] = ()
    checks: dict[str, bool] = field(default_factory=dict)
    threshold: int = DEFAULT_PASS_THRESHOLD

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", max(0, min(100, int(self.score))))
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "missing_evidence", tuple(self.missing_evidence))
        object.__setattr__(self, "checks", dict(self.checks))
        object.__setattr__(self, "threshold", int(self.threshold))

    @property
    def failed_signals(self) -> tuple[PatchSignal, ...]:
        """Return failed checks in deterministic declaration order."""

        failed: list[PatchSignal] = []
        for signal in _SIGNAL_ORDER:
            if self.checks.get(signal.value) is False:
                failed.append(signal)
        return tuple(failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "passed": self.passed,
            "missing_evidence": list(self.missing_evidence),
            "checks": dict(self.checks),
            "threshold": self.threshold,
        }

    def summary(self) -> str:
        """One-line human-readable summary."""

        state = "PASS" if self.passed else "FAIL"
        details = "; ".join(self.missing_evidence) if self.missing_evidence else "all checks passed"
        return f"Dockerless {state} score={self.score}/{self.threshold}: {details}"


@dataclass(frozen=True)
class _FunctionChange:
    name: str
    old_params: tuple[str, ...] | None
    new_params: tuple[str, ...] | None


class DockerlessAgenticVerifier:
    """Score a patch without Docker using deterministic repository records."""

    def __init__(
        self,
        threshold: int = DEFAULT_PASS_THRESHOLD,
        *,
        pass_threshold: int | None = None,
    ) -> None:
        if pass_threshold is not None:
            threshold = pass_threshold
        if not 0 <= int(threshold) <= 100:
            raise ValueError("threshold must be between 0 and 100")
        self.threshold = int(threshold)

    def verify(
        self,
        patch: str,
        *,
        repo_index: Any,
        call_chain: Any = None,
    ) -> DockerlessReport:
        """Run all checks and return a scored :class:`DockerlessReport`."""

        patch_text = str(patch or "")
        checks: dict[str, bool] = {}
        missing: list[str] = []

        signature_ok, signature_missing = self._check_signatures(patch_text, repo_index, call_chain)
        checks[PatchSignal.SIGNATURE_COMPATIBILITY.value] = signature_ok
        missing.extend(signature_missing)

        lock_ok, lock_missing = self._check_lock_release(patch_text)
        checks[PatchSignal.LOCK_RELEASE.value] = lock_ok
        missing.extend(lock_missing)

        boundary_ok, boundary_missing = self._check_boundaries(patch_text, repo_index, call_chain)
        checks[PatchSignal.BOUNDARY_CONDITIONS.value] = boundary_ok
        missing.extend(boundary_missing)

        passed_count = sum(1 for value in checks.values() if value)
        score = round(100 * passed_count / len(checks)) if checks else 0
        passed = score >= self.threshold
        return DockerlessReport(
            score=score,
            passed=passed,
            missing_evidence=tuple(missing),
            checks=checks,
            threshold=self.threshold,
        )

    # ── signature compatibility ─────────────────────────────────

    def _check_signatures(
        self,
        patch: str,
        repo_index: Any,
        call_chain: Any,
    ) -> tuple[bool, list[str]]:
        changes = _function_changes(patch)
        index = _index_signatures(repo_index)
        calls = _calls_from_chain(call_chain)
        missing: list[str] = []
        for change in changes:
            if change.old_params is not None and change.new_params is not None:
                if not _compatible_signatures(change.old_params, change.new_params):
                    old_sig = _render_params(change.old_params)
                    new_sig = _render_params(change.new_params)
                    missing.append(
                        f"signature mismatch: {change.name} changes {old_sig} -> {new_sig}"
                    )
                    continue
            indexed = index.get(change.name)
            if change.new_params is not None and indexed is not None:
                if not _compatible_signatures(indexed, change.new_params):
                    missing.append(
                        f"signature mismatch: {change.name} no longer matches repo index "
                        f"{_render_params(indexed)}"
                    )
                    continue
            if change.new_params is not None:
                required = _required_params(change.new_params)
                for call in calls:
                    if call["name"] != change.name:
                        continue
                    if not call["has_kwargs"] and call["positional"] < required:
                        missing.append(
                            f"signature mismatch: caller of {change.name} passes "
                            f"{call['positional']} arg(s), signature requires {required}"
                        )
                        break
        return (not missing), missing

    # ── lock release ────────────────────────────────────────────

    def _check_lock_release(self, patch: str) -> tuple[bool, list[str]]:
        added = _added_lines(patch)
        lock_acquire_re = re.compile(
            r"(?P<target>[A-Za-z_][A-Za-z0-9_.]*(?:_?lock|mutex|semaphore))\s*\.acquire\s*\(",
            re.IGNORECASE,
        )
        lock_release_re = re.compile(
            r"(?P<target>[A-Za-z_][A-Za-z0-9_.]*(?:_?lock|mutex|semaphore))\s*\.release\s*\(",
            re.IGNORECASE,
        )
        lock_lines = [
            line
            for line in added
            if re.search(r"(?:_?lock|mutex|semaphore)\b", line, re.IGNORECASE)
        ]
        acquires = [
            line for line in lock_lines if re.search(r"\.acquire\s*\(|\bacquire\s*\(", line)
        ]
        if not acquires:
            return True, []

        named_acquires: set[str] = set()
        for line in acquires:
            for m in lock_acquire_re.finditer(line):
                named_acquires.add(m.group("target"))
        named_releases: set[str] = set()
        for line in added:
            for m in lock_release_re.finditer(line):
                named_releases.add(m.group("target"))
        if named_acquires:
            missing_locks = sorted(named_acquires - named_releases)
            if not missing_locks:
                return True, []
            return False, [
                "lock release path missing: "
                f"{', '.join(missing_locks)} acquired without matching release()"
            ]

        releases = [line for line in added if re.search(r"\.release\s*\(|\brelease\s*\(", line)]
        context_managed = [
            line
            for line in lock_lines
            if re.search(r"\bwith\b", line) and not re.search(r"\bacquire\s*\(", line)
        ]
        if releases or context_managed:
            return True, []
        return False, [
            "lock release path missing: a lock is acquired but no release() or context manager "
            "appears in the added code"
        ]

    # ── boundary conditions ─────────────────────────────────────

    def _check_boundaries(
        self,
        patch: str,
        repo_index: Any,
        call_chain: Any,
    ) -> tuple[bool, list[str]]:
        expectations = _boundary_expectations(repo_index, call_chain)
        if not expectations:
            return True, []
        added_lines = _added_lines(patch)
        covered_text = "\n".join(added_lines) if added_lines else ""
        missing = [
            f"boundary condition unaddressed: {expectation}"
            for expectation in expectations
            if not _covers_boundary(covered_text, expectation)
        ]
        return (not missing), missing


# ── patch parsing ───────────────────────────────────────────────


def _added_lines(patch: str) -> list[str]:
    lines: list[str] = []
    for raw_line in str(patch or "").splitlines():
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            lines.append(raw_line[1:])
    return lines


def _function_changes(patch: str) -> list[_FunctionChange]:
    removed: dict[str, tuple[str, ...]] = {}
    added: dict[str, tuple[str, ...]] = {}
    order: list[str] = []
    for raw_line in str(patch or "").splitlines():
        prefix = raw_line[:1]
        if prefix not in {"+", "-"} or raw_line.startswith(prefix * 3):
            continue
        match = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)", raw_line[1:])
        if not match:
            continue
        name = match.group(1)
        params = _split_params(match.group(2))
        target = added if prefix == "+" else removed
        target[name] = params
        if name not in order:
            order.append(name)
    changes: list[_FunctionChange] = []
    for name in order:
        changes.append(_FunctionChange(name, removed.get(name), added.get(name)))
    return changes


def _split_params(text: str) -> tuple[str, ...]:
    raw = str(text or "").strip()
    if not raw:
        return ()
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in raw:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                parts.append(item)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return tuple(parts)


def _param_name(param: str) -> str:
    head = str(param).split("=", 1)[0]
    head = head.split(":", 1)[0].strip()
    return head.lstrip("*")


def _is_optional_param(param: str) -> bool:
    cleaned = str(param).strip()
    return cleaned in {"*", "/"} or cleaned.startswith("*") or "=" in cleaned


def _required_params(params: Sequence[str]) -> int:
    count = 0
    for param in params:
        cleaned = str(param).strip()
        name = _param_name(cleaned)
        if name in {"self", "cls", "/"} or cleaned in {"/", "*"}:
            continue
        if cleaned.startswith("*") or "=" in cleaned:
            continue
        count += 1
    return count


def _compatible_signatures(old: Sequence[str], new: Sequence[str]) -> bool:
    old_params = [
        p
        for p in old
        if _param_name(str(p)) not in {"self", "cls"} and str(p).strip() not in {"/", "*"}
    ]
    new_params = [
        p
        for p in new
        if _param_name(str(p)) not in {"self", "cls"} and str(p).strip() not in {"/", "*"}
    ]
    if len(new_params) < len(old_params):
        return False
    for old_param, new_param in zip(old_params, new_params):
        if _param_name(str(old_param)) != _param_name(str(new_param)):
            return False
        if _is_optional_param(str(old_param)) and not _is_optional_param(str(new_param)):
            return False
    for extra in new_params[len(old_params) :]:
        if not _is_optional_param(str(extra)):
            return False
    return True


def _render_params(params: Sequence[str]) -> str:
    return "(" + ", ".join(str(param) for param in params) + ")"


def _index_signatures(repo_index: Any) -> dict[str, tuple[str, ...]]:
    signatures: dict[str, tuple[str, ...]] = {}
    if isinstance(repo_index, Mapping):
        for key, value in repo_index.items():
            if str(key) in {"boundary_conditions", "boundaries", "tests", "files"}:
                continue
            found = _signature_from_value(key, value)
            if found is not None:
                signatures[found[0]] = found[1]
            elif isinstance(value, Mapping):
                for name, nested in value.items():
                    found_nested = _signature_from_value(name, nested)
                    if found_nested is not None:
                        signatures[found_nested[0]] = found_nested[1]
    return signatures


def _signature_from_value(name: Any, value: Any) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(name, str):
        return None
    if isinstance(value, str):
        match = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)", value)
        if match:
            return match.group(1), _split_params(match.group(2))
        if "(" in value and value.rstrip().endswith(")"):
            inner = value[value.find("(") + 1 : value.rfind(")")]
            return name, _split_params(inner)
        return None
    if isinstance(value, Mapping):
        for key in ("signature", "params", "parameters", "args"):
            if key in value:
                candidate = value[key]
                if isinstance(candidate, str):
                    match = re.match(
                        r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)",
                        candidate,
                    )
                    if match:
                        return match.group(1), _split_params(match.group(2))
                    if "(" in candidate and candidate.rstrip().endswith(")"):
                        inner = candidate[candidate.find("(") + 1 : candidate.rfind(")")]
                        return name, _split_params(inner)
                    return name, _split_params(candidate)
                if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                    return name, tuple(str(item) for item in candidate)
    return None


def _calls_from_chain(call_chain: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if call_chain is None:
        return calls
    if isinstance(call_chain, Mapping):
        raw_items: list[Any]
        if "calls" in call_chain and isinstance(call_chain["calls"], Sequence):
            raw_items = list(call_chain["calls"])
        else:
            raw_items = [call_chain]
    elif isinstance(call_chain, Sequence) and not isinstance(call_chain, (str, bytes)):
        raw_items = list(call_chain)
    else:
        raw_items = [call_chain]
    for item in raw_items:
        if isinstance(item, str):
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\((.*)\)", item)
            if not match:
                continue
            name = match.group(1).split(".")[-1]
            args = _split_params(match.group(2))
            calls.append(
                {
                    "name": name,
                    "positional": len([arg for arg in args if "=" not in arg]),
                    "has_kwargs": any("=" in arg for arg in args),
                }
            )
        elif isinstance(item, Mapping):
            name = str(item.get("name") or item.get("symbol") or "").split(".")[-1]
            if not name:
                continue
            args = item.get("args", item.get("positional", ()))
            kwargs = item.get("kwargs", {})
            if isinstance(args, Sequence) and not isinstance(args, str):
                positional = len(args)
            else:
                positional = int(args or 0)
            calls.append(
                {
                    "name": name,
                    "positional": positional,
                    "has_kwargs": bool(kwargs),
                }
            )
    return calls


def _boundary_expectations(repo_index: Any, call_chain: Any) -> list[str]:
    expectations: list[str] = []

    def collect(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if value.strip():
                expectations.append(value.strip())
            return
        if isinstance(value, Mapping):
            for key in ("condition", "description", "name", "boundary", "edge_case"):
                if isinstance(value.get(key), str):
                    expectations.append(value[key])
                    return
            for nested in value.values():
                collect(nested)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                collect(item)

    if isinstance(repo_index, Mapping):
        for key in ("boundary_conditions", "boundaries", "edge_cases"):
            if key in repo_index:
                collect(repo_index[key])
        for value in repo_index.values():
            if isinstance(value, Mapping):
                for key in ("boundary_conditions", "boundaries", "edge_cases"):
                    if key in value:
                        collect(value[key])
    if call_chain is not None:
        if isinstance(call_chain, Sequence) and not isinstance(call_chain, (str, bytes)):
            items = list(call_chain)
        else:
            items = [call_chain]
        for item in items:
            if isinstance(item, Mapping):
                for key in ("boundary", "boundary_condition", "edge_case"):
                    if key in item:
                        collect(item[key])
    deduped: list[str] = []
    seen: set[str] = set()
    for expectation in expectations:
        key = " ".join(expectation.lower().split())
        if key and key not in seen:
            seen.add(key)
            deduped.append(expectation)
    return deduped


_BOUNDARY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "empty": ("empty", "not ", "len(", "== []", "== {}", '== ""'),
    "none": ("none", "is none", "is not none", "optional"),
    "null": ("none", "null", "optional"),
    "zero": ("== 0", "<= 0", "zero", "not "),
    "negative": ("< 0", "negative", "max(", "abs("),
    "overflow": ("max", "min", "overflow", "bounds"),
    "timeout": ("timeout", "deadline", "retry"),
    "missing": ("missing", "none", "not in", "get("),
    "invalid": ("invalid", "raise", "error", "validate"),
    "large": ("max", "limit", "chunk", "size"),
    "unicode": ("encode", "decode", "unicode", "utf"),
}


def _covers_boundary(patch: str, expectation: str) -> bool:
    lowered = str(patch or "").lower()
    words = re.findall(r"[a-z]+", str(expectation).lower())
    if not words:
        return True
    checked = False
    for word in words:
        synonyms = _BOUNDARY_SYNONYMS.get(word)
        if synonyms is None:
            continue
        checked = True
        if any(synonym.lower() in lowered for synonym in synonyms):
            return True
    if not checked:
        significant = [word for word in words if len(word) > 3]
        return any(word in lowered for word in significant)
    return False
