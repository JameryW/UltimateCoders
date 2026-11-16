"""Zero-edit PerceptionPass: compile a proposal into an implementation spec.

A planner proposal is only useful once it is grounded in the repository it will
be executed against. :class:`PerceptionPass` performs that grounding as a pure
read: it parses the target Python files with :mod:`ast`, maps their imports to
repository paths, computes a bounded dependency closure, extracts the public
API surface, and emits a deterministic set of static mapping checks. It never
edits a file and never touches the network.

The output is an :class:`ImplementationSpec` -- the contract handed to an
implementation step. It records both what the caller asked for and what the
repository actually contains, so a proposal can be rejected before any edit is
made instead of after a failed attempt.

The eight mapping checks are fixed and always emitted in :data:`MAPPING_CHECKS`
order:

``request_mapped``
    The request text is non-empty and can be carried into the spec.
``targets_declared``
    At least one target file was declared (or inferred from the request).
``targets_exist``
    Every target exists on disk; partial existence is a warning.
``python_targets_parse``
    Every Python target parses with :func:`ast.parse`; syntax errors are missing.
``imports_mapped``
    Imports of each Python target were classified as resolved or external.
``call_edges_mapped``
    Calls inside the target functions/classes were collected.
``dependency_closure_mapped``
    A two-hop dependency closure was computed from the existing Python targets.
``public_api_mapped``
    Public definitions in the target files were extracted.

Checks use ``"pass"``, ``"warn"`` or ``"missing"`` statuses and carry a short
human-readable detail. Confidence is a deterministic function of those statuses
(no model call, no clock), so two identical inputs always produce identical
specs.

Feature flag: ``UC_PERCEPTION_PASS``.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

__all__ = [
    "MAX_DEPENDENCY_HOPS",
    "MAPPING_CHECKS",
    "PERCEPTION_PASS_ENV",
    "FileBoundary",
    "ImplementationSpec",
    "PerceptionPass",
    "compile_spec",
    "perception_pass_enabled",
]

PERCEPTION_PASS_ENV = "UC_PERCEPTION_PASS"
#: Dependencies are followed exactly two hops from every target file.
MAX_DEPENDENCY_HOPS = 2

#: Every check name the compiler emits, in output order.
MAPPING_CHECKS = (
    "request_mapped",
    "targets_declared",
    "targets_exist",
    "python_targets_parse",
    "imports_mapped",
    "call_edges_mapped",
    "dependency_closure_mapped",
    "public_api_mapped",
)

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})
_CONFIG_SUFFIXES = frozenset(
    {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf"}
)
_CONFIG_NAMES = frozenset({"dockerfile", "makefile", "justfile"})
_CHECK_STATUSES = frozenset({"pass", "warn", "missing"})
_MAX_REQUEST_PATHS = 50

# ``path/to/file.py`` or a backticked token. Deliberately narrow: the
# perception pass must not mistake prose for a path.
_PATH_TOKEN = re.compile(
    r"`([^`\n]+)`"
    r"|(?<![\w/.-])((?:[\w.-]+[/\\])+[\w.-]+\.(?:py|pyi|md|rst|txt|toml|ya?ml|json|ini|cfg))"
)
# A definition kept out of the public API on purpose.
_PRIVATE_PREFIX = "_"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _truthy(value: str | None) -> bool:
    return _text(value).strip().lower() in _TRUTHY_VALUES


@dataclass(frozen=True)
class FileBoundary:
    """One file the implementation is allowed to touch, and why it is in scope."""

    path: str
    role: str
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "role": self.role, "reason": self.reason}


@dataclass
class ImplementationSpec:
    """A grounded proposal-to-implementation contract.

    ``target_files`` are the only files the implementation may edit;
    ``dependency_closure`` is read-only context discovered by import analysis.
    """

    task_id: str
    request: str
    target_files: list[FileBoundary] = field(default_factory=list)
    dependency_closure: list[str] = field(default_factory=list)
    public_api: list[str] = field(default_factory=list)
    checks: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "request": self.request,
            "target_files": [boundary.to_dict() for boundary in self.target_files],
            "dependency_closure": list(self.dependency_closure),
            "public_api": list(self.public_api),
            "checks": [dict(check) for check in self.checks],
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class _ModuleFacts:
    """Read-only AST facts for one parsed Python module."""

    path: str
    public_api: tuple[str, ...] = ()
    imports: tuple[tuple[str, int], ...] = ()  # (module, relative_level)
    call_edges: tuple[str, ...] = ()
    error: str = ""


def _file_role(path: str) -> str:
    """Classify a path as a coarse boundary role -- a naming convention, not I/O."""
    pure = PurePosixPath(_text(path).replace("\\", "/"))
    name = pure.name.lower()
    parts = {part.lower() for part in pure.parts[:-1]}
    if pure.suffix == ".py":
        if name == "__init__.py":
            return "package"
        if name.startswith("test_") or name.endswith("_test.py"):
            return "test"
        if "tests" in parts or "test" in parts:
            return "test"
        return "module"
    if pure.suffix in _DOC_SUFFIXES:
        return "documentation"
    if pure.suffix in _CONFIG_SUFFIXES or name in _CONFIG_NAMES:
        return "config"
    return "resource"


def _paths_in_request(request: str) -> list[str]:
    """Extract path-like tokens from a request without guessing at prose."""
    found: list[str] = []
    for match in _PATH_TOKEN.finditer(_text(request)):
        token = match.group(1) or match.group(2) or ""
        token = token.strip().strip("`").replace("\\", "/")
        if not token:
            continue
        # A backticked token is only a path candidate when it looks like one;
        # ``use `PerceptionPass``` must not become a target file.
        if match.group(1) and "/" not in token and "." not in PurePosixPath(token).name:
            continue
        if token not in found:
            found.append(token)
        if len(found) >= _MAX_REQUEST_PATHS:
            break
    return found


def _normalize_rel(path: Any, root: Path) -> str:
    """Normalize a caller path to a POSIX path relative to ``repo_root`` if possible."""
    raw = _text(path).strip().replace("\\", "/")
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return candidate.as_posix()
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _full_path(rel: str, root: Path) -> Path:
    candidate = Path(rel)
    return candidate if candidate.is_absolute() else root / candidate


def _as_rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _target_boundaries(
    constraints: Iterable[Any], *, inferred_from_request: bool
) -> list[FileBoundary]:
    boundaries: list[FileBoundary] = []
    seen: set[str] = set()
    reason = "inferred from request text" if inferred_from_request else "declared file constraint"
    for item in constraints or ():
        if isinstance(item, FileBoundary):
            path = _text(item.path)
            role = _text(item.role) or _file_role(path)
            item_reason = _text(item.reason) or reason
        else:
            path = _text(item)
            role = _file_role(path)
            item_reason = reason
        if not path or path in seen:
            continue
        seen.add(path)
        boundaries.append(FileBoundary(path=path, role=role, reason=item_reason))
    return boundaries


def _parse_module(path: Path, rel: str) -> _ModuleFacts:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _ModuleFacts(path=rel, error=f"{type(exc).__name__}: {exc}")
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return _ModuleFacts(path=rel, error=f"SyntaxError line {exc.lineno}: {exc.msg}")
    return _ModuleFacts(
        path=rel,
        public_api=tuple(_public_symbols(tree)),
        imports=tuple(_imports_of(tree)),
        call_edges=tuple(_call_edges(tree)),
    )


def _public_symbols(tree: ast.AST) -> list[str]:
    """Top-level public names, with public class methods as ``Class.method``."""
    symbols: list[str] = []
    for node in getattr(tree, "body", ()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            if _is_public(name):
                symbols.append(name)
        elif isinstance(node, ast.ClassDef):
            if _is_public(node.name):
                symbols.append(node.name)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if _is_public(child.name) and child.name != "__init__":
                            symbols.append(f"{node.name}.{child.name}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in _assign_targets(node):
                if _is_public(target):
                    symbols.append(target)
    return sorted(dict.fromkeys(symbols))


def _is_public(name: str) -> bool:
    return bool(name) and not name.startswith(_PRIVATE_PREFIX)


def _assign_targets(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets: list[ast.AST] = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.extend(item.id for item in target.elts if isinstance(item, ast.Name))
    return names


def _imports_of(tree: ast.AST) -> list[tuple[str, int]]:
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, 0))
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.module or "", int(node.level)))
    return imports


def _call_edges(tree: ast.AST) -> list[str]:
    """Deterministic ``caller -> callee`` pairs found in the module."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    edges: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = ""
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        if not callee:
            continue
        caller = _enclosing_name(node, parents)
        edges.add(f"{caller or '<module>'}->{callee}")
    return sorted(edges)


