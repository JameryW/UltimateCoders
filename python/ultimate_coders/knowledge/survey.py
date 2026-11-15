"""Static AST survey of a Python working tree for the knowledge layer.

A survey is an observation, not an authority: it records which modules exist,
which symbols each module defines, which line span each symbol occupies and
which module names it imports. Versioned knowledge pages derive from these
observations; nothing in this module writes to the repository.

The survey is deliberately offline, deterministic and bounded:

* only ``.py`` files under ``root`` are parsed; tooling/vendor directories
  (``.venv``/``node_modules``/``.git``/``vendor``/``build``/``dist`` and any
  hidden directory) are pruned before they are walked;
* modules are ordered by POSIX relative path, symbols by start line and
  qualified name, imports alphabetically - the same tree always yields the same
  survey and therefore the same :func:`survey_digest`;
* a file that cannot be read or parsed is skipped and counted, never raised;
* every string is clipped with :func:`ultimate_coders.knowledge.model.safe_text`
  and line numbers are clamped, so a survey is safe to store and log.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .model import digest, safe_text

__all__ = [
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_SYMBOLS_PER_FILE",
    "MAX_CANDIDATE_FILES",
    "MAX_DOCSTRING_CHARS",
    "MAX_FILES_LIMIT",
    "MAX_IMPORTS",
    "MAX_IMPORT_CHARS",
    "MAX_LINE_NUMBER",
    "MAX_PATH_CHARS",
    "MAX_SYMBOLS_LIMIT",
    "SKIP_DIRS",
    "ModuleSurvey",
    "SurveyReport",
    "SurveyStats",
    "SymbolInfo",
    "repo_survey",
    "summarize_survey",
    "survey_digest",
    "survey_report",
    "survey_source",
]

DEFAULT_MAX_FILES = 200
DEFAULT_MAX_SYMBOLS_PER_FILE = 200
MAX_FILES_LIMIT = 5_000
MAX_SYMBOLS_LIMIT = 2_000
#: Hard ceiling on how many candidate paths are collected before bounds apply.
MAX_CANDIDATE_FILES = 10_000
MAX_IMPORTS = 200
MAX_IMPORT_CHARS = 200
MAX_DOCSTRING_CHARS = 300
MAX_PATH_CHARS = 400
MAX_LINE_NUMBER = 10_000_000
MAX_FILE_BYTES = 2_000_000
#: Directory names never walked: caches, virtualenvs, vendored and build output.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "target",
        "vendor",
        "venv",
    }
)
_TEST_DIR_NAMES = frozenset({"test", "tests", "testing"})
_SYMBOL_KINDS = frozenset({"class", "function", "method"})


# ---------------------------------------------------------------------------
# Small coercion / bounding helpers
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Bounded, stripped text for a stored string field."""

    return safe_text(value, limit=MAX_PATH_CHARS).strip()


def _docstring(value: Any) -> str:
    """Whitespace-collapsed, bounded module docstring (empty when absent)."""

    collapsed = " ".join(safe_text(value, limit=MAX_DOCSTRING_CHARS * 4).split())
    return safe_text(collapsed, limit=MAX_DOCSTRING_CHARS).strip()


def _import_name(value: Any) -> str:
    """Bounded module name for the imports projection."""

    return safe_text(value, limit=MAX_IMPORT_CHARS).strip()


def _line(value: Any, default: int = 0) -> int:
    """Clamp a line number into ``[0, MAX_LINE_NUMBER]``."""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_LINE_NUMBER, number))


def _clamp(value: Any, *, default: int, maximum: int) -> int:
    """Clamp a caller-supplied bound into ``[0, maximum]``."""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(maximum, number))


def _kind(value: Any) -> str:
    """Coerce a symbol kind; unknown labels degrade to ``function``."""

    text = safe_text(value, limit=40).strip().lower()
    return text if text in _SYMBOL_KINDS else "function"


