"""Repository history context for the local harness (V10 #30).

The ledger item (**Chronos**, ``arXiv:2610.11578``) distills merged pull
requests into four-field experience cards - summary, signals, motivation,
solution - links them into an eleven-kind evolution graph, then retrieves a
handful of cards for the task at hand. The implementation lives in
``python/ultimate_coders/knowledge/chronos_pr_graph.py`` but nothing ever fed it
a repository, so it was dead weight.

This module is the missing mining step plus a prompt block for the local
harness. Everything comes from ``git``:

* one pass of ``git log -n <limit> --name-only`` yields each commit's subject,
  body, touched files and author, which is enough to build a card;
* commits that name a pull request (``(#123)``, the squash-merge shape) become
  cards keyed by that number; commits without one are keyed by their short hash
  so a plain-history repository still gets cards;
* ``motivation`` prefers body lines that explain *why* (``because``,
  ``so that``, ``to avoid``, ``fixes``...), falling back to the subject;
  ``solution`` is the first body paragraph, or the changed files when the body
  is empty;
* ``signals`` are the touched file paths, which is what the graph links on.

The graph then does exactly what the paper prescribes: search a few entry cards
for the task text, expand a few hops along the shared-file edges, cap the
result, and render one bounded block. Cards are advisory context, never
requirements.

Opt-in via ``UC_HISTORY_CONTEXT`` (and ``UC_CHRONOS_PR_GRAPH`` for the graph).
With either flag off the harness prompt is byte-identical, and a directory that
is not a git repository yields no block at all.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ultimate_coders.knowledge.chronos_pr_graph import (
    ChronosPRGraph,
    PRCard,
    chronos_pr_graph_enabled,
)

__all__ = [
    "HISTORY_CONTEXT_ENV",
    "MAX_LOG_COMMITS",
    "MAX_BLOCK_CHARS",
    "HistoryMining",
    "build_history_block",
    "history_context_enabled",
    "mine_history",
]

HISTORY_CONTEXT_ENV = "UC_HISTORY_CONTEXT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

MAX_LOG_COMMITS = 200
MAX_CARDS = 40
MAX_BLOCK_CHARS = 1400
_GIT_TIMEOUT_S = 20.0
_ENTRY_K = 4
_HOPS = 2
_MAX_RETRIEVED = 6

_FIELD = "\x1f"
_RECORD = "\x1e"
_PR_REF = re.compile(r"\(#(\d+)\)")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_WHY = (
    "because",
    "since",
    "so that",
    "to avoid",
    "fixes",
    "fixed",
    "avoids",
    "prevents",
    "otherwise",
)


def history_context_enabled() -> bool:
    """True when this mining step and the underlying graph are both on."""

    if os.environ.get(HISTORY_CONTEXT_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return chronos_pr_graph_enabled()


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _is_git_root(root: Path) -> bool:
    """True only when ``root`` is its own repository top level."""

    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if not toplevel or not toplevel.strip():
        return False
    try:
        resolved = Path(toplevel.strip()).resolve()
    except OSError:
        return False
    return os.path.normcase(str(resolved)) == os.path.normcase(str(root.resolve()))


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3].rstrip() + "..."


def _motivation(body_lines: list[str]) -> str:
    """Why the change was made, or empty when the commit does not say."""

    for line in body_lines:
        lowered = line.lower()
        if any(word in lowered for word in _WHY):
            return _clip(line.lstrip("-* ").strip(), 220)
    return ""


def _solution(body_lines: list[str], files: tuple[str, ...]) -> str:
    for line in body_lines:
        if line.strip() and not line.startswith(("#", "-", "*")):
            return _clip(line, 220)
    if files:
        return "changed " + ", ".join(files[:4])
    return ""


@dataclass(frozen=True)
class HistoryMining:
    """Cards mined from one repository plus the block rendered for a task."""

    cards: int
    edges: int
    block: str
    retrieved: tuple[str, ...]
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "cards": self.cards,
            "edges": self.edges,
            "retrieved": list(self.retrieved),
            "truncated": self.truncated,
        }


def _parse_log(raw: str) -> list[PRCard]:
    cards: list[PRCard] = []
    seen: set[str] = set()
    for chunk in raw.split(_RECORD):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split(_FIELD)
        if len(parts) < 4:
            continue
        commit, author, timestamp, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4] if len(parts) > 4 else ""
        body_lines = [line for line in body.splitlines() if line.strip()]
        # ``--name-only`` appends the touched files after a blank line.
        files: tuple[str, ...] = ()
        if body:
            tail = body.split("\n\n")[-1].strip().splitlines()
            candidate = tuple(
                line.strip()
                for line in tail
                if line.strip() and not line.startswith(("#", "-", "*", ">"))
            )
            if candidate and all("/" in item or "." in item for item in candidate):
                files = candidate
                body_lines = [line for line in body_lines if line.strip() not in candidate]
        match = _PR_REF.search(subject)
        card_id = f"#{match.group(1)}" if match else f"commit {commit[:8]}"
        if card_id in seen:
            continue
        seen.add(card_id)
        cards.append(
            PRCard(
                pr_id=card_id,
                summary=_clip(_PR_REF.sub("", subject).strip() or subject, 160),
                signals=tuple(files[:12]),
                motivation=_motivation(body_lines),
                solution=_solution(body_lines, files),
                files=tuple(files[:12]),
                authors=(_clip(author, 60),) if author.strip() else (),
                milestone=timestamp.strip(),
                timestamp=timestamp.strip(),
            )
        )
    return cards


def mine_history(root: Path | str, *, limit: int = MAX_LOG_COMMITS) -> tuple[ChronosPRGraph, int]:
    """Build the card graph from ``root``'s git history (empty when unusable)."""

    path = Path(root)
    graph = ChronosPRGraph()
    if limit <= 0:
        return graph, 0
    try:
        path = path.resolve()
    except OSError:
        return graph, 0
    if not path.is_dir() or not _is_git_root(path):
        return graph, 0
    # ``--name-only`` emits the touched files *after* the formatted record, so
    # the separator has to open each record; ending it would hand the next
    # commit's hash to a chunk that starts with a file path.
    raw = _git(
        path,
        "log",
        f"-n{max(1, min(limit, MAX_LOG_COMMITS))}",
        f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%cI{_FIELD}%s{_FIELD}%b",
        "--name-only",
    )
    if not raw:
        return graph, 0
    cards = _parse_log(raw)[:MAX_CARDS]
    for card in cards:
        graph.add_card(card)
    edges = graph.auto_link()
    return graph, edges


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(word for word in _WORD.findall(text.lower()) if len(word) >= 3)


