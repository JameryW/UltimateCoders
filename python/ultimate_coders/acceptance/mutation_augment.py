"""Deterministic mutation augmentation for acceptance review.

Mutation augmentation asks whether a test suite can kill deliberately broken
versions of the patch. Instead of relying on an LLM to invent mutants, this
module derives a small, deterministic candidate set from the diff itself:

``SECTION_REVERT``
    A contiguous changed section is reverted to its pre-patch text. This tests
    that newly added behavior is actually covered.

``REQUIREMENT_MISMATCH``
    A requirement-bearing token in an added line is changed -- a modal keyword
    (``must`` -> ``may``), a number, a boolean, or a comparison operator. This
    tests whether stated acceptance requirements are enforced.

``SEMANTIC_MUTATION``
    A Python AST-level constant or operator is altered (constant value,
    ``==`` <-> ``!=``, ``and`` <-> ``or``), approximating classic mutation
    testing without running code.

Generation is pure text/AST work: deterministic for the same snapshots, with
no execution and no network. A patch that has no suitable token simply yields
no candidate for that kind rather than a guessed one.

Feature flag: ``UC_MUTATION_AUGMENT``.
"""

from __future__ import annotations

import ast
import difflib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "MUTATION_AUGMENT_ENV",
    "Mutation",
    "MutationBatch",
    "MutationKind",
    "generate_mutations",
    "mutation_augment_enabled",
    "mutant_kill_rate",
]

MUTATION_AUGMENT_ENV = "UC_MUTATION_AUGMENT"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_MAX_SECTION_MUTATIONS = 10
_MAX_REQUIREMENT_MUTATIONS = 40
_MAX_SEMANTIC_MUTATIONS = 40

_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
_MODAL_RE = re.compile(r"\b(must|should|always|never|required|shall)\b", re.IGNORECASE)
_BOOL_RE = re.compile(r"\b(True|False)\b")
_COMPARISON_RE = re.compile(r"(==|!=|<=|>=|<|>)")
_QUOTED_RE = re.compile(r"(\"([^\"\\]|\\.)*\"|'([^'\\]|\\.)*')")

_MODAL_REPLACEMENTS = {
    "must": "may",
    "should": "may",
    "always": "never",
    "never": "always",
    "required": "optional",
    "shall": "may",
}
_COMPARISON_REPLACEMENTS = {
    "==": "!=",
    "!=": "==",
    "<=": ">",
    ">=": "<",
    "<": ">=",
    ">": "<=",
}


class MutationKind(str, Enum):
    """The three deterministic mutation families."""

    SECTION_REVERT = "section_revert"
    REQUIREMENT_MISMATCH = "requirement_mismatch"
    SEMANTIC_MUTATION = "semantic_mutation"


@dataclass(frozen=True)
class Mutation:
    """One candidate broken version of the patch."""

    kind: MutationKind
    path: str
    description: str
    before: str
    after: str
    line: int = 0

    @property
    def original(self) -> str:
        """The unmutated file content (alias for ``before``)."""
        return self.before

    @property
    def mutated(self) -> str:
        """The mutated file content (alias for ``after``)."""
        return self.after

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "description": self.description,
            "before": self.before,
            "after": self.after,
            "line": self.line,
        }


@dataclass
class MutationBatch:
    """Deterministically ordered mutation candidates for one patch."""

    mutations: list[Mutation] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.mutations)

    def __iter__(self):
        return iter(self.mutations)

    def by_kind(self, kind: MutationKind) -> list[Mutation]:
        return [mutation for mutation in self.mutations if mutation.kind == kind]

    @property
    def kinds(self) -> set[MutationKind]:
        return {mutation.kind for mutation in self.mutations}

    def kill_rate(self, killed: int) -> float:
        return mutant_kill_rate(killed, len(self.mutations))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutations": [mutation.to_dict() for mutation in self.mutations],
            "count": len(self.mutations),
            "kinds": sorted(kind.value for kind in self.kinds),
        }


def mutation_augment_enabled() -> bool:
    """Whether ``UC_MUTATION_AUGMENT`` enables mutation augmentation."""
    return os.environ.get(MUTATION_AUGMENT_ENV, "").strip().lower() in _TRUTHY_VALUES


def mutant_kill_rate(killed: int, total: int) -> float:
    """Return the killed-mutant percentage, rounded to two decimals.

    A zero total has no meaningful percentage and returns ``0.0``.
    """
    total = int(total)
    if total <= 0:
        return 0.0
    killed = max(0, min(int(killed), total))
    return round(100.0 * killed / total, 2)


