"""The documentation's own pointers must resolve.

A page is a *contract with the reader's next click*: a relative link that 404s, a
`$skill` that no longer exists, or a guard script that was renamed all read as
"this page is current" while sending the reader nowhere. The repository already
guards reference *line numbers* (`test_check_spec_refs.py`) and the governance
docs' *symbol names* (`test_governance_docs_consistency.py`); this file covers the
remaining pointer kinds, across every tracked page:

1. relative markdown links (`[x](path)`), resolved from the containing file;
2. `$skill-name` references, resolved to `.agents/skills/<name>/SKILL.md`;
3. backticked repository paths under `scripts/`, `.github/workflows/`, `docs/`,
   `.agents/`, `python/`, `crates/`, `dashboard/`, `tests/`;
4. the one hand-maintained **count** in the workflow page (the pinned skill list
   in `scripts/check-codex-issue-flow.py`), because that number silently rots
   whenever a skill is added.

Skipped on purpose: absolute URLs, in-page anchors, template placeholders
(`<feature-slug>`), glob patterns (`*/SKILL.md`), and PowerShell's `$env:` syntax.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Pages whose pointers are checked: everything under docs/, plus the entry docs.
DOCS = sorted((REPO_ROOT / "docs").rglob("*.md")) + [
    REPO_ROOT / name for name in ("README.md", "AGENTS.md", "CLAUDE.md")
]

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SKILL_REF = re.compile(r"\$([a-z][a-z0-9-]{1,50})(?![:\w-])")
PATH_TOKEN = re.compile(
    r"`((?:scripts|docs|tests|python|crates|dashboard)/[A-Za-z0-9_./-]+"
    r"|\.github/workflows/[A-Za-z0-9_.-]+"
    r"|\.agents/skills/[A-Za-z0-9_./-]+)`"
)

SKILLS_DIR = REPO_ROOT / ".agents" / "skills"

#: Paths a page deliberately names before they exist. `docs/agents/domain.md` says
#: so in the same sentence ("created lazily by `/domain-modeling`"), so this is a
#: declaration, not a hole in the scan.
LAZY_PATHS = frozenset({"docs/adr/"})
CODEX_GUARD = REPO_ROOT / "scripts" / "check-codex-issue-flow.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _relative_links() -> list[tuple[Path, str]]:
    links: list[tuple[Path, str]] = []
    for doc in DOCS:
        for target in LINK.findall(_read(doc)):
            if "://" in target or target.startswith(("mailto:", "#")):
                continue
            if "<" in target or ">" in target:
                continue
            path = target.split("#", 1)[0]
            if path:
                links.append((doc, path))
    return links


def test_every_relative_markdown_link_resolves() -> None:
    missing = [
        (doc.relative_to(REPO_ROOT).as_posix(), target)
        for doc, target in _relative_links()
        if not (doc.parent / target).exists()
    ]
    assert not missing, (
        "documentation links that do not resolve (fix the link, or the file moved): "
        + ", ".join(f"{doc} -> {target}" for doc, target in missing)
    )


def test_no_page_links_to_an_absolute_filesystem_path() -> None:
    """A link into `C:\\…` or `/home/…` only works on the machine that wrote it."""
    broken: list[tuple[str, str]] = []
    for doc, target in _relative_links():
        if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", target):
            broken.append((doc.relative_to(REPO_ROOT).as_posix(), target))
    assert not broken, f"absolute filesystem links: {broken}"


def test_every_skill_reference_resolves() -> None:
    known = {path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md")}
    referenced: dict[str, set[str]] = {}
    for doc in DOCS:
        for name in SKILL_REF.findall(_read(doc)):
            referenced.setdefault(name, set()).add(doc.relative_to(REPO_ROOT).as_posix())
    unresolved = {name: sorted(docs) for name, docs in referenced.items() if name not in known}
    assert not unresolved, (
        "docs reference skills that do not exist under .agents/skills/ (add the skill or "
        f"fix the name): {unresolved}"
    )


def test_every_backticked_repository_path_exists() -> None:
    known_roots = ("scripts/", "docs/", "tests/", "python/", "crates/", "dashboard/")
    missing: list[tuple[str, str]] = []
    for doc in DOCS:
        for token in PATH_TOKEN.findall(_read(doc)):
            if "*" in token:
                continue  # a glob is a pattern, not a claim that this exact file exists
            if token in LAZY_PATHS:
                continue
            if token.startswith(known_roots) or token.startswith((".github/", ".agents/")):
                if not (REPO_ROOT / token).exists():
                    missing.append((doc.relative_to(REPO_ROOT).as_posix(), token))
    assert not missing, (
        "docs name repository paths that do not exist (the file was renamed or removed): "
        f"{missing}"
    )


def _pinned_skill_count() -> int:
    spec = importlib.util.spec_from_file_location("_codex_issue_flow_guard", CODEX_GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return len(module.REQUIRED_SKILLS)


def test_the_documented_skill_count_matches_the_guard() -> None:
    """`codex-issue-flow.md` says "the 14 `.agents/skills/*/SKILL.md` entries"."""
    page = REPO_ROOT / "docs" / "workflows" / "codex-issue-flow.md"
    claimed = {
        int(value)
        for value in re.findall(r"the (\d+) `\.agents/skills/\*/SKILL\.md` entries", _read(page))
    }
    assert claimed, "the workflow page no longer states the pinned skill count"
    assert claimed == {_pinned_skill_count()}, (
        f"the page claims {sorted(claimed)} pinned skills, the guard pins "
        f"{_pinned_skill_count()}"
    )


def test_the_scan_is_not_vacuous() -> None:
    """A guard that reads nothing passes everything, so prove it read something."""
    assert len(DOCS) > 50, f"only {len(DOCS)} pages found under docs/ — the glob broke"
    assert len(_relative_links()) > 100, "almost no relative links parsed — the regex broke"
    skill_refs = {name for doc in DOCS for name in SKILL_REF.findall(_read(doc))}
    assert len(skill_refs) >= 5, f"skill references stopped parsing: {sorted(skill_refs)}"
    assert _pinned_skill_count() > 0
