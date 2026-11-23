//! # UltimateCoders - Core Engine
//!
//! Implements the `EngineApi` trait with local (in-process) components.
//! Depends on uc-types for the trait and data definitions.

pub mod argus_pmg;
pub mod async_dep_checker;
pub mod chain_isolation;
pub mod checkpoint;
pub mod clean_regrader;
pub mod config;
pub mod conflict;
pub mod continuous_bench;
pub mod cross_os_migration_gate;
pub mod cross_repo_coordinator;
pub mod dag;
pub mod dsh_aig_taint_guard;
pub mod events;
pub mod expert_rubric_calibrator;
pub mod germline_mcp;
pub mod git;
pub mod governance;
pub mod graph_store;
pub mod harness_awareness;
pub mod her_block_checkpoint;
pub mod indexer;
pub mod local;
pub mod mem_sidecar;
pub mod memory;
pub mod metadata;
pub mod metal_scheduler;
pub mod micro_commit;
pub mod migration_lock;
pub mod monitor_firewall;
pub mod mutation_authority_guard;
pub mod prism_agentcap;
pub mod rate_limiter;
pub mod repos_config;
pub mod rl_hardening;
pub mod runtime_metrics;
pub mod sandbox;
pub mod scheduler;
pub mod search;
pub mod setupx_lifo;
pub mod shift_reconciler;
pub mod soei_ledger;
pub mod swe_gate_verifier;
pub mod task_store;
pub mod tenon_residency;
pub mod tokencast_budgeter;
pub mod tomasu_ooo;
pub mod vuln_guard;