def generate_mutations(before: Mapping[str, str], after: Mapping[str, str]) -> MutationBatch:
    """Generate deterministic text/AST mutation candidates from a patch delta."""
    mutations: list[Mutation] = []
    changed_paths = [
        path
        for path in sorted(set(before) | set(after))
        if _text(before.get(path, "")) != _text(after.get(path, ""))
    ]
    for path in changed_paths:
        before_text = _text(before.get(path, ""))
        after_text = _text(after.get(path, ""))
        mutations.extend(_section_revert_mutations(path, before_text, after_text))
        mutations.extend(_requirement_mismatch_mutations(path, before_text, after_text))
        if path.lower().endswith(".py"):
            mutations.extend(_semantic_mutations(path, after_text))
    deduped = list(
        dict.fromkeys(
            sorted(mutations, key=lambda item: (item.path, item.line, item.kind.value, item.after))
        )
    )
    return MutationBatch(mutations=deduped)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _line_changes(
    before_lines: list[str], after_lines: list[str]
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, int, int, int, str]]]:
    """Return removed/added lines and raw opcodes for a file pair."""
    removed: list[tuple[int, str]] = []
    added: list[tuple[int, str]] = []
    opcodes: list[tuple[int, int, int, int, str]] = []
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        opcodes.append((before_start, before_end, after_start, after_end, tag))
        if tag in ("replace", "delete"):
            for offset, text in enumerate(before_lines[before_start:before_end]):
                removed.append((before_start + offset + 1, text))
        if tag in ("replace", "insert"):
            for offset, text in enumerate(after_lines[after_start:after_end]):
                added.append((after_start + offset + 1, text))
    return removed, added, opcodes


def _render_lines(lines: list[str]) -> str:
    return "\n".join(lines)


def _replace_line(text: str, line: int, replacement: str) -> str:
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return text
    lines[line - 1] = replacement
    rendered = _render_lines(lines)
    if text.endswith("\n"):
        rendered += "\n"
    return rendered


def _section_revert_mutations(path: str, before_text: str, after_text: str) -> list[Mutation]:
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    _, _, opcodes = _line_changes(before_lines, after_lines)
    mutations: list[Mutation] = []
    for before_start, before_end, after_start, after_end, tag in opcodes:
        if tag == "delete" or after_start == after_end:
            continue
        reverted = (
            after_lines[:after_start]
            + before_lines[before_start:before_end]
            + after_lines[after_end:]
        )
        mutations.append(
            Mutation(
                kind=MutationKind.SECTION_REVERT,
                path=path,
                description=(
                    f"revert changed section at line {after_start + 1} "
                    f"to its pre-patch form"
                ),
                before=after_text,
                after=_render_lines(reverted) + ("\n" if after_text.endswith("\n") else ""),
                line=after_start + 1,
            )
        )
        if len(mutations) >= _MAX_SECTION_MUTATIONS:
            break
    if not mutations and before_text != after_text:
        mutations.append(
            Mutation(
                kind=MutationKind.SECTION_REVERT,
                path=path,
                description="revert the whole file to its pre-patch form",
                before=after_text,
                after=before_text,
                line=1,
            )
        )
    return mutations


def _requirement_mismatch_mutations(
    path: str, before_text: str, after_text: str
) -> list[Mutation]:
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    _, added, _ = _line_changes(before_lines, after_lines)
    mutations: list[Mutation] = []
    for line, text in added:
        replacement = _mismatch_replacement(text)
        if replacement is None or replacement == text:
            continue
        mutations.append(
            Mutation(
                kind=MutationKind.REQUIREMENT_MISMATCH,
                path=path,
                description=f"change requirement token at line {line}",
                before=after_text,
                after=_replace_line(after_text, line, replacement),
                line=line,
            )
        )
        if len(mutations) >= _MAX_REQUIREMENT_MUTATIONS:
            break
    return mutations


