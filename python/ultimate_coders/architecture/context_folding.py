"""ACP interval folding and verified-read protection for tool loops.

The v9 worker-context upgrade asks for three things from the local harness:

* fold consumed conversation intervals once the attempt's context reaches
  ~70% of its budget, instead of letting the prompt grow until the model
  truncates it;
* keep a checkpoint that names the files/hashes that were already read, so a
  fold does not lose what the agent has verified;
* block a duplicate ``read_file`` of an unchanged range, because those bytes
  would be paid for again in every subsequent request and reduce the
  prefix-cache hit rate.

The implementation is deliberately message-shape agnostic: it operates on the
plain dict messages used by the local harness.  It never mutates the caller's
list in place, never folds the protected head (system + original task), and
never leaves an assistant ``tool_calls`` message without its matching tool
results.  Everything is disabled unless ``UC_ACP_FOLDING`` is truthy.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CONTEXT_TOKENS = 8192
DEFAULT_FOLD_THRESHOLD = 0.70
DEFAULT_KEEP_TAIL_MESSAGES = 6
MIN_KEEP_TAIL_MESSAGES = 2
MAX_CHECKPOINTS = 64
SUMMARY_MAX_CHARS = 2400

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def folding_enabled() -> bool:
    """Feature flag: ACP interval folding (default off)."""

    return _env_flag("UC_ACP_FOLDING")


def folding_threshold() -> float:
    """Fold when estimated context usage reaches this share of the budget."""

    value = _env_float("UC_ACP_FOLD_THRESHOLD", DEFAULT_FOLD_THRESHOLD)
    return max(0.5, min(0.95, value))


def context_budget_tokens() -> int:
    """The attempt context budget the fold threshold is measured against."""

    raw = os.environ.get("UC_ACP_CONTEXT_TOKENS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TOKENS
    return max(512, min(131072, value))


def keep_tail_messages() -> int:
    """How many recent messages survive a fold."""

    try:
        value = int(os.environ.get("UC_ACP_KEEP_TAIL", str(DEFAULT_KEEP_TAIL_MESSAGES)))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_TAIL_MESSAGES
    return max(MIN_KEEP_TAIL_MESSAGES, min(32, value))


def estimate_messages_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """Estimate a message list's token count with a 4-chars/token heuristic."""

    total = 0
    for message in messages:
        total += 4  # role / framing overhead
        for value in message.values():
            if isinstance(value, str):
                total += estimate_text_tokens(value)
            elif isinstance(value, (list, tuple, dict)):
                try:
                    total += estimate_text_tokens(json.dumps(value, ensure_ascii=False))
                except (TypeError, ValueError):
                    total += 4
    return total


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return int(math.ceil(len(text) / 4.0))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    return ""


def _tools_in(message: Mapping[str, Any]) -> list[str]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for call in calls:
        function = call.get("function") if isinstance(call, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str):
            names.append(name)
    return names


@dataclass(frozen=True)
class VerifiedRead:
    """A read that has already been placed into the prompt.

    ``content_sha256`` lets a later read of the same range be blocked only when
    the file bytes are unchanged.  A changed file is a new fact and must not be
    hidden by the dedupe.
    """

    path: str
    offset: int
    length: int
    content_sha256: str
    turn: int

    def matches(self, path: str, offset: int, content: str) -> bool:
        return (
            self.path == path
            and self.offset == offset
            and self.length == len(content)
            and self.content_sha256 == _digest(content)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "offset": self.offset,
            "length": self.length,
            "content_sha256": self.content_sha256,
            "turn": self.turn,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> VerifiedRead:
        return cls(
            path=str(raw.get("path") or ""),
            offset=max(0, int(raw.get("offset") or 0)),
            length=max(0, int(raw.get("length") or 0)),
            content_sha256=str(raw.get("content_sha256") or ""),
            turn=max(0, int(raw.get("turn") or 0)),
        )


@dataclass
class ReadLedger:
    """Bounded per-attempt map of reads already added to the conversation."""

    max_entries: int = 128
    reads: dict[tuple[str, int], VerifiedRead] = field(default_factory=dict)
    blocked: int = 0
    _order: list[tuple[str, int]] = field(default_factory=list)

    def record(self, path: str, offset: int, content: str, *, turn: int) -> VerifiedRead:
        key = (path, offset)
        entry = VerifiedRead(
            path=path,
            offset=offset,
            length=len(content),
            content_sha256=_digest(content),
            turn=turn,
        )
        self.reads[key] = entry
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        while len(self._order) > self.max_entries:
            oldest = self._order.pop(0)
            self.reads.pop(oldest, None)
        return entry

    def duplicate(self, path: str, offset: int, content: str) -> bool:
        entry = self.reads.get((path, offset))
        if entry is not None and entry.matches(path, offset, content):
            self.blocked += 1
            return True
        return False

    def describe(self, *, limit: int = 20) -> list[dict[str, Any]]:
        entries = [entry.to_dict() for entry in self.reads.values()]
        entries.sort(key=lambda item: (item["path"], item["offset"]))
        return entries[:limit]

    def hashes(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                f"{entry.path}@{entry.offset}:{entry.content_sha256}"
                for entry in self.reads.values()
            )
        )


