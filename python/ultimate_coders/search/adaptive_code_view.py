"""Task-adaptive code-representation routing for retrieval (ledger item #104).

The same repository is projected differently for fault localisation, security
patches, test reproduction, and general edits. This router selects the cheapest
MCP view class that supplies the signals the task actually needs, instead of a
single one-size-fits-all representation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "ADAPTIVE_CODE_VIEW_ENV",
    "ViewKind",
    "TaskKind",
    "CodeCandidate",
    "CodeViewSelection",
    "CodeViewPlan",
    "AdaptiveCodeViewRouter",
    "adaptive_code_view_enabled",
]

ADAPTIVE_CODE_VIEW_ENV = "UC_ADAPTIVE_CODE_VIEW"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def adaptive_code_view_enabled() -> bool:
    return os.environ.get(ADAPTIVE_CODE_VIEW_ENV, "").strip().lower() in _TRUTHY


class ViewKind(str):
    AST = "ast"
    CALLGRAPH = "callgraph"
    DEPS = "deps"
    DOCS = "docs"
    HYBRID = "hybrid"


class TaskKind(str):
    FAULT_LOCALIZATION = "fault_localization"
    SECURITY_PATCH = "security_patch"
    TEST_REPRODUCTION = "test_reproduction"
    GENERAL = "general"


_TASK_VIEWS = {
    TaskKind.FAULT_LOCALIZATION: (ViewKind.AST, ViewKind.DEPS),
    TaskKind.SECURITY_PATCH: (ViewKind.CALLGRAPH, ViewKind.AST),
    TaskKind.TEST_REPRODUCTION: (ViewKind.HYBRID, ViewKind.DOCS),
    TaskKind.GENERAL: (ViewKind.HYBRID,),
}


class AdaptiveCodeViewError(ValueError):
    pass


@dataclass(frozen=True)
class CodeCandidate:
    path: str
    token_estimate: int = 200
    has_ast: bool = False
    has_callgraph: bool = False
    has_deps: bool = False
    has_docs: bool = False
    has_hybrid: bool = False
    relevance: float = 0.5

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise AdaptiveCodeViewError("path must not be empty")
        if self.token_estimate <= 0:
            raise AdaptiveCodeViewError("token_estimate must be positive")
        if not 0.0 <= self.relevance <= 1.0:
            raise AdaptiveCodeViewError("relevance must be in [0, 1]")

    def supports(self, kind: str) -> bool:
        return {
            ViewKind.AST: self.has_ast,
            ViewKind.CALLGRAPH: self.has_callgraph,
            ViewKind.DEPS: self.has_deps,
            ViewKind.DOCS: self.has_docs,
            ViewKind.HYBRID: self.has_hybrid,
        }.get(kind, False)


@dataclass(frozen=True)
class CodeViewSelection:
    path: str
    view: str
    token_estimate: int
    score: float


@dataclass(frozen=True)
class CodeViewPlan:
    task: str
    views: tuple[CodeViewSelection, ...]
    reason: str
    budget_tokens: int
    degraded: bool = False

    def render(self) -> str:
        lines = [f"## Code views for {self.task}"]
        for selection in self.views:
            lines.append(
                f"- [{selection.view}] {selection.path} (~{selection.token_estimate} tokens)"
            )
        return "\n".join(lines)


class AdaptiveCodeViewRouter:
    """Route a set of repository candidates to task-appropriate views."""

    def __init__(self, budget_tokens: int = 4000) -> None:
        if budget_tokens <= 0:
            raise AdaptiveCodeViewError("budget_tokens must be positive")
        self.budget_tokens = budget_tokens

    def route(
        self,
        task: str,
        candidates: tuple[CodeCandidate, ...] | list[CodeCandidate],
        *,
        budget_tokens: int | None = None,
    ) -> CodeViewPlan:
        if task not in _TASK_VIEWS:
            raise AdaptiveCodeViewError(f"unknown task kind: {task}")
        budget = self.budget_tokens if budget_tokens is None else budget_tokens
        if budget <= 0:
            raise AdaptiveCodeViewError("budget_tokens must be positive")

        preferred = _TASK_VIEWS[task]
        scored: list[CodeViewSelection] = []
        for candidate in candidates:
            for priority, view in enumerate(preferred):
                if candidate.supports(view):
                    score = candidate.relevance + (len(preferred) - priority) * 0.1
                    scored.append(
                        CodeViewSelection(
                            path=candidate.path,
                            view=view,
                            token_estimate=candidate.token_estimate,
                            score=round(score, 4),
                        )
                    )
        scored.sort(key=lambda item: (-item.score, item.path, item.view))
        selected: list[CodeViewSelection] = []
        used = 0
        degraded = False
        for selection in scored:
            if used + selection.token_estimate <= budget:
                selected.append(selection)
                used += selection.token_estimate
            else:
                degraded = True
        if not selected:
            # Preserve at least one useful projection even in a tight budget.
            fallback = min(
                candidates,
                key=lambda candidate: (
                    candidate.token_estimate,
                    -candidate.relevance,
                    candidate.path,
                ),
                default=None,
            )
            if fallback is not None:
                view = preferred[0] if fallback.supports(preferred[0]) else ViewKind.HYBRID
                selected = [
                    CodeViewSelection(
                        path=fallback.path,
                        view=view,
                        token_estimate=min(fallback.token_estimate, budget),
                        score=round(fallback.relevance, 4),
                    )
                ]
                degraded = True
        reason = "preferred views fit budget" if not degraded else "budget required degradation"
        return CodeViewPlan(
            task=task,
            views=tuple(selected),
            reason=reason,
            budget_tokens=budget,
            degraded=degraded,
        )
