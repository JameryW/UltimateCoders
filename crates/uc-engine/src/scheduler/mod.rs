//! Scheduler module — task scheduling with night-time orchestration.
//!
//! Provides:
//! - `NightWindow`: Time-window evaluation logic (cross-midnight aware)
//! - `SchedulerService`: Cron and one-shot job scheduling with night-window guard
//! - `ScheduleDispatcher`: Trait for dispatching scheduled tasks
//! - `OrchestratorDispatcher`: NATS-based dispatcher for Orchestrator integration
//! - `ScheduleStore`: Trait for schedule persistence (abstract over PostgreSQL for testing)
//! - `InMemoryScheduleStore`: In-memory store for testing
//! - `PostgresScheduleStore`: PostgreSQL-backed store for production (requires `storage` feature)
//! - Migration functions for scheduler database tables (requires `storage` feature)

pub mod config;
pub mod dependency;
pub mod dispatcher;
pub mod executor;
pub mod kv_metastability_budgeter;
pub mod lock;
pub mod migration;
pub mod night_window;
pub mod overserve_evidence_guard;
pub mod service;
pub mod store;

pub use dependency::resolve_execution_order;
pub use dispatcher::{EngineSubmitDispatcher, OrchestratorDispatcher, WindowEventType};
pub use executor::{
    AttemptOutcome, AttemptStatus, Executor, ExecutorError, ExecutorSelector, LocalExecutor,
    LocalNodeHandler, RemoteExecutor, RouteDecision, SandboxExecutor,
};
pub use kv_metastability_budgeter::{
    BudgetPlan, KvBudgetError, KvMetastabilityBudgeter, SessionDemand, DEFAULT_SAFETY_RATIO,
};
pub use lock::{LockProvider, NoOpLockProvider};
pub use night_window::{NightWindow, NightWindowError};
pub use overserve_evidence_guard::{
    ClaimEvidence, CompletionAttempt, CompletionSource, CompletionVerdict, CounterEvidenceGuard,
    EvidenceVerdict, OverServeError, OverServeGuard, PollObservation, PollThrottle, PollVerdict,
    ProviderCrashFuse, DEFAULT_BASE_DELAY_MS, DEFAULT_MAX_DELAY_MS,
};
pub use service::{
    AddJobResult, DispatchSource, LoggingDispatcher, ScheduleDispatcher, SchedulerService,
};
pub use store::{InMemoryScheduleStore, ScheduleStore};

#[cfg(feature = "storage")]
pub use store::PostgresScheduleStore;

#[cfg(feature = "messaging")]
pub use dispatcher::publish_window_event;
#[cfg(feature = "messaging")]
pub use executor::NatsExecutor;
