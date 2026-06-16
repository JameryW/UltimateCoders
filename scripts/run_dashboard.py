"""Run the Dashboard locally with a real Orchestrator + Worker + LLMClient.

This script starts the Dashboard with real components:
- Orchestrator with real LLMClient (Anthropic API via MaaS proxy)
- Worker (execution_mode="llm") registered with Orchestrator
- Auto-execution loop: assigns ready subtasks and executes them
- Dashboard on http://localhost:8080

Usage:
    .venv/bin/python scripts/run_dashboard.py
"""

import asyncio
import logging
import os
import sys

# Load .env
from pathlib import Path

_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

from ultimate_coders.agent.llm import LLMClient
from ultimate_coders.agent.orchestrator import Orchestrator
from ultimate_coders.agent.worker import Worker
from ultimate_coders.agent.types import WorkerInfo
from ultimate_coders.dashboard import DashboardApp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def auto_execute_loop(orch: Orchestrator, worker: Worker) -> None:
    """Background loop that automatically assigns and executes ready subtasks.

    Polls every 2 seconds for:
    1. Pending subtasks with no unresolved dependencies (ready to execute)
    2. Assigns them to the Worker
    3. Executes them via Worker.execute_subtask()
    4. Calls Orchestrator.handle_subtask_result() to update state
    """
    logger.info("Auto-execute loop started (worker=%s)", worker.worker_id)

    while True:
        await asyncio.sleep(2)

        # Find all tasks with pending subtasks
        for task in list(orch.tasks.values()):
            if task.status.value not in ("in_progress", "planning"):
                continue

            for subtask in task.subtasks:
                # Only process PENDING subtasks whose deps are all completed
                if subtask.status.value != "pending":
                    continue

                # Check dependencies are all completed
                # Fix self-referencing deps (LLM decomposition bug)
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

                # Assign to worker
                worker_info = orch.workers.get(worker.worker_id)
                if worker_info is None or not worker_info.is_available:
                    continue

                from ultimate_coders.agent.types import SubtaskStatus

                subtask.assigned_worker = worker.worker_id
                subtask.status = SubtaskStatus.ASSIGNED
                worker_info.current_load += 1

                logger.info(
                    "Auto-assigning subtask %s (%s) to worker %s",
                    subtask.id,
                    subtask.description[:60],
                    worker.worker_id,
                )

                # Execute in background
                result = await worker.execute_subtask(subtask)

                # Release worker load
                worker_info.current_load = max(0, worker_info.current_load - 1)

                # Report result to orchestrator
                await orch.handle_subtask_result(result)

                logger.info(
                    "Subtask %s completed (success=%s)",
                    result.subtask_id,
                    result.success,
                )


def main():
    # Build real LLMClient
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    model = os.environ.get("ANTHROPIC_MODEL", "astron-code-latest")

    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set. Create .env file.")
        sys.exit(1)

    logger.info("Creating LLMClient: model=%s, base_url=%s", model, base_url)

    llm = LLMClient(
        provider="anthropic",
        api_key=api_key,
        model=model,
    )
    # Override base URL for MaaS proxy
    if base_url:
        import anthropic
        llm._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
        )

    # Build Orchestrator (no engine — fallback mode)
    # Optionally connect NATS publisher for gRPC TaskStore sync
    nats_publisher = None
    nats_url = os.environ.get("UC_NATS_URL", "")
    if nats_url:
        try:
            import nats as nats_lib
            from ultimate_coders.nats_worker import NatsPublisher

            async def _connect_nats():
                nc = await nats_lib.connect(nats_url)
                return NatsPublisher(nc)

            nats_publisher = asyncio.new_event_loop().run_until_complete(_connect_nats())
            logger.info("NATS publisher connected at %s", nats_url)
        except Exception:
            logger.warning("NATS connection failed, running without TaskStore sync", exc_info=True)

    orch = Orchestrator(
        engine=None,
        llm_client=llm,
        nats_publisher=nats_publisher,
    )

    # Build Worker with real LLM client + event emitter
    worker = Worker(
        worker_id="local-worker-1",
        llm_client=llm,
        engine=None,
        capabilities=["code", "search", "memory", "test"],
        max_capacity=3,
        execution_mode="llm",
        event_emitter=orch.event_emitter,
    )

    # Register worker info with Orchestrator
    worker_info = WorkerInfo(
        id=worker.worker_id,
        capabilities=["code", "search", "memory", "test"],
        current_load=0,
        max_capacity=3,
    )
    orch.workers[worker_info.id] = worker_info

    logger.info("Orchestrator + Worker created (engine=None, fallback mode)")

    # Build Dashboard (with NATS publisher for TaskStore sync)
    dashboard = DashboardApp(orch, nats_publisher=nats_publisher)

    logger.info("Starting Dashboard on http://localhost:8080/dashboard/")
    dashboard.start(host="0.0.0.0", port=8080)

    # Start auto-execute loop in background
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


if __name__ == "__main__":
    main()
