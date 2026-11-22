"""RETRACE: deterministic three-way verification of a repair.

RETRACE (#15) checks that a patch actually matches the reasoning that
produced it.  Three views are compared:

1. **Forward** -- :meth:`RetraceVerifier.forward` turns an issue and its
   trajectory into explicit rationale clauses (what the agent claims needed
   fixing).
2. **Backward** -- :meth:`RetraceVerifier.backward` reconstructs what a patch
   appears to do from the patch and an AST index.  This view is *blind*: it
   never receives, reads, or embeds the issue text.
3. **Arbitration** -- :meth:`RetraceVerifier.arbitrate` matches the two claim
   sets, reports missing or contradictory requirements, and emits one
   targeted repair instruction when they disagree.

Everything is deterministic string and structure processing -- no LLM, no
network, no hidden state.  The feature is opt-in and default-off
(``UC_RETRACE_VERIFIER``).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RETRACE_ENABLED_ENV",
    "STATUS_AGREE",
    "STATUS_CONTRADICTORY",
    "STATUS_MISMATCH",
    "BlindReconstruction",
    "RetraceVerdict",
    "RetraceVerifier",
    "TraceRationale",
    "retrace_enabled",
]

RETRACE_ENABLED_ENV = "UC_RETRACE_VERIFIER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

STATUS_AGREE = "agree"
STATUS_MISMATCH = "mismatch"
STATUS_CONTRADICTORY = "contradictory"

_REQUIREMENT_WORDS = frozenset(
    {
        "must",
        "should",
        "shall",
        "need",
        "needs",
        "required",
        "require",
        "requires",
        "ensure",
        "expected",
        "expect",
        "support",
        "return",
        "raise",
        "preserve",
        "avoid",
        "add",
        "fix",
        "release",
        "handle",
    }
)
_NEGATIONS = frozenset({"not", "never", "no", "without", "cannot", "can't", "doesn't", "don't"})
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "when",
        "where",
        "which",
        "while",
        "should",
        "must",
        "shall",
        "need",
        "needs",
        "can",
        "could",
        "would",
        "may",
        "might",
        "also",
        "then",
        "than",
        "there",
        "their",
        "they",
        "them",
        "our",
        "your",
        "you",
        "patch",
        "adds",
        "added",
        "define",
        "defines",
        "defined",
        "self",
        "file",
        "files",
    }
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_PATCH_PATH_RE = re.compile(r"^(?:\+\+\+|---)\s+(.+?)\s*$")
_MAX_CLAUSE_CHARS = 240
_MATCH_THRESHOLD = 0.6


def retrace_enabled() -> bool:
    """Feature flag: RETRACE is enabled only when ``UC_RETRACE_VERIFIER`` is truthy."""

    return os.environ.get(RETRACE_ENABLED_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TraceRationale:
    """Forward rationale extracted from an issue and its trajectory."""

    clauses: tuple[str, ...] = ()
    issue_summary: str = ""
    trajectory_steps: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clauses = tuple(_clean_clause(item) for item in self.clauses if _clean_clause(item))
        object.__setattr__(self, "clauses", clauses)
        object.__setattr__(self, "issue_summary", str(self.issue_summary or "")[:500])
        object.__setattr__(self, "trajectory_steps", max(0, int(self.trajectory_steps)))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @property
    def claims(self) -> tuple[str, ...]:
        """Alias used by arbitration."""

        return self.clauses

    def to_dict(self) -> dict[str, Any]:
        return {
            "clauses": list(self.clauses),
            "issue_summary": self.issue_summary,
            "trajectory_steps": self.trajectory_steps,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BlindReconstruction:
    """Backward view reconstructed from a patch without issue text."""

    claims: tuple[str, ...] = ()
    patch_summary: str = ""
    used_issue_text: bool = False
    patch_digest: str = ""
    files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        claims = tuple(_clean_clause(item) for item in self.claims if _clean_clause(item))
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "patch_summary", str(self.patch_summary or "")[:500])
        object.__setattr__(self, "used_issue_text", bool(self.used_issue_text))
        object.__setattr__(self, "patch_digest", str(self.patch_digest or ""))
        object.__setattr__(self, "files", tuple(str(item) for item in self.files))

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": list(self.claims),
            "patch_summary": self.patch_summary,
            "used_issue_text": self.used_issue_text,
            "patch_digest": self.patch_digest,
            "files": list(self.files),
        }


@dataclass(frozen=True)
class RetraceVerdict:
    """Result of comparing forward rationale with a blind reconstruction."""

    status: str = STATUS_AGREE
    passed: bool = False
    missing: tuple[str, ...] = ()
    contradictory: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()
    repair_instruction: str = ""

    def __post_init__(self) -> None:
        if self.status not in {STATUS_AGREE, STATUS_MISMATCH, STATUS_CONTRADICTORY}:
            raise ValueError(f"unknown RetraceVerdict status: {self.status!r}")
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "missing", tuple(self.missing))
        object.__setattr__(self, "contradictory", tuple(self.contradictory))
        object.__setattr__(self, "matched", tuple(self.matched))
        object.__setattr__(self, "repair_instruction", str(self.repair_instruction or ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "missing": list(self.missing),
            "contradictory": list(self.contradictory),
            "matched": list(self.matched),
            "repair_instruction": self.repair_instruction,
        }


class RetraceVerifier:
    """Deterministic forward/backward/arbitrate verifier."""

    def __init__(self, *, match_threshold: float = _MATCH_THRESHOLD) -> None:
        if not 0 < match_threshold <= 1:
            raise ValueError("match_threshold must be in (0, 1]")
        self.match_threshold = float(match_threshold)

    # ── forward ─────────────────────────────────────────────────

    def forward(self, issue: Any, trajectory: Any) -> TraceRationale:
        """Extract rationale clauses from the issue and the agent trajectory."""

        issue_units = _issue_units(issue)
        trajectory_units = _trajectory_units(trajectory)
        raw = issue_units + trajectory_units
        requirement_units = [unit for unit in raw if _looks_like_requirement(unit)]
        if not requirement_units:
            requirement_units = raw
        clauses: list[str] = []
        seen: set[str] = set()
        for unit in requirement_units:
            clause = _clean_clause(unit)
            if not clause:
                continue
            key = _claim_key(clause)
            if not key or key in seen:
                continue
            seen.add(key)
            clauses.append(clause)
        summary = " ".join(issue_units[:2])[:500]
        return TraceRationale(
            clauses=tuple(clauses),
            issue_summary=summary,
            trajectory_steps=_trajectory_step_count(trajectory),
            metadata={"issue_units": len(issue_units), "trajectory_units": len(trajectory_units)},
        )

    # ── backward ────────────────────────────────────────────────

    def backward(
        self,
        patch: str,
        ast_index: Any,
        *,
        include_issue: bool = False,
    ) -> BlindReconstruction:
        """Reconstruct claims from ``patch`` and ``ast_index`` only.

        ``include_issue`` exists as an explicit API guard: the backward pass is
        blind by design, so the flag is accepted but deliberately ignored --
        no issue text is available on this path and none is embedded.
        """

        del include_issue  # blind reconstruction: the flag must never unlock issue text
        patch_text = str(patch or "")
        claims = _claims_from_patch(patch_text)
        claims.extend(_claims_from_ast_index(ast_index))
        deduped: list[str] = []
        seen: set[str] = set()
        for claim in claims:
            cleaned = _clean_clause(claim)
            key = _claim_key(cleaned)
            if not cleaned or not key or key in seen:
                continue
            seen.add(key)
            deduped.append(cleaned)
        files = tuple(sorted(_patch_files(patch_text)))
        digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()[:16] if patch_text else ""
        summary = _patch_summary(patch_text)
        return BlindReconstruction(
            claims=tuple(deduped),
            patch_summary=summary,
            used_issue_text=False,
            patch_digest=digest,
            files=files,
        )

    # ── arbitration ─────────────────────────────────────────────

    def arbitrate(
        self,
        rationale: TraceRationale | Any,
        reconstruction: BlindReconstruction | Any,
        patch_evidence: Any = None,
        *,
        requirements: Iterable[str] | None = None,
    ) -> RetraceVerdict:
        """Compare forward and backward claims and emit a targeted repair hint.

        ``patch_evidence`` is optional third-view evidence: a patch string, a
        mapping with a ``claims`` key, or any iterable of claim strings.  When
        supplied, its claims join the backward side of the comparison.
        """

        forward = _coerce_rationale(rationale).clauses
        if requirements is not None:
            forward = tuple(_clean_clause(item) for item in requirements if _clean_clause(item))
        backward_view = _coerce_reconstruction(reconstruction)
        backward = list(backward_view.claims)
        backward.extend(_coerce_evidence(patch_evidence))
        if backward_view.used_issue_text:
            return RetraceVerdict(
                status=STATUS_CONTRADICTORY,
                passed=False,
                contradictory=("backward reconstruction used issue text",),
                repair_instruction=(
                    "Re-run the backward pass without issue text: reconstruct only from the "
                    "patch and the AST index."
                ),
            )
        missing: list[str] = []
        contradictory: list[str] = []
        matched: list[str] = []
        for clause in forward:
            best_score = 0.0
            best_claim = ""
            for claim in backward:
                score = _claim_similarity(clause, claim)
                if score > best_score:
                    best_score = score
                    best_claim = claim
            if best_score >= self.match_threshold:
                if _negation_mismatch(clause, best_claim):
                    contradictory.append(clause)
                else:
                    matched.append(clause)
            else:
                missing.append(clause)
        if contradictory:
            status = STATUS_CONTRADICTORY
        elif missing:
            status = STATUS_MISMATCH
        else:
            status = STATUS_AGREE
        passed = status == STATUS_AGREE and bool(forward)
        if not forward:
            status = STATUS_MISMATCH
            passed = False
        instruction = _repair_instruction(missing, contradictory)
        return RetraceVerdict(
            status=status,
            passed=passed,
            missing=tuple(missing),
            contradictory=tuple(contradictory),
            matched=tuple(matched),
            repair_instruction=instruction,
        )


# ── text extraction helpers ─────────────────────────────────────


def _issue_units(issue: Any) -> list[str]:
    units: list[str] = []
    if isinstance(issue, Mapping):
        for key in (
            "title",
            "summary",
            "description",
            "body",
            "problem",
            "expected_behavior",
            "acceptance_criteria",
            "requirements",
        ):
            value = issue.get(key)
            units.extend(_text_units(value))
    else:
        units.extend(_text_units(issue))
    return _dedupe_units(units)


def _trajectory_units(trajectory: Any) -> list[str]:
    units: list[str] = []
    if isinstance(trajectory, Mapping):
        for key in ("rationale", "reasoning", "claims", "plan", "steps", "actions", "notes"):
            units.extend(_text_units(trajectory.get(key)))
    else:
        units.extend(_text_units(trajectory))
    return _dedupe_units(units)


def _trajectory_step_count(trajectory: Any) -> int:
    if isinstance(trajectory, Mapping):
        for key in ("steps", "actions"):
            value = trajectory.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return len(value)
    if isinstance(trajectory, Sequence) and not isinstance(trajectory, (str, bytes)):
        return len(trajectory)
    return 0


def _text_units(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _split_text(value)
    if isinstance(value, Mapping):
        units: list[str] = []
        for key in ("text", "description", "summary", "claim", "content", "action"):
            if key in value:
                units.extend(_text_units(value[key]))
        if not units:
            for nested in value.values():
                units.extend(_text_units(nested))
        return units
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        units = []
        for item in value:
            units.extend(_text_units(item))
        return units
    return [str(value)]


def _split_text(text: str) -> list[str]:
    pieces: list[str] = []
    for line in str(text).splitlines():
        stripped = line.strip().lstrip("-*0123456789. )(").strip()
        if not stripped:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+", stripped):
            cleaned = " ".join(sentence.split()).strip()
            if cleaned:
                pieces.append(cleaned)
    return pieces


def _dedupe_units(units: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for unit in units:
        cleaned = _clean_clause(unit)
        key = _claim_key(cleaned)
        if cleaned and key and key not in seen:
            seen.add(key)
            deduped.append(cleaned)
    return deduped


def _looks_like_requirement(text: str) -> bool:
    words = {word.lower() for word in _IDENTIFIER_RE.findall(text)}
    return bool(words & _REQUIREMENT_WORDS)


def _clean_clause(text: Any) -> str:
    cleaned = " ".join(str(text or "").replace("\x00", " ").split())
    return cleaned[:_MAX_CLAUSE_CHARS]


# ── patch / AST helpers ─────────────────────────────────────────


def _claims_from_patch(patch: str) -> list[str]:
    claims: list[str] = []
    for raw_line in patch.splitlines():
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+"):
            added = raw_line[1:].strip()
            if not added or added.startswith("#"):
                continue
            claims.append(f"patch adds: {added}")
    return claims


def _claims_from_ast_index(ast_index: Any) -> list[str]:
    claims: list[str] = []
    if isinstance(ast_index, Mapping):
        for path, symbols in ast_index.items():
            for name in _symbol_names(symbols):
                claims.append(f"patch defines {name} in {path}")
    elif ast_index is not None:
        for name in _symbol_names(ast_index):
            claims.append(f"patch defines {name}")
    return claims


def _symbol_names(value: Any) -> list[str]:
    names: list[str] = []
    if value is None:
        return names
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        for key in ("name", "symbol", "function", "class", "qualname"):
            if isinstance(value.get(key), str):
                names.append(value[key])
        for key in ("symbols", "functions", "classes", "definitions", "nodes"):
            if key in value:
                names.extend(_symbol_names(value[key]))
        return names
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            names.extend(_symbol_names(item))
        return names
    for attr in ("name", "symbol", "qualname"):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, str):
            names.append(candidate)
    return names


def _patch_files(patch: str) -> set[str]:
    files: set[str] = set()
    for raw_line in patch.splitlines():
        match = _PATCH_PATH_RE.match(raw_line.strip())
        if not match:
            continue
        raw = match.group(1).split("\t", 1)[0].strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            path = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            path = raw.split(" ", 1)[0]
        if not path or path == "/dev/null":
            continue
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path:
            files.add(path)
    return files


def _patch_summary(patch: str) -> str:
    files = sorted(_patch_files(patch))
    additions = sum(
        1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")
    )
    return f"{len(files)} file(s), +{additions}/-{deletions}: {', '.join(files[:5])}"


# ── claim matching helpers ──────────────────────────────────────


def _coerce_clauses(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(_split_text(raw))
    if isinstance(raw, (bytes, bytearray)):
        return tuple(_split_text(raw.decode("utf-8", errors="replace")))
    if isinstance(raw, Iterable):
        return tuple(str(item) for item in raw)
    return (str(raw),)


def _coerce_rationale(value: Any) -> TraceRationale:
    if value is None:
        return TraceRationale()
    if isinstance(value, TraceRationale):
        return value
    if isinstance(value, Mapping):
        raw = value.get("clauses")
        if raw is None:
            raw = value.get("claims", ())
        return TraceRationale(clauses=_coerce_clauses(raw))
    return TraceRationale(clauses=_coerce_clauses(value))


def _coerce_reconstruction(value: Any) -> BlindReconstruction:
    if value is None:
        return BlindReconstruction()
    if isinstance(value, BlindReconstruction):
        return value
    if isinstance(value, Mapping):
        raw = value.get("claims")
        if raw is None:
            raw = value.get("clauses", ())
        used_issue = value.get("used_issue_text", False)
        if isinstance(used_issue, str):
            used_issue_bool = used_issue.strip().lower() in _TRUTHY
        else:
            used_issue_bool = bool(used_issue)
        return BlindReconstruction(
            claims=_coerce_clauses(raw),
            used_issue_text=used_issue_bool,
        )
    return BlindReconstruction(claims=_coerce_clauses(value))


def _coerce_evidence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _claims_from_patch(value)
    if isinstance(value, (bytes, bytearray)):
        return _claims_from_patch(value.decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        raw = value.get("claims")
        if raw is None:
            raw = value.get("clauses", ())
        return list(_coerce_clauses(raw))
    if isinstance(value, Iterable):
        return [str(item) for item in value]
    return []


def _claim_key(text: str) -> str:
    return " ".join(sorted(_tokens(text)))


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z][a-z0-9]*", str(text).lower()):
        token = _stem(raw)
        if token and token not in _STOPWORDS and len(token) > 1:
            tokens.add(token)
    return tokens


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _claim_similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    if _claim_key(left) == _claim_key(right):
        return 1.0
    overlap = left_tokens & right_tokens
    if len(overlap) >= 2 and (right_tokens <= left_tokens or left_tokens <= right_tokens):
        return 1.0
    recall = len(overlap) / len(left_tokens)
    precision = len(overlap) / len(right_tokens)
    return (recall + precision) / 2


def _negation_mismatch(left: str, right: str) -> bool:
    left_words = {word.lower() for word in _WORD_RE.findall(left)}
    right_words = {word.lower() for word in _WORD_RE.findall(right)}
    return bool(left_words & _NEGATIONS) != bool(right_words & _NEGATIONS)


def _repair_instruction(missing: Sequence[str], contradictory: Sequence[str]) -> str:
    if not missing and not contradictory:
        return ""
    parts: list[str] = []
    if missing:
        parts.append("Add patch evidence satisfying: " + "; ".join(missing[:3]) + ".")
    if contradictory:
        parts.append("Resolve contradictory requirement(s): " + "; ".join(contradictory[:3]) + ".")
    parts.append("Re-run RETRACE backward reconstruction after the targeted repair.")
    return " ".join(parts)
