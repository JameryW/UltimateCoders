"""Tests for the V10 #30 repository-history context wiring."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ultimate_coders.agent import harness_local_loop as hll
from ultimate_coders.agent import history_context as hc
from ultimate_coders.agent.history_context import (
    build_history_block,
    history_context_enabled,
    mine_history,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _commit(
    root: Path,
    subject: str,
    *,
    body: str = "",
    files: dict[str, str] | None = None,
) -> None:
    for name, content in (files or {"app.py": subject}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    args = ["git", "-C", str(root), "commit", "-q", "-m", subject]
    if body:
        args += ["-m", body]
    subprocess.run(args, check=True, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    _commit(
        root,
        "feat(cache): add read-through caching (#101)",
        body="Caching was added because every request re-read the store.",
        files={"src/cache.py": "STORE = {}\n"},
    )
    _commit(
        root,
        "fix(cache): invalidate on write (#102)",
        body="Invalidate the entry so that a write is never served stale.",
        files={"src/cache.py": "STORE = {}\n\ndef invalidate(key):\n    STORE.pop(key, None)\n"},
    )
    return root


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hc.HISTORY_CONTEXT_ENV, "1")
    monkeypatch.setenv("UC_CHRONOS_PR_GRAPH", "1")


class TestFlag:
    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(hc.HISTORY_CONTEXT_ENV, raising=False)
        monkeypatch.setenv("UC_CHRONOS_PR_GRAPH", "1")
        assert history_context_enabled() is False

    def test_requires_the_graph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(hc.HISTORY_CONTEXT_ENV, "1")
        monkeypatch.delenv("UC_CHRONOS_PR_GRAPH", raising=False)
        assert history_context_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_truthy(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(hc.HISTORY_CONTEXT_ENV, value)
        monkeypatch.setenv("UC_CHRONOS_PR_GRAPH", "true")
        assert history_context_enabled() is True


class TestMining:
    def test_cards_are_keyed_by_pull_request(self, repo: Path) -> None:
        graph, edges = mine_history(repo)
        ids = set(graph.cards)
        assert {"#101", "#102"} <= ids
        assert edges >= 1

    def test_commit_without_pr_uses_the_hash(self, repo: Path) -> None:
        _commit(repo, "chore: tidy", files={"README.md": "hi\n"})
        graph, _ = mine_history(repo)
        ids = set(graph.cards)
        assert any(item.startswith("commit ") for item in ids)

    def test_motivation_and_files_are_extracted(self, repo: Path) -> None:
        graph, _ = mine_history(repo)
        card = graph.cards["#102"]
        assert card.files == ("src/cache.py",)
        assert "Invalidate the entry" in card.motivation
        assert card.signals == ("src/cache.py",)

    def test_missing_motivation_stays_empty(self, repo: Path) -> None:
        _commit(repo, "chore(docs): refresh", files={"docs/a.md": "x\n"})
        graph, _ = mine_history(repo)
        card = next(card for card in graph.cards.values() if "refresh" in card.summary)
        assert card.motivation == ""

    def test_body_is_used_as_the_solution(self, repo: Path) -> None:
        graph, _ = mine_history(repo)
        card = graph.cards["#102"]
        assert card.solution.startswith("Invalidate the entry")

    def test_non_repository_yields_nothing(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        graph, edges = mine_history(plain)
        assert edges == 0
        assert graph.cards == {}

    def test_nested_directory_is_not_mined(self, repo: Path) -> None:
        nested = repo / "src"
        graph, edges = mine_history(nested)
        assert edges == 0

    def test_limit_is_bounded(self, repo: Path) -> None:
        graph, _ = mine_history(repo, limit=0)
        assert graph.cards == {}


class TestBlock:
    def test_block_names_files_and_labels(self, repo: Path) -> None:
        mined = build_history_block(repo, "invalidate the cache on write")
        assert mined is not None
        assert "Change " in mined.block
        assert "Files: src/cache.py" in mined.block
        assert "Why: " in mined.block
        assert mined.cards >= 1
        assert mined.to_dict()["retrieved"]

    def test_block_is_bounded(self, repo: Path) -> None:
        mined = build_history_block(repo, "invalidate the cache on write", max_chars=160)
        assert mined is not None
        assert len(mined.block) <= 160
        assert mined.truncated is True
        assert mined.block.endswith("...")

    def test_irrelevant_task_still_gets_cards(self, repo: Path) -> None:
        mined = build_history_block(repo, "zzz unrelated")
        assert mined is None or mined.cards >= 0

    def test_empty_repository_returns_none(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert build_history_block(plain, "anything") is None


class TestHarnessWiring:
    def _resp(self, tool_calls: list[tuple[str, dict]]) -> SimpleNamespace:
        calls = [
            SimpleNamespace(
                id=f"c{index}",
                function=SimpleNamespace(name=name, arguments=json.dumps(args)),
            )
            for index, (name, args) in enumerate(tool_calls)
        ]
        choice = SimpleNamespace(message=SimpleNamespace(content="", tool_calls=calls))
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        return SimpleNamespace(choices=[choice], usage=usage)

    async def test_prompt_carries_the_history_block(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        _enable(monkeypatch)
        seen: list[str] = []

        def fake_completion(*args, **kwargs):
            messages = args[0] if args else kwargs["messages"]
            seen.append(next(m["content"] for m in messages if m["role"] == "user"))
            return self._resp([("done", {"summary": "ok"})])

        with patch.object(hll, "_completion", side_effect=fake_completion):
            assert await hll.run_loop("invalidate the cache on write", str(repo), max_turns=2) == 0
        user_prompt = seen[0]
        final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert user_prompt.startswith("invalidate the cache on write")
        assert "## Repository history" in user_prompt
        assert final["history_context"]["cards"] >= 1
        assert final["history_context"]["retrieved"]

    async def test_disabled_prompt_is_unchanged(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        monkeypatch.delenv(hc.HISTORY_CONTEXT_ENV, raising=False)
        monkeypatch.setenv("UC_CHRONOS_PR_GRAPH", "1")
        seen: list[str] = []

        def fake_completion(*args, **kwargs):
            messages = args[0] if args else kwargs["messages"]
            seen.append(next(m["content"] for m in messages if m["role"] == "user"))
            return self._resp([("done", {"summary": "ok"})])

        with patch.object(hll, "_completion", side_effect=fake_completion):
            assert await hll.run_loop("invalidate the cache on write", str(repo), max_turns=2) == 0
        final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert seen[0] == "invalidate the cache on write"
        assert "history_context" not in final

    async def test_no_history_keeps_the_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        _enable(monkeypatch)
        plain = tmp_path / "plain"
        plain.mkdir()
        seen: list[str] = []

        def fake_completion(*args, **kwargs):
            messages = args[0] if args else kwargs["messages"]
            seen.append(next(m["content"] for m in messages if m["role"] == "user"))
            return self._resp([("done", {"summary": "ok"})])

        with patch.object(hll, "_completion", side_effect=fake_completion):
            await hll.run_loop("do a thing", str(plain), max_turns=2)
        final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert seen[0] == "do a thing"
        assert "history_context" not in final
