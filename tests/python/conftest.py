"""Shared test fixtures for UltimateCoders Python tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


class StubEngine:
    """Pure-Python stub engine for testing without the Rust extension.

    Implements the same duck-typed API surface as the real Engine class,
    returning sensible defaults for every method.
    """

    def health(self) -> dict[str, Any]:
        return {"status": "healthy", "components": []}

    def search(self, query: Any, **kwargs: Any) -> Any:
        return MagicMock(items=[], total=0)

    def index_repo(self, **kwargs: Any) -> Any:
        return MagicMock(repo_id="test-repo", status="indexed")

    def get_index_state(self, repo_id: str = "", **kwargs: Any) -> Any:
        return MagicMock(repo_id=repo_id, state="ready")

    def remove_index(self, repo_id: str = "", **kwargs: Any) -> None:
        pass

    def read_memory(
        self, key_scope: str = "", key: str = "", **kwargs: Any
    ) -> Any:
        return None

    def write_memory(
        self, key_scope: str = "", key: str = "", content: str = "",
        **kwargs: Any
    ) -> Any:
        return True

    def delete_memory(
        self, key_scope: str = "", key: str = "", **kwargs: Any
    ) -> bool:
        return True

    def search_memory(self, **kwargs: Any) -> Any:
        return MagicMock(results=[], total=0)

    def batch_write_memory(self, entries: Any = None, **kwargs: Any) -> Any:
        return MagicMock(written=0)

    def list_repos(self, **kwargs: Any) -> list[Any]:
        return []

    def search_stream(self, query: Any = None, **kwargs: Any) -> list[Any]:
        return []

    def submit_task(self, **kwargs: Any) -> Any:
        return MagicMock(task_id="test-task-id", status="submitted")

    def get_task(self, task_id: str = "", **kwargs: Any) -> Any:
        return None

    def list_tasks(self, **kwargs: Any) -> list[Any]:
        return []

    def pause_task(self, task_id: str = "", **kwargs: Any) -> bool:
        return True

    def resume_task(self, task_id: str = "", **kwargs: Any) -> bool:
        return True


@pytest.fixture
def stub_engine() -> StubEngine:
    """Provide a StubEngine instance for tests."""
    return StubEngine()


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """Provide a mock LLM client that returns a basic response."""
    client = MagicMock()
    response = MagicMock()
    response.text = "Task completed successfully"
    response.tool_calls = []
    response.usage = MagicMock(input_tokens=10, output_tokens=20)
    client.complete.return_value = response
    client.complete_with_tools.return_value = (response, [])
    return client