@dataclass(frozen=True)
class FoldCheckpoint:
    """A compact record of what a fold removed from the live prompt."""

    checkpoint_id: str
    turn: int
    folded_messages: int
    kept_messages: int
    saved_tokens: int
    read_hashes: tuple[str, ...]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "turn": self.turn,
            "folded_messages": self.folded_messages,
            "kept_messages": self.kept_messages,
            "saved_tokens": self.saved_tokens,
            "read_hashes": list(self.read_hashes),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FoldCheckpoint:
        hashes = raw.get("read_hashes")
        return cls(
            checkpoint_id=str(raw.get("checkpoint_id") or ""),
            turn=max(0, int(raw.get("turn") or 0)),
            folded_messages=max(0, int(raw.get("folded_messages") or 0)),
            kept_messages=max(0, int(raw.get("kept_messages") or 0)),
            saved_tokens=max(0, int(raw.get("saved_tokens") or 0)),
            read_hashes=tuple(str(item) for item in hashes) if isinstance(hashes, list) else (),
            summary=str(raw.get("summary") or ""),
        )


@dataclass(frozen=True)
class FoldResult:
    """Result of one fold attempt; ``checkpoint`` is None when no fold happened."""

    messages: list[dict[str, Any]]
    checkpoint: FoldCheckpoint | None = None
    folded_messages: int = 0
    saved_tokens: int = 0

    @property
    def folded(self) -> bool:
        return self.checkpoint is not None