pub use argus_pmg::{
    ArgusError, ArgusPhase, ArgusReport, ArgusRuntime, ArgusStrategy, PhysicalConstraint,
    PmgDecision, PmgError, PmgGuard, S2PMapping, DEFAULT_MAX_STRATEGIES, MATURE_WAVE_THRESHOLD,
};
pub use async_dep_checker::{
    AsyncDepError, AsyncDependencyChecker, DependencyEdge, DependencyKind, DependencyProbe,
    DependencyState, HoppingWindow, IntegrationReport, DEFAULT_MAX_EDGES,
};
pub use chain_isolation::{
    AstEdit, BugScope, ChainError, ChainPatch, ChainSanitizer, SanitizedPatch,
    DEFAULT_OVERSHOOT_LAMBDA,
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
pub use continuous_bench::{
    Attribution, AttributionKind, BenchmarkBudget, BenchmarkResult, BenchmarkSpec,
    CalibrationReport as BenchCalibrationReport, ContinuousBench, ContinuousBenchError,
    TrajectoryQaFinding, DEFAULT_MAX_RESULTS, DEFAULT_MAX_SPECS,
};
pub use cross_os_migration_gate::{
    CoverageGap, CoverageRegion, CrossOsError, CrossOsMigrationGate, MigrationReport,
    MigrationRequest, OsTestOutcome, Platform, RegionKind, DEFAULT_MAX_REGIONS,
};
pub use cross_repo_coordinator::{
    ConsumerDependency, ContractChange, ContractCheck, ContractCheckState, CrossRepoCoordinator,
    CrossRepoError, DependencyKind as CrossRepoDependencyKind, ExportedSymbol, PropagationReport,
    RepoContract, DEFAULT_MAX_EDGES as DEFAULT_MAX_CROSS_REPO_EDGES, DEFAULT_MAX_REPOS,
};
pub use dag::{CumulativeRegressionPlan, RegressionObligation, RegressionPlanError};
pub use dsh_aig_taint_guard::{
    fresh_nonce, is_hidden_unicode, strip_hidden_unicode, AigTaintGuard, LlmJudge, TaintChannel,
    TaintFinding, TaintKind, TaintReport, ToolEnvelope,
};
/// NATS JetStream-backed `EventStore` (feature-gated on `messaging`).
#[cfg(feature = "messaging")]
pub use events::NatsEventStore;
pub use events::{
    AgentEventType, EventStore, InMemoryEventStore, LineRange, RecordedEvent, TaskSnapshot,
};
pub use expert_rubric_calibrator::{
    CalibrationCase, CalibrationReport, ExpertRubricCalibrator, PatchEvidence, PatchKind,
    RubricCriterion, RubricError, RubricScore, DEFAULT_MAX_CRITERIA,
};
pub use germline_mcp::{
    GermlineError, GermlineExecution, GermlinePool, SinkKind, ToolInvocation, DEFAULT_POOL_SIZE,
};
pub use graph_store::{requires_independence, transition_ok, GraphShadowSink, SweptAttempt};
#[cfg(feature = "storage")]
pub use graph_store::{BackfillStats, FailOutcome, GraphStore, DEFAULT_MAX_ATTEMPTS};
pub use harness_awareness::{
    HarnessAwarenessError, HarnessAwarenessRouter, RouteDecision, RouteScore, ToolCandidate,
    ToolHealthSnapshot, ToolObservation, DEFAULT_FAULT_PENALTY_MS, DEFAULT_HEALTH_WINDOW,
};
pub use her_block_checkpoint::{
    BlockCheckpoint, CheckpointOutcome, FailureRelocation, HerBlockController, HerError,
    HerSnapshot, PatchBack, ReplayPlan, SetupBlock, SetupBlockKind, DEFAULT_MAX_BLOCKS,
    DEFAULT_MAX_CHECKPOINTS,
};
pub use indexer::semantic::{EmbeddingService, SemanticIndexer};
pub use indexer::IndexPipeline;
pub use local::LocalEngine;
pub use mem_sidecar::{
    HookRule, MemSidecar, MemSidecarError, MemoryKind, MemoryRecord, MemoryRoute, PrefetchEntry,
    PrefetchPrediction, RouteReport, SidecarStats, DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_MAX_PREFETCH,
};
pub use metal_scheduler::{
    CollusionMonitor, CommandAssignment, CommandKind, CrossProviderReview, DecisionRecord,
    MetalCommand, MetalError, MetalScheduler, NodeSpec, ProvenanceReport, ReviewEvidence,
    SchedulePlan, DEFAULT_PARALLEL_SLOTS, DEFAULT_SERIAL_SLOTS,
};
pub use micro_commit::{
    AgentEvent, AgentEventKind, AgentStepper, HotEdit, IngestOutcome, MicroCommit,
    MicroCommitError, StepperState, DEFAULT_MAX_COMMITS, DEFAULT_MAX_EVENTS, DEFAULT_MAX_OVERRIDES,
};
pub use monitor_firewall::{
    admit_message, evidence_digest, EvidenceKind, MonitorDecision, MonitorFirewall,
    MonitorFirewallError, MonitorRequest, MonitorVerdict, StructuredEvidence, DEFAULT_MAX_EVIDENCE,
};
pub use mutation_authority_guard::{
    receipt_signature, AuthorityDecision, AuthorityReceipt, MutationAuthorityError,
    MutationAuthorityGuard, MutationRequest, DEFAULT_MAX_DECISIONS,
};
pub use prism_agentcap::{
    distinct_tools, AgentCapVerifier, CapabilityIssuer, CapabilityToken, MinimizedRole, PrismError,
    PrismMinimizer, ToolPrivilege, DEFAULT_NECESSITY_THRESHOLD,
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
pub use setupx_lifo::{
    SetupEvidence, SetupXError, SetupXLifo, Snapshot, Speculation, SpeculationOutcome, Verdict,
    Xpu, DEFAULT_MAX_SNAPSHOTS, DEFAULT_MAX_XPUS,
};
pub use shift_reconciler::{
    RecoveryPlan, ShiftError, ShiftEvent, ShiftKind, ShiftPhase, ShiftReconciler, WorkspaceEpoch,
    DEFAULT_EVENT_CAPACITY,
};
pub use soei_ledger::{
    LedgerEntry, LedgerEventKind, PendingRecord, ProvEdge, ProvRelation, SoeiError, SoeiLedger,
    DEFAULT_LEDGER_CAPACITY, GENESIS_HASH, LEDGER_ALGORITHM,
};
pub use swe_gate_verifier::{
    ConstraintCheck, ConstraintKind, FunctionalResult, GateReport, ReviewConstraint,
    RewardHackScrubber, RewardHackSignals, ScrubReport, SweGateError, SweGateVerifier,
    DEFAULT_MAX_CONSTRAINTS,
};
#[cfg(feature = "storage")]
pub use task_store::PostgresTaskBackend;
pub use task_store::{InMemoryTaskBackend, TaskStoreBackend};
pub use tenon_residency::{
    ResidencyAction, ResidencyPlan, SessionDemand, SessionState, TenonError, TenonResidency,
    DEFAULT_MAX_OVERSUBSCRIPTION, DEFAULT_MEMORY_BUDGET_MB,
};
pub use tokencast_budgeter::{
    BudgetAction, BudgetDecision, ExecutionSegment, TokenCastBudgeter, TokenForecast,
    DEFAULT_TOKEN_BUDGET,
};
pub use tomasu_ooo::{
    ActionKind, AuditEntry, AuditOutcome, CommittedObservation, OperandKind, RunAheadConfig,
    RunAheadController, RunAheadStats, SpeculativeAction, TomasuError, TraceIrValidator,
    ValidationFailure, ValidationReport, DEFAULT_AUDIT_CAPACITY, DEFAULT_MAX_CHAIN_DEPTH,
    DEFAULT_MAX_SPECULATIVE_SLOTS,
};
pub use vuln_guard::{
    CweClass, Disclosure, MemoryAuditReport, MemoryFile, MemoryFinding, MemoryRule, PatchFinding,
    PatchFindingKind, PatchHunk, PatchReview, VulnGuard, VulnGuardError, VulnerableSnippet,
    DEFAULT_MAX_MEMORY_FILES, DEFAULT_MAX_SNIPPETS, MEMORY_RULES,
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