def _enclosing_name(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    names.reverse()
    return ".".join(names)


def _resolve_import(module: str, level: int, source_rel: str, root: Path) -> str:
    """Map an import to a repository-relative file path, or ``""`` if external."""
    parts = [part for part in _text(module).split(".") if part]
    if level:
        base = list(PurePosixPath(source_rel).parent.parts)
        drop = max(0, level - 1)
        if drop:
            base = base[: len(base) - drop] if drop <= len(base) else []
        parts = base + parts
    if not parts:
        return ""
    for search_root in (root, root / "python"):
        candidate = search_root.joinpath(*parts)
        options = []
        if candidate.suffix == ".py":
            options.append(candidate)
        else:
            options.append(candidate.with_suffix(".py"))
            options.append(candidate / "__init__.py")
        for option in options:
            if option.is_file():
                return _as_rel(option, root)
    return ""


def _looks_internal(module: str, root: Path) -> bool:
    first = _text(module).split(".")[0].strip()
    if not first:
        return False
    return (root / first).exists() or (root / "python" / first).exists()


def _dependency_closure(
    root: Path, targets: Sequence[str], facts: Mapping[str, _ModuleFacts]
) -> list[str]:
    """Two-hop outgoing import closure from the existing Python targets."""
    python_targets = [
        target
        for target in targets
        if target.lower().endswith(".py") and _full_path(target, root).is_file()
    ]
    visited = set(targets)
    frontier = list(python_targets)
    closure: dict[str, int] = {}
    for hop in range(1, MAX_DEPENDENCY_HOPS + 1):
        next_frontier: list[str] = []
        for rel in frontier:
            module_facts = facts.get(rel)
            if module_facts is None:
                module_facts = _parse_module(_full_path(rel, root), rel)
            for module, level in module_facts.imports:
                dependency = _resolve_import(module, level, rel, root)
                if not dependency or dependency in visited:
                    continue
                visited.add(dependency)
                closure[dependency] = hop
                next_frontier.append(dependency)
        frontier = next_frontier
    return sorted(closure)


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    if status not in _CHECK_STATUSES:
        raise ValueError(f"unknown check status: {status!r}")
    return {"name": name, "status": status, "detail": detail}


def _confidence(checks: Sequence[Mapping[str, str]]) -> float:
    score = 1.0
    for check in checks:
        if check.get("status") == "warn":
            score -= 0.15
        elif check.get("status") == "missing":
            score -= 0.35
    return round(max(0.0, min(1.0, score)), 2)


class PerceptionPass:
    """Compile a request and its file constraints into an implementation spec.

    The pass is stateless and read-only. A single instance may be reused; all
    per-call state lives in the returned :class:`ImplementationSpec`.
    """

    def compile(
        self,
        task_id: str,
        request: str,
        *,
        repo_root: str = ".",
        file_constraints: Sequence[Any] = (),
    ) -> ImplementationSpec:
        """Ground ``request`` against ``repo_root`` without editing anything."""
        root = Path(repo_root or ".")
        request_text = _text(request).strip()
        raw_constraints = list(file_constraints or ())
        inferred_from_request = False
        if not raw_constraints:
            inferred = _paths_in_request(request_text)
            raw_constraints = list(inferred)
            inferred_from_request = bool(inferred)

        normalized_constraints: list[Any] = []
        for item in raw_constraints:
            if isinstance(item, FileBoundary):
                normalized_constraints.append(
                    FileBoundary(
                        path=_normalize_rel(item.path, root),
                        role=_text(item.role) or _file_role(item.path),
                        reason=_text(item.reason),
                    )
                )
            else:
                normalized_constraints.append(_normalize_rel(item, root))
        boundaries = _target_boundaries(
            normalized_constraints, inferred_from_request=inferred_from_request
        )
        target_paths = [boundary.path for boundary in boundaries]

        facts: dict[str, _ModuleFacts] = {}
        parse_errors: list[str] = []
        missing_targets: list[str] = []
        for rel in target_paths:
            full = _full_path(rel, root)
            if not full.is_file():
                missing_targets.append(rel)
                continue
            if rel.lower().endswith(".py"):
                module_facts = _parse_module(full, rel)
                facts[rel] = module_facts
                if module_facts.error:
                    parse_errors.append(f"{rel}: {module_facts.error}")

        # Imports and call edges are only meaningful for parsed Python targets.
        resolved_imports = 0
        external_imports = 0
        unresolved_internal: list[str] = []
        call_edge_count = 0
        for rel, module_facts in facts.items():
            if module_facts.error:
                continue
            call_edge_count += len(module_facts.call_edges)
            for module, level in module_facts.imports:
                dependency = _resolve_import(module, level, rel, root)
                if dependency:
                    resolved_imports += 1
                    continue
                if level or _looks_internal(module, root):
                    label = f"{'.' * level}{module}" if level else module
                    unresolved_internal.append(f"{rel}: {label}")
                else:
                    external_imports += 1

        dependency_closure = _dependency_closure(root, target_paths, facts)
        public_api: list[str] = []
        for rel, module_facts in facts.items():
            if module_facts.error:
                continue
            for symbol in module_facts.public_api:
                if symbol not in public_api:
                    public_api.append(symbol)
        public_api.sort()

        python_targets = [rel for rel in target_paths if rel.lower().endswith(".py")]
        existing_targets = [rel for rel in target_paths if rel not in missing_targets]

        checks = [
            _check(
                "request_mapped",
                "pass" if request_text else "missing",
                f"{len(request_text)} character request"
                if request_text
                else "request text is empty",
            ),
            _check(
                "targets_declared",
                "pass" if target_paths else "missing",
                f"{len(target_paths)} target file(s)"
                + (" (inferred from request)" if inferred_from_request and target_paths else "")
                if target_paths
                else "no file constraints declared",
            ),
            _check(
                "targets_exist",
                _targets_exist_status(target_paths, missing_targets),
                _targets_exist_detail(target_paths, missing_targets),
            ),
            _check(
                "python_targets_parse",
                _parse_status(python_targets, parse_errors),
                _parse_detail(python_targets, parse_errors),
            ),
            _check(
                "imports_mapped",
                "warn" if unresolved_internal else "pass",
                f"{resolved_imports} resolved, {external_imports} external"
                + (
                    f", {len(unresolved_internal)} unresolved internal"
                    if unresolved_internal
                    else ""
                ),
            ),
            _check(
                "call_edges_mapped",
                "pass" if call_edge_count else "warn",
                f"{call_edge_count} call edge(s)"
                if call_edge_count
                else "no call edges found in Python targets",
            ),
            _check(
                "dependency_closure_mapped",
                "pass" if dependency_closure else "warn",
                f"{len(dependency_closure)} file(s) within {MAX_DEPENDENCY_HOPS} hop(s)"
                if dependency_closure
                else "dependency closure is empty",
            ),
            _check(
                "public_api_mapped",
                _public_api_status(python_targets, public_api),
                f"{len(public_api)} public symbol(s)"
                if public_api
                else "no public definitions found",
            ),
        ]
        checks = [dict(check, name=name) for name, check in zip(MAPPING_CHECKS, checks)]
        if len(checks) != len(MAPPING_CHECKS):
            raise AssertionError("perception pass must emit exactly eight checks")

        notes = [
            "read-only perception pass: no files were modified and no network was used",
            f"targets: {len(target_paths)} declared, {len(existing_targets)} existing",
            f"python targets parsed: {len(facts) - len(parse_errors)}",
            f"dependency closure: {len(dependency_closure)} file(s)",
            f"call edges: {call_edge_count}",
        ]
        if inferred_from_request and target_paths:
            notes.append("target files inferred from path tokens in the request")
        if missing_targets:
            notes.append("missing targets: " + ", ".join(sorted(missing_targets)))
        if parse_errors:
            notes.append("parse errors: " + "; ".join(parse_errors))

        return ImplementationSpec(
            task_id=_text(task_id),
            request=request_text,
            target_files=boundaries,
            dependency_closure=dependency_closure,
            public_api=public_api,
            checks=checks,
            confidence=_confidence(checks),
            notes=notes,
        )


def _targets_exist_status(targets: Sequence[str], missing: Sequence[str]) -> str:
    if not targets:
        return "missing"
    if not missing:
        return "pass"
    return "warn" if len(missing) < len(targets) else "missing"


def _targets_exist_detail(targets: Sequence[str], missing: Sequence[str]) -> str:
    if not targets:
        return "no targets to check"
    if not missing:
        return f"{len(targets)} target file(s) exist"
    return "missing: " + ", ".join(sorted(missing))


def _parse_status(python_targets: Sequence[str], errors: Sequence[str]) -> str:
    if not python_targets:
        return "pass"
    return "missing" if errors else "pass"


def _parse_detail(python_targets: Sequence[str], errors: Sequence[str]) -> str:
    if not python_targets:
        return "no Python targets to parse"
    if errors:
        return "; ".join(errors)
    return f"{len(python_targets)} Python target(s) parsed"


def _public_api_status(python_targets: Sequence[str], public_api: Sequence[str]) -> str:
    if not python_targets:
        return "pass"
    return "pass" if public_api else "warn"


def compile_spec(
    task_id: str,
    request: str,
    *,
    repo_root: str = ".",
    file_constraints: Sequence[Any] = (),
) -> ImplementationSpec:
    """Module-level convenience wrapper around :meth:`PerceptionPass.compile`."""
    return PerceptionPass().compile(
        task_id,
        request,
        repo_root=repo_root,
        file_constraints=file_constraints,
    )


def perception_pass_enabled() -> bool:
    """Whether ``UC_PERCEPTION_PASS`` enables the perception pass."""
    return _truthy(os.environ.get(PERCEPTION_PASS_ENV))