class ACPFolder:
    """Fold consumed middle intervals while preserving head and recent tail.

    The fold boundary is chosen from the *end* of the conversation: the last
    ``keep_tail`` messages stay intact, and the range before them (after the
    protected head) is replaced with one deterministic summary message.  We do
    not fold when the tail ends in an assistant tool-call message or in a tool
    message without its assistant: that would create an invalid message
    sequence in the next completion.
    """

    def __init__(
        self,
        *,
        budget_tokens: int | None = None,
        threshold: float | None = None,
        keep_tail: int | None = None,
        max_checkpoints: int = MAX_CHECKPOINTS,
    ) -> None:
        raw_budget = budget_tokens if budget_tokens is not None else context_budget_tokens()
        self.budget_tokens = max(512, int(raw_budget))
        raw_threshold = threshold if threshold is not None else folding_threshold()
        threshold_val = float(raw_threshold)
        if not math.isfinite(threshold_val):
            threshold_val = DEFAULT_FOLD_THRESHOLD
        self.threshold = max(0.5, min(0.95, threshold_val))
        self.keep_tail = max(
            MIN_KEEP_TAIL_MESSAGES,
            keep_tail if keep_tail is not None else keep_tail_messages(),
        )
        self.max_checkpoints = max(1, max_checkpoints)
        self.checkpoints: list[FoldCheckpoint] = []

    def usage_ratio(self, messages: Sequence[Mapping[str, Any]]) -> float:
        return estimate_messages_tokens(messages) / float(self.budget_tokens)

    def should_fold(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        return self.usage_ratio(messages) >= self.threshold

    def maybe_fold(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        read_ledger: ReadLedger | None = None,
        turn: int = 0,
    ) -> FoldResult:
        if not folding_enabled() or not self.should_fold(messages):
            return FoldResult(messages=list(messages))
        return self.fold(messages, read_ledger=read_ledger, turn=turn)

    def fold(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        read_ledger: ReadLedger | None = None,
        turn: int = 0,
    ) -> FoldResult:
        snapshot = list(messages)
        if not snapshot:
            return FoldResult(messages=snapshot)
        if snapshot[-1].get("role") == "assistant" and _tools_in(snapshot[-1]):
            return FoldResult(messages=snapshot)
        head_len = self._head_length(snapshot)
        tail_start = max(head_len, len(snapshot) - self.keep_tail)
        tail_start = self._safe_tail_start(snapshot, tail_start)
        if tail_start <= head_len:
            return FoldResult(messages=snapshot)

        head = snapshot[:head_len]
        middle = snapshot[head_len:tail_start]
        tail = snapshot[tail_start:]
        if not middle:
            return FoldResult(messages=snapshot)
        if tail and tail[0].get("role") == "tool":
            return FoldResult(messages=snapshot)

        before_tokens = estimate_messages_tokens(snapshot)
        summary = self._summary(middle, tail=tail, read_ledger=read_ledger, turn=turn)
        folded = [
            *head,
            {"role": "system", "content": summary},
            *tail,
        ]
        after_tokens = estimate_messages_tokens(folded)
        checkpoint = FoldCheckpoint(
            checkpoint_id=f"fold-{turn}-{_digest(summary)}",
            turn=turn,
            folded_messages=len(middle),
            kept_messages=len(head) + len(tail),
            saved_tokens=max(0, before_tokens - after_tokens),
            read_hashes=read_ledger.hashes() if read_ledger is not None else (),
            summary=summary,
        )
        self.checkpoints.append(checkpoint)
        if len(self.checkpoints) > self.max_checkpoints:
            self.checkpoints = self.checkpoints[-self.max_checkpoints :]
        return FoldResult(
            messages=folded,
            checkpoint=checkpoint,
            folded_messages=len(middle),
            saved_tokens=checkpoint.saved_tokens,
        )

    def _head_length(self, messages: Sequence[Mapping[str, Any]]) -> int:
        # Keep the system prompt and the original task; they are the stable
        # prefix every request shares.  Never fold them on a later pass.
        length = 0
        for message in messages:
            if message.get("role") in ("system", "user"):
                length += 1
                continue
            break
        return max(1, min(length, 2))

    def _safe_tail_start(
        self, messages: Sequence[Mapping[str, Any]], tail_start: int
    ) -> int:
        # A kept tail may begin with a tool result only if its assistant call is
        # included too.  Move back to the preceding assistant message in that case.
        if 0 <= tail_start < len(messages) and messages[tail_start].get("role") == "tool":
            tail_start -= 1
            while tail_start > 0 and messages[tail_start].get("role") == "tool":
                tail_start -= 1
        return max(0, tail_start)

    def _summary(
        self,
        middle: Sequence[Mapping[str, Any]],
        *,
        tail: Sequence[Mapping[str, Any]],
        read_ledger: ReadLedger | None,
        turn: int,
    ) -> str:
        tools: list[str] = []
        writes: list[str] = []
        prior_folds = 0
        for message in middle:
            if message.get("role") == "system":
                content = _message_text(message)
                if content.startswith("[ACP fold]"):
                    prior_folds += 1
                    for line in content.splitlines():
                        if line.startswith("Tools used:"):
                            items = line.split(":", 1)[1].strip().rstrip(".")
                            if items and items != "none recorded":
                                tools.extend(
                                    part.strip() for part in items.split(",") if part.strip()
                                )
                        elif line.startswith("Files written:"):
                            items = line.split(":", 1)[1].strip().rstrip(".")
                            if items:
                                writes.extend(
                                    part.strip() for part in items.split(",") if part.strip()
                                )
            for name in _tools_in(message):
                tools.append(name)
            if message.get("role") == "tool":
                text = _message_text(message)
                try:
                    payload = json.loads(text)
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, Mapping):
                    if payload.get("written"):
                        writes.append(str(payload["written"]))
                    if payload.get("entries") and "list_dir" not in tools:
                        tools.append("list_dir")
        unique_tools = sorted(set(tools))
        unique_writes = sorted(set(writes))
        fold_note = f" (includes {prior_folds} prior fold(s))" if prior_folds else ""
        lines = [
            "[ACP fold] Earlier conversation was compacted to keep the context budget.",
            f"Turn {turn} checkpoint; folded {len(middle)} messages{fold_note}.",
            f"Tools used: {', '.join(unique_tools) if unique_tools else 'none recorded'}.",
        ]
        if unique_writes:
            lines.append(f"Files written: {', '.join(unique_writes[:12])}.")
        if read_ledger is not None:
            hashes = read_ledger.hashes()
            if hashes:
                lines.append("Verified reads: " + "; ".join(hashes[:16]))
            if read_ledger.blocked:
                lines.append(f"Duplicate reads blocked: {read_ledger.blocked}.")
        if tail:
            lines.append(
                "Recent turns are kept verbatim: "
                + ", ".join(str(item.get("role") or "?") for item in tail[-6:])
                + "."
            )
        lines.append(
            "Do not re-read a verified file unless its contents changed; use the summary "
            "and the recent tool results to continue."
        )
        return "\n".join(lines)[:SUMMARY_MAX_CHARS]


def fold_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    read_ledger: ReadLedger | None = None,
    turn: int = 0,
) -> FoldResult:
    """Convenience wrapper for callers that do not need a persistent folder."""

    return ACPFolder().maybe_fold(messages, read_ledger=read_ledger, turn=turn)


def available_checkpoints_json(checkpoints: Iterable[FoldCheckpoint]) -> list[dict[str, Any]]:
    return [checkpoint.to_dict() for checkpoint in checkpoints]
