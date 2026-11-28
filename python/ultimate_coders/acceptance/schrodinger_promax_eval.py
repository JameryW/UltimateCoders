"""SchrodingerRepo latent-variable transforms and ProMax refactor transactions.

Implements ledger item #59 (SchrodingerRepo, arXiv:2609.27891 and SWE-Bench
ProMax, arXiv:2608.09802). The evaluator applies four behavior-preserving
transformations before a benchmark task is admitted, so a model cannot score by
remembering a repository, file path, or symbol name. The refactor transaction
applies a multi-file rename atomically and can roll the workspace back.

This is a deterministic evaluation utility. It does not execute the transformed
repository, compile it, or write to disk.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "AtomicRefactorTransaction",
    "RefactorEdit",
    "RefactorError",
    "RepositorySnapshot",
    "RepoFile",
    "SCHRODINGER_PROMAX_ENV",
    "SchrodingerConfig",
    "SchrodingerTransformer",
    "TransformResult",
    "TransformationLevel",
    "schrodinger_promax_enabled",
]

SCHRODINGER_PROMAX_ENV = "UC_SCHRODINGER_PROMAX"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def schrodinger_promax_enabled() -> bool:
    """Return whether the evaluator is enabled in this environment."""
    return os.environ.get(SCHRODINGER_PROMAX_ENV, "").strip().lower() in _TRUTHY


class RefactorError(ValueError):
    """A malformed repository or refactor plan."""


class TransformationLevel(str, Enum):
    """The four latent-variable levels from SchrodingerRepo."""

    ISSUE_REWRITE = "issue_rewrite"
    NAMESPACE_REMAP = "namespace_remap"
    LAYOUT_SHUFFLE = "layout_shuffle"
    EQUIVALENT_REWRITE = "equivalent_rewrite"


DEFAULT_LEVELS: tuple[TransformationLevel, ...] = (
    TransformationLevel.ISSUE_REWRITE,
    TransformationLevel.NAMESPACE_REMAP,
    TransformationLevel.LAYOUT_SHUFFLE,
    TransformationLevel.EQUIVALENT_REWRITE,
)


@dataclass(frozen=True)
class RepoFile:
    """One file in a repository snapshot."""

    path: str
    content: str
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise RefactorError("file path must not be empty")
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise RefactorError("file path must be relative and contained")
        if not self.content:
            raise RefactorError("file content must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "content": self.content,
            "symbols": list(self.symbols),
        }


@dataclass(frozen=True)
class RepositorySnapshot:
    """A repository and its issue text."""

    files: tuple[RepoFile, ...]
    issue: str
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.files:
            raise RefactorError("repository must contain at least one file")
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise RefactorError("file paths must be unique")
        if not self.issue.strip():
            raise RefactorError("issue must not be empty")

    def file(self, path: str) -> RepoFile:
        for item in self.files:
            if item.path == path:
                return item
        raise RefactorError(f"unknown file: {path}")

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)

    def to_dict(self) -> dict[str, object]:
        return {
            "files": [item.to_dict() for item in self.files],
            "issue": self.issue,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SchrodingerConfig:
    """Deterministic salt and the active transformation levels."""

    salt: str = "schrodinger"
    levels: tuple[TransformationLevel, ...] = DEFAULT_LEVELS

    def __post_init__(self) -> None:
        if not self.salt.strip():
            raise RefactorError("salt must not be empty")
        if not self.levels:
            raise RefactorError("at least one transformation level is required")
        if len(set(self.levels)) != len(self.levels):
            raise RefactorError("transformation levels must be unique")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.salt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TransformResult:
    """The transformed repository plus the rename observations."""

    repository: RepositorySnapshot
    levels: tuple[TransformationLevel, ...]
    namespace_map: dict[str, str]
    issue_original: str

    def to_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository.to_dict(),
            "levels": [level.value for level in self.levels],
            "namespace_map": dict(self.namespace_map),
            "issue_original": self.issue_original,
        }


class SchrodingerTransformer:
    """Apply the four latent-variable levels in a fixed order."""

    def transform(
        self,
        snapshot: RepositorySnapshot,
        config: SchrodingerConfig | None = None,
    ) -> TransformResult:
        config = config or SchrodingerConfig()
        files = list(snapshot.files)
        issue = snapshot.issue
        namespace_map: dict[str, str] = {}
        applied: list[TransformationLevel] = []

        if TransformationLevel.ISSUE_REWRITE in config.levels:
            issue = self._rewrite_issue(issue, files)
            applied.append(TransformationLevel.ISSUE_REWRITE)

        if TransformationLevel.NAMESPACE_REMAP in config.levels:
            files, namespace_map = self._remap_namespace(files, config)
            applied.append(TransformationLevel.NAMESPACE_REMAP)

        if TransformationLevel.LAYOUT_SHUFFLE in config.levels:
            files = self._shuffle_layout(files, config)
            applied.append(TransformationLevel.LAYOUT_SHUFFLE)

        if TransformationLevel.EQUIVALENT_REWRITE in config.levels:
            files = [
                RepoFile(
                    path=item.path,
                    content=self._equivalent_rewrite(item.content),
                    symbols=item.symbols,
                )
                for item in files
            ]
            applied.append(TransformationLevel.EQUIVALENT_REWRITE)

        transformed = RepositorySnapshot(
            files=tuple(files),
            issue=issue,
            metadata=dict(snapshot.metadata),
        )
        return TransformResult(
            repository=transformed,
            levels=tuple(applied),
            namespace_map=namespace_map,
            issue_original=snapshot.issue,
        )

    @staticmethod
    def _rewrite_issue(issue: str, files: Iterable[RepoFile]) -> str:
        rewritten = issue
        for item in sorted(files, key=lambda file: len(file.path), reverse=True):
            rewritten = rewritten.replace(item.path, "<module>")
        for symbol in sorted(
            {symbol for item in files for symbol in item.symbols},
            key=len,
            reverse=True,
        ):
            rewritten = re.sub(
                rf"\b{re.escape(symbol)}\b",
                "<symbol>",
                rewritten,
            )
        return (
            "A defect is reported in the affected component. "
            "Reproduce it from the visible behavior; the original names are "
            "withheld.\n\n"
            f"{rewritten.strip()}"
        )

    @staticmethod
    def _remap_namespace(
        files: list[RepoFile],
        config: SchrodingerConfig,
    ) -> tuple[list[RepoFile], dict[str, str]]:
        symbols = sorted({symbol for item in files for symbol in item.symbols})
        mapping: dict[str, str] = {}
        for index, symbol in enumerate(symbols):
            digest = hashlib.sha256(
                f"{config.digest}:{index}:{symbol}".encode()
            ).hexdigest()[:8]
            mapping[symbol] = f"obs_{digest}"
        remapped: list[RepoFile] = []
        for item in files:
            content = item.content
            for old, new in sorted(mapping.items(), key=lambda pair: len(pair[0]), reverse=True):
                content = re.sub(rf"\b{re.escape(old)}\b", new, content)
            remapped.append(
                RepoFile(
                    path=item.path,
                    content=content,
                    symbols=tuple(mapping[symbol] for symbol in item.symbols),
                )
            )
        return remapped, mapping

    @staticmethod
    def _shuffle_layout(
        files: list[RepoFile],
        config: SchrodingerConfig,
    ) -> list[RepoFile]:
        return sorted(
            files,
            key=lambda item: hashlib.sha256(
                f"{config.digest}:{item.path}".encode()
            ).hexdigest(),
        )

    @staticmethod
    def _equivalent_rewrite(content: str) -> str:
        # Removing redundant grouping parentheses is a semantics-preserving
        # rewrite for the simple identifier conditions emitted by the model.
        content = re.sub(r"\b(if|while)\s+\(([A-Za-z_][\w.]*)\)\s*:", r"\1 \2:", content)
        return content


@dataclass(frozen=True)
class RefactorEdit:
    """Rename one symbol in a bounded set of files."""

    old_symbol: str
    new_symbol: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.old_symbol.strip() or not self.new_symbol.strip():
            raise RefactorError("refactor symbols must not be empty")
        if self.old_symbol == self.new_symbol:
            raise RefactorError("refactor symbols must differ")
        if not self.paths:
            raise RefactorError("refactor edit must target at least one file")
        if len(set(self.paths)) != len(self.paths):
            raise RefactorError("refactor paths must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "old_symbol": self.old_symbol,
            "new_symbol": self.new_symbol,
            "paths": list(self.paths),
        }


class AtomicRefactorTransaction:
    """Validate, apply, and roll back a multi-file rename transaction."""

    def __init__(
        self,
        snapshot: RepositorySnapshot,
        edits: Iterable[RefactorEdit],
    ) -> None:
        self._before = snapshot
        self._edits = tuple(edits)
        if not self._edits:
            raise RefactorError("transaction must contain at least one edit")
        self._order = self._validate()
        self._after: RepositorySnapshot | None = None

    @property
    def edits(self) -> tuple[RefactorEdit, ...]:
        return self._edits

    @property
    def applied(self) -> bool:
        return self._after is not None

    def _validate(self) -> tuple[RefactorEdit, ...]:
        olds = [edit.old_symbol for edit in self._edits]
        if len(set(olds)) != len(olds):
            raise RefactorError("old symbols must be unique")
        by_old = {edit.old_symbol: edit for edit in self._edits}
        for edit in self._edits:
            for path in edit.paths:
                item = self._before.file(path)
                if not re.search(rf"\b{re.escape(edit.old_symbol)}\b", item.content):
                    raise RefactorError(
                        f"symbol {edit.old_symbol} not found in {path}"
                    )
        # A -> B and B -> C must be applied B -> C first, otherwise A -> B is
        # rewritten a second time into C.
        graph = {
            edit.old_symbol: (
                by_old[edit.new_symbol].old_symbol
                if edit.new_symbol in by_old
                else None
            )
            for edit in self._edits
        }
        visiting: set[str] = set()
        visited: set[str] = set()
        order: list[RefactorEdit] = []

        def visit(symbol: str) -> None:
            if symbol in visiting:
                raise RefactorError("refactor rename cycle detected")
            if symbol in visited:
                return
            visiting.add(symbol)
            next_symbol = graph[symbol]
            if next_symbol is not None:
                visit(next_symbol)
            visiting.remove(symbol)
            visited.add(symbol)
            order.append(by_old[symbol])

        for symbol in sorted(graph):
            visit(symbol)
        return tuple(order)

    def apply(self) -> RepositorySnapshot:
        if self._after is not None:
            return self._after
        files = {item.path: item for item in self._before.files}
        for edit in self._order:
            for path in edit.paths:
                item = files[path]
                content = re.sub(
                    rf"\b{re.escape(edit.old_symbol)}\b",
                    edit.new_symbol,
                    item.content,
                )
                files[path] = RepoFile(
                    path=item.path,
                    content=content,
                    symbols=tuple(
                        edit.new_symbol if symbol == edit.old_symbol else symbol
                        for symbol in item.symbols
                    ),
                )
        self._after = RepositorySnapshot(
            files=tuple(files[path] for path in self._before.paths),
            issue=self._before.issue,
            metadata=dict(self._before.metadata),
        )
        return self._after

    def rollback(self) -> RepositorySnapshot:
        self._after = None
        return self._before
