# MetaInfer reliability and experiment delivery

Source: architecture review accepted by the user's request to fix all findings on 2026-10-02.
Tracker mode: local Markdown; GitHub authentication preflight failed.

UC retains planning, execution authority and acceptance. MetaInfer retains specialized execution
and GPU allocation. Repair the cross-module lifecycle and implement the reviewed capability,
artifact and measurement improvements inside the existing deployment.

The single implementation ticket is [runtime reliability](issues/01-runtime-reliability.md).
The authorized public verification seams are Orchestrator.submit_task, Worker.execute_subtask,
WorkspaceManager.acquire/release, MetaInferAdapter.execute, OptimizationWorkflow.run,
NATS delivery/result publication, BenchmarkRunner.measure/Oracle.evaluate and Dashboard HTTP.

No unresolved product decisions. Preserve Python 3.9 and existing adapters. PostgreSQL is the
shared production store; local persistence remains available for standalone operation.
