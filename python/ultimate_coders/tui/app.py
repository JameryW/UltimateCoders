"""Main Textual application for the UltimateCoders sandbox TUI.

Provides a full terminal UI with:
- Top: Logo header with ASCII art + version
- Main area: ChatLog (left, wide) + SubtaskTree (right, narrow)
- Bottom: Task input field + status bar

The TUI integrates with the existing Orchestrator/Worker event system
via the TaskEventEmitter for real-time updates.
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
from ultimate_coders.agent.worker import Worker
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

    The TUI creates an Orchestrator + Worker internally, subscribes to
    the event emitter, and updates the subtask tree and chat log in
    real-time as tasks are decomposed and executed.
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
            config: SandboxConfig for Worker/Orchestrator setup.
            initial_task: Optional task description to auto-submit on start.
            **kwargs: Additional Textual App keyword arguments.
        """
        super().__init__(**kwargs)
        self._config = config
        self._initial_task = initial_task
        self._orch: Orchestrator | None = None
        self._worker: Worker | None = None
        self._execute_task_handle: asyncio.Task | None = None
        self._event_listener_handle: asyncio.Task | None = None
        # Track subtask index mapping for display
        self._subtask_index: dict[str, int] = {}
        # Event-driven dispatch: set when a subtask completes/fails, wakes _auto_execute_loop
        self._dispatch_event: asyncio.Event = asyncio.Event()

    def compose(self) -> ComposeResult:
        """Build the TUI layout."""
        yield LogoHeader()
        with Horizontal(id="main-area"):
            yield ChatLog(id="chat-log")
            yield SubtaskTree(id="subtask-tree")
        yield TaskInput(id="task-input")
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        """Initialize Orchestrator/Worker and start background tasks."""
        self._setup_orchestrator()

        # Configure status bar
        status_bar = self.query_one("#status-bar", StatusBar)
        if self._worker:
            status_bar.configure(self._worker.worker_id, self._config.backend)

        # Start event listener
        if self._orch:
            self._event_listener_handle = asyncio.create_task(
                self._listen_for_events()
            )

        # Auto-submit initial task if provided
        if self._initial_task:
            asyncio.create_task(self._submit_and_execute(self._initial_task))

    def _setup_orchestrator(self) -> None:
        """Create and configure Orchestrator + Worker."""
        from ultimate_coders.agent.sandbox import SandboxManager

        sandbox_manager = SandboxManager(self._config)

        self._orch = Orchestrator(
            engine=None,
            sandbox_manager=sandbox_manager,
        )

        self._worker = Worker(
            worker_id="local-sandbox-worker",
            engine=None,
            sandbox_config=self._config,
            event_emitter=self._orch.event_emitter,
        )

        worker_info = WorkerInfo(
            id=self._worker.worker_id,
            capabilities=["code", "search", "memory", "test"],
            current_load=0,
            max_capacity=3,
        )
        self._orch.workers[worker_info.id] = worker_info

    async def _listen_for_events(self) -> None:
        """Background task that listens to the event emitter and updates the TUI."""
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
                    subtask_count = data.get("subtask_count", 0)
                    chat_log.append(
                        f"Decomposed into {subtask_count} subtasks",
                        style="cyan",
                    )

                elif event_type == "subtask_started":
                    desc = data.get("description", subtask_id[:8])
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
                    self._dispatch_event.set()

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
                    self._dispatch_event.set()

                elif event_type == "task_completed":
                    status = data.get("status", "unknown")
                    if status == "completed":
                        chat_log.append("Task completed!", style="bold green")
                    else:
                        chat_log.append(
                            f"Task finished with status: {status}",
                            style="bold red",
                        )

                elif event_type == "tool_call":
                    tool = data.get("tool", "unknown")
                    chat_log.append(
                        f"Tool call: {tool}",
                        style="dim",
                    )

                elif event_type == "llm_request":
                    model = data.get("model", "unknown")
                    chat_log.append(
                        f"LLM request ({model})",
                        style="dim",
                    )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("Event listener error", exc_info=True)
                await asyncio.sleep(1)

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

    async def _submit_and_execute(self, description: str) -> None:
        """Submit a task and start the auto-execute loop.

        Args:
            description: The task description to submit.
        """
        if self._orch is None or self._worker is None:
            return

        chat_log = self.query_one("#chat-log", ChatLog)
        subtask_tree = self.query_one("#subtask-tree", SubtaskTree)

        chat_log.append(f"Submitting task: {description}", style="bold")
        chat_log.append("Decomposing via Claude Code...", style="dim")

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
                f"Decomposition failed: {task.result}",
                style="bold red",
            )
            return

        # Populate the subtask tree
        subtask_tree.set_task(task.description, task.id)
        self._subtask_index.clear()
        for i, st in enumerate(task.subtasks, start=1):
            subtask_tree.add_subtask(st, i)
            self._subtask_index[st.id] = i

        self.subtask_count = len(task.subtasks)
        self.completed_count = 0
        self.failed_count = 0
        subtask_tree.update_progress(0, len(task.subtasks))

        chat_log.append(
            f"Executing {len(task.subtasks)} subtasks...",
            style="cyan",
        )

        # Run the auto-execute loop as a background task
        self._execute_task_handle = asyncio.create_task(
            self._auto_execute_loop()
        )

    async def _auto_execute_loop(self) -> None:
        """Event-driven loop that assigns and executes ready subtasks.

        Runs as an asyncio task within Textual's event loop.
        Uses asyncio.Event to wake immediately when a subtask completes/fails,
        with a 30s safety timeout to prevent deadlocks.
        """
        # ponytail: event-driven replaces 2s polling, 30s safety timeout prevents deadlock
        DISPATCH_TIMEOUT = 30.0

        if self._orch is None or self._worker is None:
            return

        chat_log = self.query_one("#chat-log", ChatLog)
        orch = self._orch
        worker = self._worker

        try:
            while True:
                # Check if current task is still active
                task = orch.tasks.get(self.current_task_id)
                if task is None:
                    break

                if task.status.value not in ("in_progress", "planning"):
                    chat_log.append(
                        f"Task loop ended (status: {task.status.value})",
                        style="dim",
                    )
                    break

                executed_any = False
                for subtask in task.subtasks:
                    if subtask.status.value != "pending":
                        continue

                    # Fix self-referencing deps
                    effective_deps = [d for d in subtask.depends_on if d != subtask.id]
                    deps_done = True
                    if effective_deps:
                        for dep_id in effective_deps:
                            dep_st = None
                            for st in task.subtasks:
                                if st.id == dep_id:
                                    dep_st = st
                                    break
                            if dep_st is None or not dep_st.is_complete:
                                deps_done = False
                                break

                    if not deps_done:
                        continue

                    worker_info = orch.workers.get(worker.worker_id)
                    if worker_info is None or not worker_info.is_available:
                        continue

                    subtask.assigned_worker = worker.worker_id
                    subtask.status = SubtaskStatus.ASSIGNED
                    worker_info.current_load += 1

                    idx = self._subtask_index.get(subtask.id, 0)
                    chat_log.append(
                        f"Executing subtask {idx}: {subtask.description[:60]}",
                        style="cyan",
                    )

                    # Update tree to show assigned state
                    subtask_tree = self.query_one("#subtask-tree", SubtaskTree)
                    subtask_tree.update_subtask_status(
                        subtask.id, SubtaskStatus.IN_PROGRESS
                    )

                    result = await worker.execute_subtask(subtask)

                    worker_info.current_load = max(0, worker_info.current_load - 1)
                    await orch.handle_subtask_result(result)

                    executed_any = True

                if not executed_any:
                    # Check if all subtasks are terminal (done or failed)
                    all_terminal = all(
                        st.is_complete or st.is_failed for st in task.subtasks
                    )
                    if all_terminal and task.subtasks:
                        chat_log.append("All subtasks processed", style="dim")
                        break

                    # Wait for a subtask to complete/fail (event-driven) with safety timeout
                    self._dispatch_event.clear()
                    try:
                        await asyncio.wait_for(
                            self._dispatch_event.wait(), timeout=DISPATCH_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        pass  # Safety check: re-evaluate ready subtasks

        except asyncio.CancelledError:
            # Decrement worker load for the currently executing subtask
            # (if one was in-flight) to prevent load counter leak.
            worker_info = orch.workers.get(worker.worker_id)
            if worker_info is not None:
                # Find any subtask assigned to this worker that is still
                # in a non-terminal state and decrement its load.
                for task in orch.tasks.values():
                    for st in task.subtasks:
                        if (
                            st.assigned_worker == worker.worker_id
                            and st.status
                            in (SubtaskStatus.ASSIGNED, SubtaskStatus.IN_PROGRESS)
                        ):
                            worker_info.current_load = max(
                                0, worker_info.current_load - 1
                            )
            raise  # Re-raise so the asyncio.Task is properly marked cancelled

    # ── Input handling ─────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the task input field.

        Submits a new task and clears the input.
        """
        description = event.value.strip()
        if not description:
            return

        event.input.value = ""

        chat_log = self.query_one("#chat-log", ChatLog)

        # Clear previous log and echo user input as the first line
        chat_log.clear_log()
        chat_log.append_user_input(description)

        # If there is an active execute loop, cancel it first
        if self._execute_task_handle and not self._execute_task_handle.done():
            self._execute_task_handle.cancel()
            self._execute_task_handle = None

        asyncio.create_task(self._submit_and_execute(description))

    # ── Cleanup ────────────────────────────────────────────────────

    def on_unmount(self) -> None:
        """Cancel background tasks on shutdown."""
        if self._execute_task_handle and not self._execute_task_handle.done():
            self._execute_task_handle.cancel()
        if self._event_listener_handle and not self._event_listener_handle.done():
            self._event_listener_handle.cancel()
