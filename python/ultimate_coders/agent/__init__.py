"""Agent module — shared types, conflict detection, and scheduling."""

from ultimate_coders.agent.codegraph import CodegraphClient
from ultimate_coders.agent.conflict import (
    ConflictDetector,
    ConflictInfo,
    ConflictMarker,
    ConflictResolver,
    ConflictResult,
    EditIntent,
    EditType,
    LineRange,
    MergeResult,
    ResolutionTier,
)
from ultimate_coders.agent.rate_limiter import (
    CircuitBreaker,
    CircuitState,
    ModelFallbackChain,
    RateLimiter,
    RateLimiterConfig,
    RequestPriority,
    RetryPolicy,
    TaskComplexity,
    TokenBucket,
)
from ultimate_coders.agent.scheduler import Scheduler
from ultimate_coders.agent.scheduler_config import (
    NightWindowConfig,
    ScheduledTaskConfig,
    SchedulerConfig,
    load_scheduler_config,
)
from ultimate_coders.agent.types import (
    ChangeType,
    FileChange,
    OrchestratorConfig,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    Task,
    TaskStatus,
    WorkerInfo,
)

__all__ = [
    "CodegraphClient",
    "ChangeType",
    "FileChange",
    "OrchestratorConfig",
    "Subtask",
    "SubtaskResult",
    "SubtaskStatus",
    "Task",
    "TaskStatus",
    "WorkerInfo",
    # Rate limiter
    "CircuitBreaker",
    "CircuitState",
    "ModelFallbackChain",
    "RateLimiter",
    "RateLimiterConfig",
    "RequestPriority",
    "RetryPolicy",
    "TaskComplexity",
    "TokenBucket",
    # Conflict detection
    "ConflictDetector",
    "ConflictInfo",
    "ConflictMarker",
    "ConflictResolver",
    "ConflictResult",
    "EditIntent",
    "EditType",
    "LineRange",
    "MergeResult",
    "ResolutionTier",
    # Scheduler
    "Scheduler",
    "SchedulerConfig",
    "ScheduledTaskConfig",
    "NightWindowConfig",
    "load_scheduler_config",
]
