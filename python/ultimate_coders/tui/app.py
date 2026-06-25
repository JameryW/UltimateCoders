"""Main Textual application for the UltimateCoders sandbox TUI.

Provides a full terminal UI with:
- Top: Logo header with ASCII art + version
- Main area: ChatLog (left, wide) + SubtaskTree (right, narrow)
- Bottom: Task input field + status bar

The TUI integrates with the thin Orchestrator bridge which delegates
to the omp UCOrchestrator subprocess. Events from omp are forwarded
to the Python event_emitter for real-time TUI updates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Input

from ultimate_coders.agent.event_emitter import TaskEventEmitter
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.sandbox import SandboxConfig
from ultimate_coders.agent.types import SubtaskStatus, WorkerInfo
from ultimate_coders.tui.widgets import (
    ChatLog,
    LogoHeader,
    StatusBar,
    SubtaskTree,
    TaskInput,
)

logger = logging.getLogger(__name__)


class SandboxTUI(App):
    """Textual TUI for the UltimateCoders sandbox mode.

    Launch with:
        python scripts/run_sandbox.py --tui "Fix the bug in main.rs"
        python scripts/run_sandbox.py --tui

    The TUI creates a thin Orchestrator bridge which delegates to the
    omp UCOrchestrator subprocess. It subscribes to the event emitter
    and updates the subtask tree and chat log in real-time.
    """

    TITLE = "UltimateCoders Sandbox"

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-area {
        height: 1fr;
    }

    ChatLog {
        width: 2fr;
    }

    SubtaskTree {
        width: 1fr;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
    ]

    # Reactive state for the current task
    current_task_id: reactive[str] = reactive("")
    subtask_count: reactive[int] = reactive(0)
    completed_count: reactive[int] = reactive(0)
    failed_count: reactive[int] = reactive(0)

    def __init__(
        self,
        config: SandboxConfig,
        initial_task: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the TUI app.

        Args:
            config: SandboxConfig for Orchestrator setup.
            initial_task: Optional task description to auto-submit on start.
            **kwargs: Additional Textual App keyword arguments.
        """
        super().__init__(**kwargs)
        self._config = config
        self._initial_task = initial_task
        self._orch: Orchestrator | None = None
        self._event_listener_handle: asyncio.Task | None = None
        self._status_poll_handle: asyncio.Task | None = None
        # Track subtask index mapping for display
        self._subtask_index: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        """Build the TUI layout."""
        yield LogoHeader()
        with Horizontal(id="main-area"):
            yield ChatLog(id="chat-log")
            yield SubtaskTree(id="subtask-tree")
        yield TaskInput(id="task-input")
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        """Initialize Orchestrator and start background tasks."""
        self._setup_orchestrator()

        # Configure status bar
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.configure("omp-bridge", self._config.backend)

        # Start event listener
        if self._orch:
            self._event_listener_handle = asyncio.create_task(
                self._listen_for_events()
            )

        # Auto-submit initial task if provided
        if self._initial_task:
            asyncio.create_task(self._submit_task(self._initial_task))

    def _setup_orchestrator(self) -> None:
        """Create the thin Orchestrator bridge.

        omp handles subtask execution — no Python Worker needed.
        """
        self._orch = Orchestrator(engine=None)

    async def _listen_for_events(self) -> None:
        """Background task that listens to the event emitter and updates the TUI.

        Events come from omp via OmpBridge.on_event → event_emitter.
        """
        if self._orch is None:
            return

        emitter: TaskEventEmitter = self._orch.event_emitter
        chat_log = self.query_one("#chat-log", ChatLog)
        subtask_tree = self.query_one("#subtask-tree", SubtaskTree)

        while True:
            try:
                event = await emitter.wait_for_event(timeout=2.0)
                if event is None:
                    continue

                event_type = event.type
                data = event.data
                subtask_id = event.subtask_id

                if event_type == "task_submitted":
                    chat_log.append(
                        f"Task submitted: {data.get('description', '')}",
                        style="bold",
                    )

                elif event_type == "task_decomposed":
                    count = data.get("subtask_count", 0)
                    waves = data.get("wave_count", 0)
                    chat_log.append(
                        f"Decomposed into {count} subtasks, {waves} wave(s)",
                        style="cyan",
                    )
                    # Populate subtask tree from synced task state
                    self._populate_subtask_tree()

                elif event_type == "wave_started":
                    wave = data.get("wave", 0)
                    total = data.get("total_waves", 0)
                    chat_log.append(
                        f"Wave {wave}/{total} started",
                        style="cyan",
                    )
                    # Mark subtasks as in-progress
                    for st_id in data.get("subtask_ids", []):
                        subtask_tree.update_subtask_status(
                            st_id, SubtaskStatus.IN_PROGRESS
                        )

                elif event_type == "subtask_started":
                    desc = data.get("description", subtask_id[:8] if subtask_id else "")
                    chat_log.append(
                        f"Subtask started: {desc}",
                        style="cyan",
                    )
                    if subtask_id:
                        subtask_tree.update_subtask_status(
                            subtask_id, SubtaskStatus.IN_PROGRESS
                        )

                elif event_type == "subtask_completed":
                    chat_log.append(
                        f"Subtask completed: {data.get('summary', '')[:80]}",
                        style="green",
                    )
                    if subtask_id:
                        subtask_tree.update_subtask_status(
                            subtask_id, SubtaskStatus.COMPLETED
                        )
                    self._update_progress()

                elif event_type == "subtask_failed":
                    chat_log.append(
                        f"Subtask failed: {data.get('error', 'unknown')[:80]}",
                        style="bold red",
                    )
                    if subtask_id:
                        subtask_tree.update_subtask_status(
                            subtask_id, SubtaskStatus.FAILED
                        )
                    self._update_progress()

                elif event_type == "task_completed":
                    status = data.get("status", "unknown")
                    if status == "completed":
                        chat_log.append("Task completed!", style="bold green")
                    else:
                        chat_log.append(
                            f"Task finished with status: {status}",
                            style="bold red",
                        )
                    self._update_progress()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Event listener error", exc_info=True)
                await asyncio.sleep(1)

    def _populate_subtask_tree(self) -> None:
        """Populate the subtask tree from the synced task state."""
        if self._orch is None or not self.current_task_id:
            return

        task = self._orch.tasks.get(self.current_task_id)
        if task is None or not task.subtasks:
            return

        subtask_tree = self.query_one("#subtask-tree", SubtaskTree)
        subtask_tree.set_task(task.description, task.id)
        self._subtask_index.clear()
        for i, st in enumerate(task.subtasks, start=1):
            subtask_tree.add_subtask(st, i)
            self._subtask_index[st.id] = i

        self.subtask_count = len(task.subtasks)
        subtask_tree.update_progress(0, len(task.subtasks))

    def _update_progress(self) -> None:
        """Recount completed/failed subtasks and update the tree + status bar."""
        if self._orch is None or not self.current_task_id:
            return

        task = self._orch.tasks.get(self.current_task_id)
        if task is None:
            return

        total = len(task.subtasks)
        completed = sum(1 for s in task.subtasks if s.is_complete)
        failed = sum(1 for s in task.subtasks if s.is_failed)
        self.completed_count = completed
        self.failed_count = failed
        self.subtask_count = total

        subtask_tree = self.query_one("#subtask-tree", SubtaskTree)
        subtask_tree.update_progress(completed, total)

        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.set_progress(completed, total)

    async def _submit_task(self, description: str) -> None:
        """Submit a task via the thin Orchestrator bridge.

        omp handles decomposition and execution. TUI just monitors events.
        """
        if self._orch is None:
            return

        chat_log = self.query_one("#chat-log", ChatLog)

        chat_log.append(f"Submitting task: {description}", style="bold")
        chat_log.append("Delegating to omp orchestrator...", style="dim")

        try:
            task = await self._orch.submit_task(
                description=description,
                project_id=self._config.project_path,
            )
        except Exception as e:
            chat_log.append(f"Task submission failed: {e}", style="bold red")
            return

        self.current_task_id = task.id

        if task.status.value == "failed":
            chat_log.append(
                f"Task failed: {task.result}",
                style="bold red",
            )
            return

        # If subtasks already synced from omp, populate tree immediately
        if task.subtasks:
            self._populate_subtask_tree()
            chat_log.append(
                f"Executing {len(task.subtasks)} subtasks via omp...",
                style="cyan",
            )
        else:
            chat_log.append(
                "Task submitted, waiting for decomposition...",
                style="dim",
            )

        # Start status polling to keep subtask tree in sync
        if self._status_poll_handle and not self._status_poll_handle.done():
            self._status_poll_handle.cancel()
        self._status_poll_handle = asyncio.create_task(self._poll_task_status())

    async def _poll_task_status(self) -> None:
        """Periodically sync task status from omp to keep TUI up-to-date.

        ponytail: 3s polling — events handle most updates, this is a safety net.
        """
        while True:
            try:
                await asyncio.sleep(3)
                if self._orch and self.current_task_id:
                    await self._orch._sync_task_from_omp(self.current_task_id)
                    self._update_progress()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Status poll error", exc_info=True)

    # ── Input handling ─────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the task input field."""
        description = event.value.strip()
        if not description:
            return

        event.input.value = ""

        chat_log = self.query_one("#chat-log", ChatLog)
        chat_log.clear_log()
        chat_log.append_user_input(description)

        # Cancel previous status poll
        if self._status_poll_handle and not self._status_poll_handle.done():
            self._status_poll_handle.cancel()
            self._status_poll_handle = None

        asyncio.create_task(self._submit_task(description))

    # ── Cleanup ────────────────────────────────────────────────────

    def on_unmount(self) -> None:
        """Cancel background tasks on shutdown."""
        if self._event_listener_handle and not self._event_listener_handle.done():
            self._event_listener_handle.cancel()
        if self._status_poll_handle and not self._status_poll_handle.done():
            self._status_poll_handle.cancel()
        # Close the OmpBridge subprocess
        if self._orch:
            asyncio.create_task(self._orch.close())