def _dedupe(values: Iterable[Any], *, limit: int, render=_import_name) -> tuple[str, ...]:
    """Order-preserving, duplicate-free, bounded tuple of rendered strings."""

    collected: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = render(item)
        if not text or text in seen:
            continue
        seen.add(text)
        collected.append(text)
        if len(collected) >= limit:
            break
    return tuple(collected)


def _module_name(relative: PurePosixPath) -> str:
    """Derive a dotted module name from a path relative to the survey root."""

    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or relative.stem or ""


def _is_test_path(relative: PurePosixPath) -> bool:
    """True for conventional test files/directories (``tests/``, ``test_*.py``)."""

    if any(part.lower() in _TEST_DIR_NAMES for part in relative.parts[:-1]):
        return True
    name = relative.name.lower()
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _skip_dir(name: str) -> bool:
    """True when a directory must not be walked (tooling, vendor, hidden)."""

    return name.lower() in SKIP_DIRS or name.startswith(".")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolInfo:
    """One function/class symbol observed in one module.

    ``qualname`` is the dotted name relative to the module (``Class.method``);
    ``parent`` is the enclosing symbol's qualified name, or ``""`` at module
    level. Line numbers are clamped and ``end_line`` never precedes
    ``start_line``.
    """

    path: str
    kind: str
    name: str
    qualname: str
    start_line: int
    end_line: int
    parent: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _text(self.path))
        object.__setattr__(self, "kind", _kind(self.kind))
        qualname = _text(self.qualname)
        name = _text(self.name) or qualname.rpartition(".")[2]
        object.__setattr__(self, "qualname", qualname or name)
        object.__setattr__(self, "name", name)
        parent = _text(self.parent)
        if not parent and "." in (qualname or name):
            parent = (qualname or name).rpartition(".")[0]
        object.__setattr__(self, "parent", parent)
        start = _line(self.start_line)
        end = _line(self.end_line, default=start)
        object.__setattr__(self, "start_line", start)
        object.__setattr__(self, "end_line", max(start, end))

    def to_dict(self) -> dict:
        """JSON-safe projection (no source text)."""

        return {
            "path": self.path,
            "kind": self.kind,
            "name": self.name,
            "qualname": self.qualname,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "parent": self.parent,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SymbolInfo:
        """Rebuild a symbol from stored data, tolerating missing fields."""

        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            path=_text(data.get("path")),
            kind=_kind(data.get("kind")),
            name=_text(data.get("name")),
            qualname=_text(data.get("qualname")),
            start_line=_line(data.get("start_line")),
            end_line=_line(data.get("end_line")),
            parent=_text(data.get("parent")),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: location and name only, never source text."""

        return {
            "path": self.path,
            "kind": self.kind,
            "qualname": self.qualname,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class ModuleSurvey:
    """Symbols, imports and docstring headline observed for one Python module.

    The docstring is reduced to its first whitespace-collapsed headline and
    bounded; it is never a copy of arbitrary module text. ``symbols`` are
    sorted by ``(start_line, end_line, qualname)`` and ``imports``
    alphabetically, so two surveys of the same file compare equal.
    """

    path: str
    module: str
    docstring: str = ""
    symbols: tuple[SymbolInfo, ...] = ()
    imports: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _text(self.path))
        module = _text(self.module) or _module_name(PurePosixPath(self.path))
        object.__setattr__(self, "module", module)
        object.__setattr__(self, "docstring", _docstring(self.docstring))
        collected: list[SymbolInfo] = []
        seen: set[tuple] = set()
        for item in tuple(self.symbols or ()):
            symbol = item if isinstance(item, SymbolInfo) else None
            if symbol is None and isinstance(item, Mapping):
                symbol = SymbolInfo.from_dict(item)
            if symbol is None:
                continue
            key = (symbol.qualname, symbol.kind, symbol.start_line, symbol.end_line)
            if key in seen:
                continue
            seen.add(key)
            collected.append(symbol)
            if len(collected) >= MAX_SYMBOLS_LIMIT:
                break
        collected.sort(key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.qualname))
        object.__setattr__(self, "symbols", tuple(collected))
        object.__setattr__(
            self,
            "imports",
            _dedupe(tuple(self.imports or ()), limit=MAX_IMPORTS),
        )

    def symbol_count(self) -> int:
        """Number of symbols the survey retained for this module."""

        return len(self.symbols)

    def to_dict(self) -> dict:
        """JSON-safe projection of the module survey."""

        return {
            "path": self.path,
            "module": self.module,
            "docstring": self.docstring,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "imports": list(self.imports),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ModuleSurvey:
        """Rebuild a module survey from stored data, tolerating malformed parts."""

        data = payload if isinstance(payload, Mapping) else {}
        symbols = data.get("symbols") or ()
        imports = data.get("imports") or ()
        return cls(
            path=_text(data.get("path")),
            module=_text(data.get("module")),
            docstring=_docstring(data.get("docstring")),
            symbols=tuple(
                item for item in symbols if isinstance(item, (SymbolInfo, Mapping))
            ),
            imports=tuple(item for item in imports if isinstance(item, str)),
        )

    def to_summary(self) -> dict:
        """Log-safe projection: counts and identity, never docstring/import text."""

        return {
            "path": self.path,
            "module": self.module,
            "symbols": len(self.symbols),
            "imports": len(self.imports),
            "has_docstring": bool(self.docstring),
        }


@dataclass(frozen=True)
class SurveyStats:
    """Bounded counters describing how a repository survey was produced."""

    scanned_files: int = 0
    parsed_files: int = 0
    skipped_files: int = 0
    skipped_dirs: int = 0
    truncated_files: int = 0
    limited_files: int = 0

    def to_dict(self) -> dict:
        """JSON-safe counters (never file contents)."""

        return {
            "scanned_files": self.scanned_files,
            "parsed_files": self.parsed_files,
            "skipped_files": self.skipped_files,
            "skipped_dirs": self.skipped_dirs,
            "truncated_files": self.truncated_files,
            "limited_files": self.limited_files,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SurveyStats:
        """Rebuild counters from stored data, clamping every field."""

        data = payload if isinstance(payload, Mapping) else {}
        return cls(
            scanned_files=_line(data.get("scanned_files")),
            parsed_files=_line(data.get("parsed_files")),
            skipped_files=_line(data.get("skipped_files")),
            skipped_dirs=_line(data.get("skipped_dirs")),
            truncated_files=_line(data.get("truncated_files")),
            limited_files=_line(data.get("limited_files")),
        )

    def to_summary(self) -> dict:
        """Log-safe projection of the counters."""

        return self.to_dict()


@dataclass(frozen=True)
class SurveyReport:
    """A completed survey plus the counters that explain its bounds."""

    modules: tuple[ModuleSurvey, ...] = ()
    stats: SurveyStats = SurveyStats()

    def __post_init__(self) -> None:
        collected: list[ModuleSurvey] = []
        for item in tuple(self.modules or ()):
            module = item if isinstance(item, ModuleSurvey) else None
            if module is None and isinstance(item, Mapping):
                module = ModuleSurvey.from_dict(item)
            if module is not None:
                collected.append(module)
        collected.sort(key=lambda module: module.path)
        object.__setattr__(self, "modules", tuple(collected))
        stats = self.stats if isinstance(self.stats, SurveyStats) else None
        if stats is None and isinstance(self.stats, Mapping):
            stats = SurveyStats.from_dict(self.stats)
        object.__setattr__(self, "stats", stats if stats is not None else SurveyStats())

    def symbol_count(self) -> int:
        """Total symbols retained across every module in the survey."""

        return sum(module.symbol_count() for module in self.modules)

    def to_dict(self) -> dict:
        """JSON-safe projection of the report."""

        return {
            "modules": [module.to_dict() for module in self.modules],
            "stats": self.stats.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SurveyReport:
        """Rebuild a report from stored data, skipping malformed modules."""

        data = payload if isinstance(payload, Mapping) else {}
        modules = data.get("modules") or ()
        return cls(
            modules=tuple(item for item in modules if isinstance(item, (ModuleSurvey, Mapping))),
            stats=SurveyStats.from_dict(data.get("stats") or {}),
        )

    def to_summary(self) -> dict:
        """Log-safe summary: module/symbol counts and bounds only."""

        return {
            "modules": len(self.modules),
            "symbols": self.symbol_count(),
            **{
                key: value
                for key, value in self.stats.to_summary().items()
                if key in {"scanned_files", "parsed_files", "skipped_files", "truncated_files"}
            },
        }


# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


def _node_end(node: ast.AST) -> int:
    """End line of an AST node, falling back to its start line."""

    return _line(getattr(node, "end_lineno", None), default=_line(getattr(node, "lineno", 0)))


def _symbols_from_tree(path: str, tree: ast.AST) -> list[SymbolInfo]:
    """Qualified symbols of one parsed module, in source order."""

    symbols: list[SymbolInfo] = []

    def visit(container: ast.AST, prefix: str, parent: str) -> None:
        for child in ast.iter_child_nodes(container):
            if isinstance(child, ast.ClassDef):
                qualified = f"{prefix}{child.name}"
                symbols.append(
                    SymbolInfo(
                        path=path,
                        kind="class",
                        name=child.name,
                        qualname=qualified,
                        start_line=_line(child.lineno),
                        end_line=_node_end(child),
                        parent=parent,
                    )
                )
                visit(child, f"{qualified}.", qualified)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}{child.name}"
                kind = "method" if isinstance(container, ast.ClassDef) else "function"
                symbols.append(
                    SymbolInfo(
                        path=path,
                        kind=kind,
                        name=child.name,
                        qualname=qualified,
                        start_line=_line(child.lineno),
                        end_line=_node_end(child),
                        parent=parent,
                    )
                )
                visit(child, f"{qualified}.", qualified)
            else:
                visit(child, prefix, parent)

    visit(tree, "", "")
    return symbols


def _imports_from_tree(tree: ast.AST) -> tuple[str, ...]:
    """Imported module names (``pkg.mod`` / ``pkg.mod.name``), deduped."""

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base and node.level:
                base = "." * int(node.level) + base
            elif node.level:
                base = "." * int(node.level)
            for alias in node.names:
                if alias.name == "*":
                    names.append(base or "*")
                elif not base:
                    names.append(alias.name)
                elif set(base) == {"."}:
                    # ``from . import sibling`` -> ``.sibling`` (no double dot).
                    names.append(f"{base}{alias.name}")
                else:
                    names.append(f"{base}.{alias.name}")
    unique = _dedupe(names, limit=MAX_IMPORTS)
    return tuple(sorted(unique))


def _collect_source(
    path: str,
    source: Any,
    *,
    module: str = "",
    max_symbols_per_file: int = DEFAULT_MAX_SYMBOLS_PER_FILE,
) -> tuple[ModuleSurvey | None, bool]:
    """Parse one source string into a module survey plus a truncation flag."""

    text = source if isinstance(source, str) else ""
    if not text.strip():
        return None, False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None, False
    symbol_cap = _clamp(
        max_symbols_per_file, default=DEFAULT_MAX_SYMBOLS_PER_FILE, maximum=MAX_SYMBOLS_LIMIT
    )
    symbols = _symbols_from_tree(path, tree)
    truncated = symbol_cap > 0 and len(symbols) > symbol_cap
    if symbol_cap <= 0:
        symbols = []
    else:
        symbols = symbols[:symbol_cap]
    survey = ModuleSurvey(
        path=path,
        module=module or _module_name(PurePosixPath(path)),
        docstring=ast.get_docstring(tree, clean=True) or "",
        symbols=tuple(symbols),
        imports=_imports_from_tree(tree),
    )
    return survey, truncated


def survey_source(
    path: Any,
    source: Any,
    *,
    module: str = "",
    max_symbols_per_file: int = DEFAULT_MAX_SYMBOLS_PER_FILE,
) -> ModuleSurvey | None:
    """Survey one Python source string; ``None`` when it cannot be parsed.

    This is the pure, file-system-free entry point: callers that already hold
    source text (tests, the attempt chain) never need a working tree. Whitespace
    only or unparsable input yields ``None`` instead of an empty module, so a
    syntax error is never silently recorded as a module with no symbols.
    """

    survey, _truncated = _collect_source(
        _text(path),
        source,
        module=_text(module),
        max_symbols_per_file=max_symbols_per_file,
    )
    return survey


# ---------------------------------------------------------------------------
# Repository walking
# ---------------------------------------------------------------------------


def _collect_python_files(
    root: Path,
    *,
    include_tests: bool,
    max_candidates: int,
) -> tuple[list[PurePosixPath], int, int]:
    """Deterministically collect candidate module paths under ``root``.

    Returns ``(relative_paths, skipped_directory_count, limited_candidates)``.
    Directory and file names are sorted before descending, so truncation at
    ``max_candidates`` is deterministic rather than file-system dependent.
    """

    candidates: list[PurePosixPath] = []
    skipped_dirs = 0
    limited = 0
    try:
        walker: Iterator[tuple[str, list[str], list[str]]] = os.walk(root)
        for dirpath, dirnames, filenames in walker:
            kept = [name for name in dirnames if not _skip_dir(name)]
            skipped_dirs += len(dirnames) - len(kept)
            dirnames[:] = sorted(kept)
            for filename in sorted(filenames):
                if not filename.lower().endswith(".py"):
                    continue
                absolute = Path(dirpath, filename)
                try:
                    relative = PurePosixPath(absolute.relative_to(root).as_posix())
                except ValueError:
                    continue
                if not include_tests and _is_test_path(relative):
                    continue
                if len(candidates) >= max_candidates:
                    limited += 1
                else:
                    candidates.append(relative)
            if len(candidates) >= max_candidates:
                # Stop walking once the deterministic candidate bound is hit.
                break
    except OSError:
        pass
    candidates.sort()
    return candidates, skipped_dirs, limited


def survey_report(
    root: Any,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_symbols_per_file: int = DEFAULT_MAX_SYMBOLS_PER_FILE,
    include_tests: bool = False,
) -> SurveyReport:
    """Survey a working tree and return modules plus bounded counters.

    ``max_files`` bounds how many modules are retained, ``max_symbols_per_file``
    bounds symbols per module and ``include_tests`` opts back in to conventional
    test files. Unreadable, oversized and unparsable files are skipped and
    counted in :attr:`SurveyReport.stats`; the function never raises for a
    missing root, a non-directory root or a malformed file.
    """

    root_path = Path(root)
    empty = SurveyReport()
    if not root_path.is_dir():
        return empty
    file_cap = _clamp(max_files, default=DEFAULT_MAX_FILES, maximum=MAX_FILES_LIMIT)
    symbol_cap = _clamp(
        max_symbols_per_file, default=DEFAULT_MAX_SYMBOLS_PER_FILE, maximum=MAX_SYMBOLS_LIMIT
    )
    candidates, skipped_dirs, limited = _collect_python_files(
        root_path,
        include_tests=include_tests,
        max_candidates=min(MAX_CANDIDATE_FILES, max(file_cap, MAX_CANDIDATE_FILES)),
    )
    selected = candidates[:file_cap]
    limited += max(0, len(candidates) - len(selected))
    modules: list[ModuleSurvey] = []
    skipped = 0
    truncated = 0
    for relative in selected:
        absolute = root_path / relative.as_posix()
        try:
            if absolute.stat().st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            source = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        survey, was_truncated = _collect_source(
            relative.as_posix(),
            source,
            module=_module_name(relative),
            max_symbols_per_file=symbol_cap,
        )
        if survey is None:
            skipped += 1
            continue
        if was_truncated:
            truncated += 1
        modules.append(survey)
    modules.sort(key=lambda module: module.path)
    return SurveyReport(
        modules=tuple(modules),
        stats=SurveyStats(
            scanned_files=len(candidates),
            parsed_files=len(modules),
            skipped_files=skipped,
            skipped_dirs=skipped_dirs,
            truncated_files=truncated,
            limited_files=limited,
        ),
    )


def repo_survey(
    root: Any,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_symbols_per_file: int = DEFAULT_MAX_SYMBOLS_PER_FILE,
    include_tests: bool = False,
) -> tuple[ModuleSurvey, ...]:
    """Survey a repository working tree: deterministic modules, bounded sizes.

    Thin wrapper over :func:`survey_report` that returns only the module
    observations; use the report form when skip/limit counters are needed.
    """

    return survey_report(
        root,
        max_files=max_files,
        max_symbols_per_file=max_symbols_per_file,
        include_tests=include_tests,
    ).modules


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _module_like(value: Any) -> ModuleSurvey | None:
    """Coerce one value into a :class:`ModuleSurvey` when it looks like one."""

    if isinstance(value, ModuleSurvey):
        return value
    if isinstance(value, Mapping) and ("path" in value or "module" in value):
        return ModuleSurvey.from_dict(value)
    return None


def _as_modules(survey: Any) -> tuple[ModuleSurvey, ...]:
    """Best-effort module tuple for a survey, report, mapping or iterable."""

    if survey is None:
        return ()
    if isinstance(survey, SurveyReport):
        return tuple(survey.modules)
    single = _module_like(survey)
    if single is not None:
        return (single,)
    if isinstance(survey, Mapping):
        contained = survey.get("modules")
        if contained is None or isinstance(contained, (int, float)):
            return ()
        if isinstance(contained, (str, bytes)):
            return ()
        return tuple(
            module for module in (_module_like(item) for item in contained) if module is not None
        )
    if isinstance(survey, (str, bytes)) or not isinstance(survey, Iterable):
        return ()
    return tuple(
        module for module in (_module_like(item) for item in survey) if module is not None
    )


def summarize_survey(survey: Any) -> dict:
    """Log-safe module/symbol/skip counts of any survey shape.

    Accepts ``None``, a :class:`SurveyReport`, a :class:`ModuleSurvey`, an
    iterable of module surveys, or a mapping of pre-computed counts (keys
    ``modules``/``symbols``/``files``/``skipped``). Never returns module paths
    or source text, so the result may be logged or attached to an event.
    """

    summary = {"modules": 0, "symbols": 0, "files": 0, "skipped": 0}
    if survey is None:
        return summary
    if isinstance(survey, SurveyReport):
        return {
            "modules": len(survey.modules),
            "symbols": survey.symbol_count(),
            "files": survey.stats.scanned_files,
            "skipped": survey.stats.skipped_files,
        }
    if isinstance(survey, Mapping) and isinstance(
        survey.get("modules"), (int, float)
    ) and not isinstance(survey.get("modules"), bool):
        return {
            "modules": _line(survey.get("modules")),
            "symbols": _line(survey.get("symbols")),
            "files": _line(survey.get("files")),
            "skipped": _line(survey.get("skipped")),
        }
    modules = _as_modules(survey)
    summary["modules"] = len(modules)
    summary["symbols"] = sum(module.symbol_count() for module in modules)
    summary["files"] = len(modules)
    return summary


def survey_digest(survey: Any) -> str:
    """Stable SHA-256 digest of a survey's module/symbol/import projection.

    Only observations participate: module paths, names, bounded docstring
    headlines, symbol locations and import names. Statistics counters are
    excluded so the digest identifies the surveyed tree, not the traversal
    budget used to read it.
    """

    return digest([module.to_dict() for module in _as_modules(survey)])
