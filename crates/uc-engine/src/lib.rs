//! # UltimateCoders - Core Engine
//!
//! Implements the `EngineApi` trait with local (in-process) components.
//! Depends on uc-types for the trait and data definitions.

pub mod async_dep_checker;
pub mod checkpoint;
pub mod clean_regrader;
pub mod config;
pub mod conflict;
pub mod cross_repo_coordinator;
pub mod dag;
pub mod events;
pub mod git;
pub mod governance;
pub mod graph_store;
pub mod her_block_checkpoint;
pub mod indexer;
pub mod local;
pub mod memory;
pub mod metadata;
pub mod migration_lock;
pub mod mutation_authority_guard;
pub mod rate_limiter;
pub mod repos_config;
pub mod rl_hardening;
pub mod runtime_metrics;
pub mod sandbox;
pub mod scheduler;
pub mod search;
pub mod shift_reconciler;
pub mod soei_ledger;
pub mod task_store;
pub mod tomasu_ooo;

pub use async_dep_checker::{
    AsyncDepError, AsyncDependencyChecker, DependencyEdge, DependencyKind, DependencyProbe,
    DependencyState, HoppingWindow, IntegrationReport, DEFAULT_MAX_EDGES,
};
pub use checkpoint::{CheckpointConfig, CheckpointManager};
pub use clean_regrader::{
    CleanRegradeReport, CleanRegradeRequest, CleanRegrader, RegradeConfigError, RegradeViolation,
    SandboxEgressPolicy, CLEAN_REGRADER_ID, DEFAULT_MAX_PATCH_BYTES,
};
pub use config::{EmbeddingConfig, EngineConfig, MemoryConfig, StorageConfig};
pub use conflict::merger::three_way_merge;
pub use conflict::{
    ConflictDetector, ConflictMarker, ConflictResult, EditIntent, EditType, MergeResult,
    ResolutionTier,
};
pub use cross_repo_coordinator::{
    ConsumerDependency, ContractChange, ContractCheck, ContractCheckState, CrossRepoCoordinator,
    CrossRepoError, DependencyKind as CrossRepoDependencyKind, ExportedSymbol, PropagationReport,
    RepoContract, DEFAULT_MAX_EDGES as DEFAULT_MAX_CROSS_REPO_EDGES, DEFAULT_MAX_REPOS,
};
pub use dag::{CumulativeRegressionPlan, RegressionObligation, RegressionPlanError};
/// NATS JetStream-backed `EventStore` (feature-gated on `messaging`).
#[cfg(feature = "messaging")]
pub use events::NatsEventStore;
pub use events::{
    AgentEventType, EventStore, InMemoryEventStore, LineRange, RecordedEvent, TaskSnapshot,
};
pub use graph_store::{requires_independence, transition_ok, GraphShadowSink, SweptAttempt};
#[cfg(feature = "storage")]
pub use graph_store::{BackfillStats, FailOutcome, GraphStore, DEFAULT_MAX_ATTEMPTS};
pub use her_block_checkpoint::{
    BlockCheckpoint, CheckpointOutcome, FailureRelocation, HerBlockController, HerError,
    HerSnapshot, PatchBack, ReplayPlan, SetupBlock, SetupBlockKind, DEFAULT_MAX_BLOCKS,
    DEFAULT_MAX_CHECKPOINTS,
};
pub use indexer::semantic::{EmbeddingService, SemanticIndexer};
pub use indexer::IndexPipeline;
pub use local::LocalEngine;
pub use mutation_authority_guard::{
    receipt_signature, AuthorityDecision, AuthorityReceipt, MutationAuthorityError,
    MutationAuthorityGuard, MutationRequest, DEFAULT_MAX_DECISIONS,
};
pub use rate_limiter::{
    LlmRateLimiter, LlmRateLimiterConfig, ModelFallbackChain, RequestPriority, TaskComplexity,
    TokenBucket,
};
pub use repos_config::{
    build_index_requests, discover_scan_dirs, load_repos_config, parse_repos_yaml,
    resolve_config_path, RepoConfig, RepoEntry,
};
pub use rl_hardening::{
    EnvironmentAnomaly, ProcessGroupCleanupPlan, ProcessGroupCommand, ProcessGroupPlan,
    ProcessGroupSpec, RlHardening, RlHardeningError, StateHashReceipt, HASH_ALGORITHM,
};
pub use search::HybridSearchEngine;
pub use search::SemanticSearchEngine;
pub use shift_reconciler::{
    RecoveryPlan, ShiftError, ShiftEvent, ShiftKind, ShiftPhase, ShiftReconciler, WorkspaceEpoch,
    DEFAULT_EVENT_CAPACITY,
};
pub use soei_ledger::{
    LedgerEntry, LedgerEventKind, PendingRecord, ProvEdge, ProvRelation, SoeiError, SoeiLedger,
    DEFAULT_LEDGER_CAPACITY, GENESIS_HASH, LEDGER_ALGORITHM,
};
#[cfg(feature = "storage")]
pub use task_store::PostgresTaskBackend;
pub use task_store::{InMemoryTaskBackend, TaskStoreBackend};
pub use tomasu_ooo::{
    ActionKind, AuditEntry, AuditOutcome, CommittedObservation, OperandKind, RunAheadConfig,
    RunAheadController, RunAheadStats, SpeculativeAction, TomasuError, TraceIrValidator,
    ValidationFailure, ValidationReport, DEFAULT_AUDIT_CAPACITY, DEFAULT_MAX_CHAIN_DEPTH,
    DEFAULT_MAX_SPECULATIVE_SLOTS,
};

// Sandbox module re-exports
pub use sandbox::agents::claude_code::ClaudeCodeAgent;
pub use sandbox::agents::codex::CodexAgent;
pub use sandbox::agents::grok::GrokBuildAgent;
pub use sandbox::agents::{available_agents, create_adapter, AgentAdapter};
pub use sandbox::file_tracker::FileTracker;
pub use sandbox::pool::SandboxPool;
pub use sandbox::subprocess::SubprocessSandbox;
pub use sandbox::{
    subtask_usage_from_token_usage, AgentOutput, ExecRequest, ExecResult, NetworkMode,
    ResourceLimits, Sandbox, SandboxConfig, SandboxHandle, SandboxHealth, SandboxStatus,
    TokenUsage,
};

// Scheduler module re-exports
pub use scheduler::{
    AddJobResult, EngineSubmitDispatcher, InMemoryScheduleStore, LoggingDispatcher, NightWindow,
    NightWindowError, OrchestratorDispatcher, ScheduleDispatcher, ScheduleStore, SchedulerService,
    WindowEventType,
};

#[cfg(feature = "storage")]
pub use scheduler::PostgresScheduleStore;

#[cfg(feature = "messaging")]
pub use scheduler::publish_window_event;
