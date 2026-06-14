"""Main Textual application for the UltimateCoders sandbox TUI.

Provides a full terminal UI with:
- Left panel: subtask tree with progress
- Right panel: scrollable output log
- Bottom: task input field + status bar

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
    OutputLog,
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
    the event emitter, and updates the subtask tree and output log in
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

    #input-area {
        height: 3;
        margin: 0 1;
    }

    SubtaskTree {
        width: 1fr;
    }

    OutputLog {
        width: 2fr;
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

    def compose(self) -> ComposeResult:
        """Build the TUI layout."""
        with Horizontal(id="main-area"):
            yield SubtaskTree(id="subtask-tree")
            yield OutputLog(id="output-log")
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
            llm_client=None,
            sandbox_manager=sandbox_manager,
        )

        self._worker = Worker(
            worker_id="local-sandbox-worker",
            engine=None,
            execution_mode="sandbox",
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
        output_log = self.query_one("#output-log", OutputLog)
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
                    output_log.append(
                        f"Task submitted: {data.get('description', '')}",
                        style="bold",
                    )
                    subtask_count = data.get("subtask_count", 0)
                    output_log.append(
                        f"Decomposed into {subtask_count} subtasks",
                        style="cyan",
                    )

                elif event_type == "subtask_started":
                    desc = data.get("description", subtask_id[:8])
                    output_log.append(
                        f"Subtask started: {desc}",
                        style="cyan",
                    )
                    if subtask_id:
                        subtask_tree.update_subtask_status(
                            subtask_id, SubtaskStatus.IN_PROGRESS
                        )

                elif event_type == "subtask_completed":
                    output_log.append(
                        f"Subtask completed: {data.get('summary', '')[:80]}",
                        style="green",
                    )
                    if subtask_id:
                        subtask_tree.update_subtask_status(
                            subtask_id, SubtaskStatus.COMPLETED
                        )
                    self._update_progress()

                elif event_type == "subtask_failed":
                    output_log.append(
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
                        output_log.append("Task completed!", style="bold green")
                    else:
                        output_log.append(
                            f"Task finished with status: {status}",
                            style="bold red",
                        )

                elif event_type == "tool_call":
                    tool = data.get("tool", "unknown")
                    output_log.append(
                        f"Tool call: {tool}",
                        style="dim",
                    )

                elif event_type == "llm_request":
                    model = data.get("model", "unknown")
                    output_log.append(
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

        output_log = self.query_one("#output-log", OutputLog)
        subtask_tree = self.query_one("#subtask-tree", SubtaskTree)

        output_log.append(f"Submitting task: {description}", style="bold")
        output_log.append("Decomposing via Claude Code...", style="dim")

        try:
            task = await self._orch.submit_task(
                description=description,
                project_id=self._config.project_path,
            )
        except Exception as e:
            output_log.append(f"Task submission failed: {e}", style="bold red")
            return

        self.current_task_id = task.id

        if task.status.value == "failed":
            output_log.append(
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

        output_log.append(
            f"Executing {len(task.subtasks)} subtasks...",
            style="cyan",
        )

        # Run the auto-execute loop as a background task
        self._execute_task_handle = asyncio.create_task(
            self._auto_execute_loop()
        )

    async def _auto_execute_loop(self) -> None:
        """Background loop that assigns and executes ready subtasks.

        Runs as an asyncio task within Textual's event loop.
        Polls every 2 seconds for pending subtasks whose dependencies
        are all completed, then executes them via the sandbox Worker.

        Handles cancellation gracefully by decrementing worker load for
        any subtask that was in-flight when the cancel occurred.
        """
        if self._orch is None or self._worker is None:
            return

        output_log = self.query_one("#output-log", OutputLog)
        orch = self._orch
        worker = self._worker

        try:
            while True:
                await asyncio.sleep(2)

                # Check if current task is still active
                task = orch.tasks.get(self.current_task_id)
                if task is None:
                    # Task removed from orchestrator — stop the loop
                    # rather than spinning forever with `continue`
                    break

                if task.status.value not in ("in_progress", "planning"):
                    # Task finished or paused -- stop the loop
                    output_log.append(
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
                    output_log.append(
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
                        output_log.append("All subtasks processed", style="dim")
                        break

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

        input_widget = event.input
        input_widget.value = ""

        output_log = self.query_one("#output-log", OutputLog)

        # If there is an active execute loop, cancel it first
        if self._execute_task_handle and not self._execute_task_handle.done():
            self._execute_task_handle.cancel()
            self._execute_task_handle = None

        output_log.clear_log()
        asyncio.create_task(self._submit_and_execute(description))

    # ── Cleanup ────────────────────────────────────────────────────

    def on_unmount(self) -> None:
        """Cancel background tasks on shutdown."""
        if self._execute_task_handle and not self._execute_task_handle.done():
            self._execute_task_handle.cancel()
        if self._event_listener_handle and not self._event_listener_handle.done():
            self._event_listener_handle.cancel()
