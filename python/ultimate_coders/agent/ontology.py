"""Project ontology loader over human-owned root CONTEXT.md (D9 / T19).

``UC_ONTOLOGY_MCP`` defaults **off**. When off, or when CONTEXT is missing /
unreadable, loaders soft-return an empty glossary (never raise for those
cases). Codegraph and Hy-Memory are **not** glossary authorities. No write /
delete / remember APIs live here — MCP tool registration is ticket 20.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: Opt-in ontology MCP / in-process glossary surface (T19 loader; T20 tools).
#: Default off — leave unset until operators enable read-only MCP advertising.
ONTOLOGY_MCP_ENV = "UC_ONTOLOGY_MCP"

#: Truthy env values shared with other UC feature flags.
_TRUTHY_ENV = frozenset({"1", "true", "yes", "on"})

#: Default authority path recorded on parsed terms (repo-relative).
DEFAULT_AUTHORITY_PATH = "CONTEXT.md"

#: Matt-format term header: ``**Term**:`` (optional trailing spaces).
_TERM_HEADER_RE = re.compile(r"^\*\*(?P<name>[^*]+)\*\*\s*:?\s*$")

#: Optional synonym reject line under a term.
_AVOID_RE = re.compile(r"^_Avoid_\s*:\s*(?P<body>.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class OntologyTerm:
    """One glossary entry from human-owned CONTEXT.md."""

    name: str
    definition: str
    avoid: tuple[str, ...] = ()
    authority_path: str = DEFAULT_AUTHORITY_PATH


def ontology_mcp_enabled() -> bool:
    """Whether ontology MCP / loader consumers may run; default off."""
    raw = os.environ.get(ONTOLOGY_MCP_ENV, "").strip().lower()
    return raw in _TRUTHY_ENV


def _default_context_path() -> Path:
    """Prefer CWD CONTEXT.md, else the nearest ancestor that has one."""
    cwd_candidate = Path.cwd() / DEFAULT_AUTHORITY_PATH
    if cwd_candidate.is_file():
        return cwd_candidate
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / DEFAULT_AUTHORITY_PATH
        if candidate.is_file():
            return candidate
    return cwd_candidate


def _split_avoid(body: str) -> tuple[str, ...]:
    parts = [p.strip() for p in body.split(",")]
    return tuple(p for p in parts if p)


def parse_context_markdown(
    text: str,
    *,
    authority_path: str = DEFAULT_AUTHORITY_PATH,
) -> list[OntologyTerm]:
    """Parse Matt-format Language terms from CONTEXT markdown.

    Malformed / incomplete term blocks are skipped; never raises for drift.
    """
    if not text or not text.strip():
        return []

    terms: list[OntologyTerm] = []
    current_name: str | None = None
    definition_lines: list[str] = []
    avoid: tuple[str, ...] = ()

    def flush() -> None:
        nonlocal current_name, definition_lines, avoid
        if current_name is None:
            return
        definition = " ".join(
            line.strip() for line in definition_lines if line.strip()
        ).strip()
        if definition:
            terms.append(
                OntologyTerm(
                    name=current_name,
                    definition=definition,
                    avoid=avoid,
                    authority_path=authority_path,
                )
            )
        current_name = None
        definition_lines = []
        avoid = ()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            # New section / title — close any open term.
            flush()
            continue
        header = _TERM_HEADER_RE.match(stripped)
        if header:
            flush()
            current_name = header.group("name").strip()
            if not current_name:
                current_name = None
            continue
        if current_name is None:
            continue
        avoid_match = _AVOID_RE.match(stripped)
        if avoid_match:
            avoid = _split_avoid(avoid_match.group("body"))
            continue
        if stripped:
            definition_lines.append(stripped)

    flush()
    return terms


def load_ontology(
    *,
    path: str | Path | None = None,
    enabled: bool | None = None,
) -> list[OntologyTerm]:
    """Load glossary terms when the flag is on and CONTEXT is readable.

    Soft-empty (``[]``) when the flag is off, the file is missing, or the file
    cannot be read. Does not raise for those soft cases.
    """
    if enabled is None:
        enabled = ontology_mcp_enabled()
    if not enabled:
        return []

    context_path = Path(path) if path is not None else _default_context_path()
    try:
        text = context_path.read_text(encoding="utf-8")
    except OSError:
        return []

    authority = (
        DEFAULT_AUTHORITY_PATH
        if path is None
        else Path(path).name or DEFAULT_AUTHORITY_PATH
    )
    try:
        return parse_context_markdown(text, authority_path=authority)
    except Exception:
        # Soft-empty on unexpected parse failures — never crash callers.
        return []


def list_terms(
    *,
    path: str | Path | None = None,
    enabled: bool | None = None,
) -> list[OntologyTerm]:
    """Return all loaded terms (thin wrapper over :func:`load_ontology`)."""
    return load_ontology(path=path, enabled=enabled)


def lookup_term(
    name: str,
    *,
    path: str | Path | None = None,
    enabled: bool | None = None,
    terms: Iterable[OntologyTerm] | None = None,
) -> OntologyTerm | None:
    """Case-insensitive lookup; preserves canonical casing from CONTEXT.

    Soft-returns ``None`` when disabled, missing, or not found.
    """
    needle = (name or "").strip()
    if not needle:
        return None
    catalogue = (
        list(terms)
        if terms is not None
        else load_ontology(path=path, enabled=enabled)
    )
    lowered = needle.lower()
    for term in catalogue:
        if term.name.lower() == lowered:
            return term
    return None
