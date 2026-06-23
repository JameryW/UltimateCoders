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

pub mod dependency;
pub mod dispatcher;
pub mod migration;
pub mod night_window;
pub mod service;
pub mod store;

pub use dependency::resolve_execution_order;
pub use dispatcher::{OrchestratorDispatcher, WindowEventType};
pub use night_window::{NightWindow, NightWindowError};
pub use service::{AddJobResult, LoggingDispatcher, ScheduleDispatcher, SchedulerService};
pub use store::{InMemoryScheduleStore, ScheduleStore};

#[cfg(feature = "storage")]
pub use store::PostgresScheduleStore;

#[cfg(feature = "messaging")]
pub use dispatcher::publish_window_event;
