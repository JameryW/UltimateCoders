"""Run UltimateCoders in sandbox mode using Claude Code CLI as the unified engine.

Both Orchestrator (task decomposition) and Worker (subtask execution)
invoke `claude -p ...` via SandboxManager -- no Python LLMClient needed.

Usage:
    # CLI mode (default): submit a task, print results
    .venv/bin/python scripts/run_sandbox.py "Fix the bug in main.rs"

    # TUI mode: interactive terminal UI with real-time updates
    .venv/bin/python scripts/run_sandbox.py --tui "Fix the bug in main.rs"
    .venv/bin/python scripts/run_sandbox.py --tui

    # Dashboard mode: web UI on http://localhost:8080
    .venv/bin/python scripts/run_sandbox.py --dashboard

    # With Docker Compose storage infrastructure
    .venv/bin/python scripts/run_sandbox.py --with-infra "Implement auth"

    # Docker sandbox backend (instead of subprocess)
    .venv/bin/python scripts/run_sandbox.py --backend docker "Add tests"

Options:
    --tui         Launch Textual TUI (terminal UI with real-time updates)
    --dashboard   Start web Dashboard alongside auto-execute loop
    --backend     Sandbox backend: subprocess (default) or docker
    --with-infra  Start Docker Compose storage (TiKV/Qdrant/PostgreSQL/NATS)
    --project     Project path (defaults to current directory)
    --max-turns   Max turns for worker Claude Code calls (default: 20)
    --port        Dashboard port (default: 8080)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# -- Load .env -----------------------------------------------------------

_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

from ultimate_coders.agent.orchestrator import Orchestrator  # noqa: E402
from ultimate_coders.agent.sandbox import SandboxConfig, SandboxManager  # noqa: E402
from ultimate_coders.agent.types import SubtaskStatus, WorkerInfo  # noqa: E402
from ultimate_coders.agent.worker import Worker  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# -- Auto-execute loop ---------------------------------------------------

async def auto_execute_loop(orch: Orchestrator, worker: Worker) -> None:
    """Background loop that assigns and executes ready subtasks.

    Polls every 2 seconds for pending subtasks whose dependencies
    are all completed, then executes them via the sandbox Worker.
    """
    logger.info("Auto-execute loop started (worker=%s, mode=sandbox)", worker.worker_id)

    while True:
        await asyncio.sleep(2)

        for task in list(orch.tasks.values()):
            if task.status.value not in ("in_progress", "planning"):
                continue

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

                logger.info(
                    "Auto-assigning subtask %s (%s) to worker %s",
                    subtask.id,
                    subtask.description[:60],
                    worker.worker_id,
                )

                result = await worker.execute_subtask(subtask)

                worker_info.current_load = max(0, worker_info.current_load - 1)
                await orch.handle_subtask_result(result)

                logger.info(
                    "Subtask %s completed (success=%s)",
                    result.subtask_id,
                    result.success,
                )


# -- CLI mode ------------------------------------------------------------

async def run_cli(task_description: str, config: SandboxConfig) -> None:
    """Submit a single task and print decomposition + execution results."""
    sandbox_manager = SandboxManager(config)

    orch = Orchestrator(
        engine=None,
        llm_client=None,
        sandbox_manager=sandbox_manager,
    )

    worker = Worker(
        worker_id="local-sandbox-worker",
        engine=None,
        execution_mode="sandbox",
        sandbox_config=config,
        event_emitter=orch.event_emitter,
    )

    worker_info = WorkerInfo(
        id=worker.worker_id,
        capabilities=["code", "search", "memory", "test"],
        current_load=0,
        max_capacity=3,
    )
    orch.workers[worker_info.id] = worker_info

    logger.info("Submitting task: %s", task_description)
    task = await orch.submit_task(
        description=task_description,
        project_id=config.project_path,
    )

    if task.status.value == "failed":
        print(f"\nX Task decomposition failed: {task.result}")
        return

    # Print decomposition
    print(f"\nTask: {task.description}")
    print(f"   Status: {task.status.value}")
    print(f"   Subtasks ({len(task.subtasks)}):")
    for i, st in enumerate(task.subtasks):
        deps = ", ".join(st.depends_on) if st.depends_on else "none"
        print(f"   {i + 1}. {st.description} (deps: {deps})")

    # Execute subtasks
    print("\nExecuting subtasks via Claude Code sandbox...")
    await auto_execute_loop(orch, worker)

    # Print final results
    final = orch.get_task_status(task.id)
    if final:
        print(f"\nTask completed (status: {final.status.value})")
        for st in final.subtasks:
            status_icon = "+" if st.is_complete else "-"
            print(f"   {status_icon} {st.description}")
    else:
        print("\nX Task status unavailable")


# -- Dashboard mode ------------------------------------------------------

def run_dashboard(config: SandboxConfig, port: int) -> None:
    """Start Dashboard with sandbox-mode Orchestrator + Worker."""
    from ultimate_coders.dashboard import DashboardApp

    sandbox_manager = SandboxManager(config)

    orch = Orchestrator(
        engine=None,
        llm_client=None,
        sandbox_manager=sandbox_manager,
    )

    worker = Worker(
        worker_id="local-sandbox-worker",
        engine=None,
        execution_mode="sandbox",
        sandbox_config=config,
        event_emitter=orch.event_emitter,
    )

    worker_info = WorkerInfo(
        id=worker.worker_id,
        capabilities=["code", "search", "memory", "test"],
        current_load=0,
        max_capacity=3,
    )
    orch.workers[worker_info.id] = worker_info

    logger.info("Orchestrator + Worker created (sandbox mode, engine=None)")

    dashboard = DashboardApp(orch)
    logger.info("Starting Dashboard on http://localhost:%d/dashboard/", port)
    dashboard.start(host="0.0.0.0", port=port)

    # Auto-execute loop in background thread
    loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(auto_execute_loop(orch, worker))

    import threading
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()

    logger.info("Dashboard + Auto-execute loop running. Press Ctrl+C to stop.")
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        dashboard.stop()


# -- TUI mode ------------------------------------------------------------

def run_tui(config: SandboxConfig, initial_task: str | None) -> None:
    """Launch the Textual TUI with sandbox-mode Orchestrator + Worker.

    Args:
        config: SandboxConfig for the worker.
        initial_task: Optional task description to auto-submit.
    """
    from ultimate_coders.tui import SandboxTUI

    app = SandboxTUI(config=config, initial_task=initial_task)
    app.run()


# -- Infrastructure helpers -----------------------------------------------

def start_infra() -> None:
    """Start Docker Compose storage infrastructure."""
    import subprocess

    compose_file = Path(__file__).parent.parent / "docker-compose.yml"
    if not compose_file.exists():
        logger.error("docker-compose.yml not found")
        sys.exit(1)

    logger.info("Starting Docker Compose infrastructure...")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Docker Compose failed: %s", result.stderr)
        sys.exit(1)
    logger.info("Infrastructure started (TiKV/Qdrant/PostgreSQL/NATS)")


def stop_infra() -> None:
    """Stop Docker Compose storage infrastructure."""
    import subprocess

    compose_file = Path(__file__).parent.parent / "docker-compose.yml"
    if not compose_file.exists():
        return

    logger.info("Stopping Docker Compose infrastructure...")
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "down"],
        capture_output=True,
    )


# -- Main ----------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run UltimateCoders in sandbox mode with Claude Code CLI",
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help="Task description to submit (required for CLI mode)",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch Textual TUI with real-time updates",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Start web Dashboard (http://localhost:8080/dashboard/)",
    )
    parser.add_argument(
        "--backend",
        choices=["subprocess", "docker"],
        default="subprocess",
        help="Sandbox backend (default: subprocess)",
    )
    parser.add_argument(
        "--with-infra",
        action="store_true",
        help="Start Docker Compose storage infrastructure",
    )
    parser.add_argument(
        "--project",
        default=str(Path.cwd()),
        help="Project path (default: current directory)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Max turns for worker Claude Code calls (default: 20)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Dashboard port (default: 8080)",
    )

    args = parser.parse_args()

    # Validate: at least one mode or a task description
    if not args.tui and not args.dashboard and args.task is None:
        parser.error("Provide a task description or use --tui or --dashboard")

    # Check ANTHROPIC_API_KEY
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error(
            "ANTHROPIC_API_KEY not set. Create .env file or set environment variable."
        )
        sys.exit(1)

    # Check claude CLI (required for all modes)
    import shutil
    if not shutil.which("claude"):
        logger.error("claude CLI not found. Install: https://claude.ai/code")
        sys.exit(1)

    # Check TUI dependencies if --tui is used
    if args.tui:
        try:
            import textual  # noqa: F401
        except ImportError:
            logger.error(
                "textual not installed. Install with: pip install textual>=0.40"
            )
            sys.exit(1)

    # Build SandboxConfig
    config = SandboxConfig(
        agent="claude-code",
        backend=args.backend,
        project_path=args.project,
        api_key=api_key,
        max_cpu_seconds=args.max_turns * 180,  # ~3 min per turn
        working_dir=args.project,
    )

    # Start infrastructure if requested
    if args.with_infra:
        start_infra()

    try:
        if args.tui:
            run_tui(config, args.task)
        elif args.dashboard:
            run_dashboard(config, args.port)
        else:
            asyncio.run(run_cli(args.task, config))
    finally:
        if args.with_infra:
            stop_infra()


if __name__ == "__main__":
    main()