def _entry_cards(graph: ChronosPRGraph, task: str) -> tuple[PRCard, ...]:
    """Rank entry cards by stem overlap.

    ``ChronosPRGraph.search`` matches whole words, so a task that says "cache"
    would miss a commit that says "caching".  Prefix matching on both sides
    keeps the retrieval usable on real commit subjects; the graph's own
    ``expand`` still supplies the hops.
    """

    wanted = _tokens(task)
    if not wanted:
        return ()
    scored: list[tuple[int, int, str, PRCard]] = []
    for card in graph.cards.values():
        words = _tokens(card.searchable())
        hits = sum(
            1
            for term in wanted
            if any(word.startswith(term[:6]) or term.startswith(word[:6]) for word in words)
        )
        if hits:
            # Ties break on file count (richer cards first), then id, so the
            # same repository always produces the same block.
            scored.append((hits, len(card.files), card.pr_id, card))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return tuple(item[3] for item in scored[:_ENTRY_K])

def _render(cards: tuple[PRCard, ...], max_chars: int) -> tuple[str, bool]:
    """Render retrieved cards, bounded; the graph's own block assumes PR ids."""

    parts: list[str] = []
    for card in cards:
        parts.append(
            "\n".join(
                (
                    f"Change {card.pr_id}: {card.summary}",
                    f"Files: {', '.join(card.files[:6]) or '-'}",
                    f"Why: {card.motivation or '-'}",
                    f"How: {card.solution or '-'}",
                )
            )
        )
    block = "\n\n".join(parts).strip()
    if len(block) <= max_chars:
        return block, False
    return block[: max_chars - 3].rstrip() + "...", True

def build_history_block(
    root: Path | str,
    task: str,
    *,
    limit: int = MAX_LOG_COMMITS,
    max_chars: int = MAX_BLOCK_CHARS,
) -> HistoryMining | None:
    """Render the bounded history block for ``task`` (``None`` when empty)."""

    graph, edges = mine_history(root, limit=limit)
    if not graph.cards:
        return None
    entries = _entry_cards(graph, task or "")
    if not entries:
        return None
    try:
        cards = graph.expand(entries, hops=_HOPS, max_cards=_MAX_RETRIEVED)
    except Exception:  # noqa: BLE001 - advisory context must never break a run
        return None
    if not cards:
        return None
    block, truncated = _render(cards, max_chars)
    if not block:
        return None
    return HistoryMining(
        cards=len(cards),
        edges=edges,
        block=block,
        retrieved=tuple(card.pr_id for card in cards),
        truncated=truncated,
    )
