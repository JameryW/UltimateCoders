"""Agent subsystem — Worker, Sandbox, Scheduler, Conflict Detection, Aggregation."""

from ultimate_coders.agent.aggregator import AggregatedResult, AggregationStatus, ResultAggregator
from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictInfo,
    ConflictResolver,
    ConflictResult,
    EditIntent,
    EditType,
    MergeResult,
    ResolutionTier,
)
from ultimate_coders.agent.distributed_conflict import (
    DistributedConflictDetector,
    MergeVerifier,
)
from ultimate_coders.agent.sandbox import AgentOutput, SandboxConfig, SandboxManager
from ultimate_coders.agent.scheduler import Scheduler
from ultimate_coders.agent.state_sync import (
    ContextInjector,
    FileChangeEvent,
    FileChangeEventType,
    SubtaskContext,
    WorkspaceState,
    WorkspaceStateMachine,
)
from ultimate_coders.agent.types import (
    ChangeType,
    DispatchMode,
    FileChange,
    OrchestratorConfig,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkerInfo,
)
from ultimate_coders.agent.worker import Worker
from ultimate_coders.agent.workspace import WorkspaceHandle, WorkspaceManager

__all__ = [
    # Aggregation
    "AggregatedResult",
    "AggregationStatus",
    "ResultAggregator",
    # Conflict
    "ConflictDetector",
    "ConflictInfo",
    "ConflictResolver",
    "ConflictResult",
    "EditIntent",
    "EditType",
    "MergeResult",
    "ResolutionTier",
    # Distributed Conflict
    "DistributedConflictDetector",
    "MergeVerifier",
    # Sandbox
    "AgentOutput",
    "SandboxConfig",
    "SandboxManager",
    # Scheduler
    "Scheduler",
    # State Sync
    "ContextInjector",
    "FileChangeEvent",
    "FileChangeEventType",
    "SubtaskContext",
    "WorkspaceState",
    "WorkspaceStateMachine",
    # Types
    "ChangeType",
    "DispatchMode",
    "FileChange",
    "OrchestratorConfig",
    "Subtask",
    "SubtaskResult",
    "SubtaskStatus",
    "Task",
    "TaskStatus",
    "WorkerInfo",
    # Worker
    "Worker",
    # Workspace
    "WorkspaceHandle",
    "WorkspaceManager",
]