def _mismatch_replacement(text: str) -> str | None:
    match = _MODAL_RE.search(text)
    if match is not None:
        token = match.group(1)
        replacement = _MODAL_REPLACEMENTS[token.lower()]
        if token.isupper():
            replacement = replacement.upper()
        elif token[0].isupper():
            replacement = replacement.capitalize()
        return text[: match.start(1)] + replacement + text[match.end(1) :]
    match = _NUMBER_RE.search(text)
    if match is not None:
        value = match.group(1)
        if "." in value:
            changed = f"{float(value) + 1:g}"
        else:
            changed = str(int(value) + 1)
        return text[: match.start(1)] + changed + text[match.end(1) :]
    match = _BOOL_RE.search(text)
    if match is not None:
        replacement = "False" if match.group(1) == "True" else "True"
        return text[: match.start(1)] + replacement + text[match.end(1) :]
    match = _COMPARISON_RE.search(text)
    if match is not None:
        return (
            text[: match.start(1)]
            + _COMPARISON_REPLACEMENTS[match.group(1)]
            + text[match.end(1) :]
        )
    return None


def _semantic_mutations(path: str, after_text: str) -> list[Mutation]:
    try:
        tree = ast.parse(after_text)
    except SyntaxError:
        return []
    mutations: list[Mutation] = []
    seen: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if len(mutations) >= _MAX_SEMANTIC_MUTATIONS:
            break
        if isinstance(node, ast.Constant):
            mutation = _constant_mutation(path, after_text, node)
            if mutation is not None:
                key = (mutation.line, mutation.after)
                if key not in seen:
                    seen.add(key)
                    mutations.append(mutation)
        elif isinstance(node, ast.Compare):
            mutation = _operator_mutation(path, after_text, node)
            if mutation is not None:
                key = (mutation.line, mutation.after)
                if key not in seen:
                    seen.add(key)
                    mutations.append(mutation)
        elif isinstance(node, ast.BoolOp):
            mutation = _bool_op_mutation(path, after_text, node)
            if mutation is not None:
                key = (mutation.line, mutation.after)
                if key not in seen:
                    seen.add(key)
                    mutations.append(mutation)
    return mutations


def _constant_mutation(path: str, text: str, node: ast.Constant) -> Mutation | None:
    value = node.value
    if isinstance(value, bool):
        token = "True" if value else "False"
        replacement = "False" if value else "True"
    elif isinstance(value, int):
        token = str(value)
        replacement = str(value + 1)
    elif isinstance(value, float):
        token = repr(value)
        replacement = repr(value + 1.0)
    elif isinstance(value, str):
        if not value or "\n" in value:
            return None
        token = repr(value)
        replacement = repr(value + "_mutant")
    else:
        return None
    line = getattr(node, "lineno", 0)
    source_line = _source_line(text, line)
    if source_line is None or token not in source_line:
        return None
    new_line = source_line.replace(token, replacement, 1)
    return Mutation(
        kind=MutationKind.SEMANTIC_MUTATION,
        path=path,
        description=f"mutate constant at line {line}: {token} -> {replacement}",
        before=text,
        after=_replace_line(text, line, new_line),
        line=line,
    )


def _operator_mutation(path: str, text: str, node: ast.Compare) -> Mutation | None:
    if not node.ops:
        return None
    operator = _COMPARISON_REPLACEMENTS.get(_comparison_symbol(node.ops[0]))
    if operator is None:
        return None
    line = getattr(node, "lineno", 0)
    source_line = _source_line(text, line)
    if source_line is None:
        return None
    original = _comparison_symbol(node.ops[0])
    if original not in source_line:
        return None
    new_line = source_line.replace(original, operator, 1)
    return Mutation(
        kind=MutationKind.SEMANTIC_MUTATION,
        path=path,
        description=f"mutate comparison at line {line}: {original} -> {operator}",
        before=text,
        after=_replace_line(text, line, new_line),
        line=line,
    )


def _bool_op_mutation(path: str, text: str, node: ast.BoolOp) -> Mutation | None:
    line = getattr(node, "lineno", 0)
    source_line = _source_line(text, line)
    if source_line is None:
        return None
    if isinstance(node.op, ast.And):
        original, replacement = "and", "or"
    elif isinstance(node.op, ast.Or):
        original, replacement = "or", "and"
    else:
        return None
    if original not in source_line:
        return None
    new_line = source_line.replace(original, replacement, 1)
    return Mutation(
        kind=MutationKind.SEMANTIC_MUTATION,
        path=path,
        description=f"mutate boolean operator at line {line}: {original} -> {replacement}",
        before=text,
        after=_replace_line(text, line, new_line),
        line=line,
    )


def _comparison_symbol(operator: ast.cmpop) -> str:
    return {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
    }.get(type(operator), "")


def _source_line(text: str, line: int) -> str | None:
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return None
    return lines[line - 1]
