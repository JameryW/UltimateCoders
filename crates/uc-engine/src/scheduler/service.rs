//! Scheduler service — cron and one-shot task scheduling with night-window guard.
//!
//! Wraps `tokio-cron-scheduler` to provide:
//! - Cron-based recurring job scheduling
//! - One-shot delayed job scheduling
//! - Night-window guard: jobs are only dispatched within the configured window
//! - Job metadata tracking (description, project_id, etc.)
//! - Persistence via `ScheduleStore` (PostgreSQL or in-memory)
//!
//! The actual dispatch to Orchestrator happens via the `ScheduleDispatcher` trait,
//! which is injected as a dependency. For now, a no-op `LoggingDispatcher` is
//! provided; real integration with Orchestrator comes in PR4.

use chrono::Utc;
use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;
#[cfg(any(feature = "scheduler", test))]
use std::time::Duration;
use tokio::sync::RwLock;
use tracing::{info, warn};
use uuid::Uuid;

use uc_types::{EngineError, ExecutionHistory, ExecutionStatus, ScheduledTask};

use super::lock::{LockProvider, NoOpLockProvider};
use super::night_window::NightWindow;
use super::store::ScheduleStore;

/// Number of execution-history rows restored into the dashboard cache during
/// scheduler startup. The durable store remains the source of truth for older
/// records and task-specific queries; live recording keeps the existing
/// in-memory behavior for the default memory backend.
const EXECUTION_HISTORY_RECOVERY_LIMIT: i64 = 50;

/// Consecutive transport-level dispatch failures a one-shot may be re-armed for
/// before the scheduler gives up and leaves the `Skipped` record as the last
/// word. Bounded on purpose: if nothing will take the job four times, the
/// fifth attempt is a different problem than a transient one.
const MAX_DISPATCH_RETRIES: u32 = 3;

/// First retry delay, in seconds; doubles per failed attempt up to the ceiling.
const DISPATCH_RETRY_BASE_SECS: u64 = 60;

/// Ceiling on a single retry delay, so a long outage cannot push the next
/// attempt hours beyond the point where the cluster is already back.
const DISPATCH_RETRY_MAX_SECS: u64 = 900;

/// What caused a dispatch attempt, which decides how much it may change.
///
/// An operator trigger is a one-off poke at the *current* plan: on failure it
/// records the outcome but must neither spend the scheduled retry budget nor
/// move `next_execution`, or a single click on "Trigger" would silently
/// reschedule a run planned for later.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DispatchSource {
    /// The runtime scheduler fired the job on its own schedule.
    Scheduled,
    /// `TriggerSchedulerJob` / `/uc schedule trigger` — an explicit request.
    Manual,
}

/// How a dispatch attempt ended, which decides what happens to the retry budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DispatchOutcome {
    /// Accepted by the dispatcher — the budget resets for the next schedule.
    Succeeded,
    /// Transport failure that entitles a one-shot to one more scheduled retry.
    FailedRetryable,
    /// Failure observed *without* spending the budget: an operator trigger, or
    /// a cron job whose next tick already is its own retry.
    FailedObserved,
}

/// Trait for dispatching scheduled tasks to the execution engine.
///
/// Implementations handle the actual task execution (e.g., submitting
/// to the Orchestrator). This decoupling allows the scheduler to be
/// tested independently of the full engine.
pub trait ScheduleDispatcher: Send + Sync {
    /// Dispatch a scheduled task for execution.
    ///
    /// Called when a job fires and the night-window guard passes.
    fn dispatch(&self, task: &ScheduledTask) -> Result<(), EngineError>;
}

/// A no-op dispatcher that logs the dispatch but does nothing.
///
/// Used for testing and as a placeholder until Orchestrator integration.
pub struct LoggingDispatcher;

impl ScheduleDispatcher for LoggingDispatcher {
    fn dispatch(&self, task: &ScheduledTask) -> Result<(), EngineError> {
        info!(
            task_id = %task.id,
            description = %task.description,
            project_id = %task.project_id,
            "Scheduled task dispatched (logging only)"
        );
        Ok(())
    }
}

/// Metadata stored alongside each scheduled job.
#[derive(Debug, Clone)]
struct JobMetadata {
    /// The scheduled task definition.
    task: ScheduledTask,
}

/// Result of adding a job to the scheduler.
#[derive(Debug, Clone)]
pub struct AddJobResult {
    /// The UUID assigned to the scheduled task.
    pub task_id: Uuid,
}

/// The scheduler service.
///
/// Manages cron-based and one-shot scheduled tasks, with an optional
/// night-window guard that prevents execution outside configured hours.
/// Supports persistence via a `ScheduleStore` backend.
///
/// All fields are `Arc`-shared, so cloning a `SchedulerService` is a cheap
/// refcount bump. The clone shares the same state (jobs, dispatcher, store)
/// and is used to pass a handle into cron-scheduler callbacks (which need
/// `'static` ownership).
#[derive(Clone)]
pub struct SchedulerService {
    /// Night window configuration (if any).
    night_window: Arc<RwLock<Option<NightWindow>>>,
    /// Job metadata indexed by task ID.
    job_metadata: Arc<RwLock<HashMap<Uuid, JobMetadata>>>,
    /// Execution history records (in-memory cache; store is source of truth).
    execution_history: Arc<RwLock<Vec<ExecutionHistory>>>,
    /// The dispatcher for executing tasks.
    ///
    /// Wrapped in `RwLock` to allow late binding: the `EngineSubmitDispatcher`
    /// needs an `Arc<LocalEngine>`, but `LocalEngine` owns the
    /// `SchedulerService` — a chicken-and-egg. The engine constructs the
    /// service with a `LoggingDispatcher` placeholder, then calls
    /// `set_dispatcher` to swap in the real `EngineSubmitDispatcher` once
    /// the engine itself is fully constructed.
    dispatcher: Arc<RwLock<Arc<dyn ScheduleDispatcher>>>,
    /// Distributed lock provider for multi-instance coordination.
    ///
    /// Defaults to `NoOpLockProvider` (always acquire — single-instance).
    /// In multi-instance deployments with NATS, the gateway injects
    /// `NatsKvLockProvider` via `set_lock_provider` so only one instance
    /// fires each cron tick.
    ///
    /// Wrapped in `RwLock` for late binding (same pattern as `dispatcher`).
    lock_provider: Arc<RwLock<Arc<dyn LockProvider>>>,
    /// The persistence store for scheduled tasks and execution history.
    store: Arc<dyn ScheduleStore>,
    /// Whether the scheduler has been started.
    started: Arc<RwLock<bool>>,
    /// The tokio-cron-scheduler instance (when the `scheduler` feature is enabled).
    #[cfg(feature = "scheduler")]
    job_scheduler: Arc<RwLock<Option<tokio_cron_scheduler::JobScheduler>>>,
    /// Runtime scheduler UUIDs keyed by our durable task IDs.
    ///
    /// `tokio-cron-scheduler` generates its own UUID for every registered job.
    /// Keeping that opaque ID separate from `ScheduledTask::id` lets removal
    /// reliably unregister the runtime job without leaking scheduler details
    /// into the public scheduler interface.
    #[cfg(feature = "scheduler")]
    scheduler_job_ids: Arc<RwLock<HashMap<Uuid, Uuid>>>,
}

impl SchedulerService {
    /// Create a new scheduler service with a logging dispatcher and in-memory store.
    pub fn new() -> Self {
        Self::with_store_and_dispatcher(
            Arc::new(super::store::InMemoryScheduleStore::new()),
            Arc::new(LoggingDispatcher),
        )
    }

    /// Create a new scheduler service with a custom store and dispatcher.
    pub fn with_store_and_dispatcher(
        store: Arc<dyn ScheduleStore>,
        dispatcher: Arc<dyn ScheduleDispatcher>,
    ) -> Self {
        Self {
            night_window: Arc::new(RwLock::new(None)),
            job_metadata: Arc::new(RwLock::new(HashMap::new())),
            execution_history: Arc::new(RwLock::new(Vec::new())),
            dispatcher: Arc::new(RwLock::new(dispatcher)),
            lock_provider: Arc::new(RwLock::new(Arc::new(NoOpLockProvider))),
            store,
            started: Arc::new(RwLock::new(false)),
            #[cfg(feature = "scheduler")]
            job_scheduler: Arc::new(RwLock::new(None)),
            #[cfg(feature = "scheduler")]
            scheduler_job_ids: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    /// Create a new scheduler service with a custom dispatcher (in-memory store).
    pub fn with_dispatcher(dispatcher: Arc<dyn ScheduleDispatcher>) -> Self {
        Self::with_store_and_dispatcher(
            Arc::new(super::store::InMemoryScheduleStore::new()),
            dispatcher,
        )
    }

    /// Create a new scheduler service with a custom store (logging dispatcher).
    pub fn with_store(store: Arc<dyn ScheduleStore>) -> Self {
        Self::with_store_and_dispatcher(store, Arc::new(LoggingDispatcher))
    }

    /// Replace the dispatcher after construction.
    ///
    /// This enables late binding for `EngineSubmitDispatcher`, which needs
    /// an `Arc<LocalEngine>` — but `LocalEngine` owns the `SchedulerService`.
    /// The engine constructs the service with a `LoggingDispatcher` placeholder,
    /// then calls `set_dispatcher` to swap in the real dispatcher.
    ///
    /// Should be called before `start()` — if called after, existing
    /// tokio-cron-scheduler callbacks will pick up the new dispatcher on
    /// their next `dispatch_with_guard` call (the RwLock read is per-dispatch).
    pub async fn set_dispatcher(&self, dispatcher: Arc<dyn ScheduleDispatcher>) {
        let mut d = self.dispatcher.write().await;
        *d = dispatcher;
        info!("Scheduler dispatcher replaced");
    }

    /// Replace the lock provider after construction.
    ///
    /// This enables late binding for `NatsKvLockProvider`, which needs an
    /// established NATS connection — not available when `SchedulerService`
    /// is first constructed (inside `LocalEngine::new`). The gateway calls
    /// `set_lock_provider` after NATS is connected, swapping in the real
    /// distributed lock provider.
    ///
    /// Should be called before `start()` — if called after, existing
    /// cron-scheduler callbacks will pick up the new provider on their next
    /// fire (the RwLock read is per-fire).
    pub async fn set_lock_provider(&self, provider: Arc<dyn LockProvider>) {
        let mut lp = self.lock_provider.write().await;
        *lp = provider;
        info!("Scheduler lock provider replaced");
    }

    /// Set the night window configuration.
    pub async fn set_night_window(
        &self,
        config: &uc_types::NightWindowConfig,
    ) -> Result<(), EngineError> {
        let window = NightWindow::from_config(config)
            .map_err(|e| EngineError::ConfigError(format!("Invalid night window config: {}", e)))?;
        let mut nw = self.night_window.write().await;
        *nw = Some(window);
        info!("Night window configuration updated");
        Ok(())
    }

    /// Clear the night window configuration (allow execution at any time).
    pub async fn clear_night_window(&self) {
        let mut nw = self.night_window.write().await;
        *nw = None;
        info!("Night window configuration cleared");
    }

    /// Add a cron-based recurring job.
    ///
    /// The `cron_expression` should be a standard cron expression (e.g., "0 22 * * *").
    /// If a night window is configured, the job will only be dispatched within
    /// the window; otherwise it will be deferred to the next window.
    ///
    /// The task is persisted to the store and registered with the job scheduler.
    pub async fn add_cron_job(&self, mut task: ScheduledTask) -> Result<AddJobResult, EngineError> {
        let cron_expr = task.cron_expression.clone().ok_or_else(|| {
            EngineError::ConfigError("Cron expression required for cron job".to_string())
        })?;

        // Validate the cron expression using croner
        croner::Cron::from_str(&cron_expr).map_err(|e| {
            EngineError::ConfigError(format!("Invalid cron expression '{}': {:?}", cron_expr, e))
        })?;

        // Keep the persisted status useful to callers before the first tick.
        // The runtime scheduler has its own clock, so expose the same next
        // occurrence through the durable task record as soon as it is added.
        task.next_execution = Self::next_execution_for_task(&task, Utc::now())?;

        let task_id = task.id;

        // Persist to store
        self.store.save_task(&task).await?;

        // Register with job scheduler (if feature enabled and started)
        #[cfg(feature = "scheduler")]
        if task.enabled {
            let js = self.job_scheduler.read().await;
            if let Some(scheduler) = js.as_ref() {
                let scheduler_job_id = self.register_cron_with_scheduler(scheduler, &task).await?;
                self.scheduler_job_ids
                    .write()
                    .await
                    .insert(task_id, scheduler_job_id);
            }
        }

        // Store metadata locally
        let metadata = JobMetadata { task };
        self.job_metadata.write().await.insert(task_id, metadata);

        info!(
            task_id = %task_id,
            cron = %cron_expr,
            "Cron job added to scheduler"
        );

        Ok(AddJobResult { task_id })
    }

    /// Add a one-shot delayed job.
    ///
    /// The job will fire at the `execute_after` time specified in the task.
    /// If a night window is configured and the execution time is outside
    /// the window, the job will be deferred to the next window.
    ///
    /// The task is persisted to the store and registered with the job scheduler.
    pub async fn add_one_shot_job(
        &self,
        mut task: ScheduledTask,
    ) -> Result<AddJobResult, EngineError> {
        let execute_after = task.execute_after.ok_or_else(|| {
            EngineError::ConfigError("execute_after required for one-shot job".to_string())
        })?;

        // A future one-shot has exactly one known next occurrence. A past
        // timestamp is still persisted for compatibility, but is represented
        // as no longer pending rather than advertising an expired run.
        //
        // Deliberately derived from `execute_after` alone: after a night-window
        // deferral `next_execution` means "the attempt the scheduler owes", and
        // a brand-new job has never been owed anything.
        task.next_execution = (execute_after > Utc::now()).then_some(execute_after);

        let task_id = task.id;

        // Persist to store
        self.store.save_task(&task).await?;

        // Register with job scheduler (if feature enabled and started)
        #[cfg(feature = "scheduler")]
        if task.enabled {
            let js = self.job_scheduler.read().await;
            if let Some(scheduler) = js.as_ref() {
                if let Some(scheduler_job_id) = self
                    .register_one_shot_with_scheduler(scheduler, &task)
                    .await?
                {
                    self.scheduler_job_ids
                        .write()
                        .await
                        .insert(task_id, scheduler_job_id);
                }
            }
        }

        // Store metadata locally
        let metadata = JobMetadata { task };
        self.job_metadata.write().await.insert(task_id, metadata);

        info!(
            task_id = %task_id,
            execute_after = %execute_after,
            "One-shot job added to scheduler"
        );

        Ok(AddJobResult { task_id })
    }

    /// Remove a job from the scheduler.
    ///
    /// Removes from both the in-memory metadata and the persistence store.
    pub async fn remove_job(&self, task_id: &Uuid) -> Result<(), EngineError> {
        // Remove from store
        self.store.delete_task(task_id).await?;

        // Remove from local metadata
        let mut metadata = self.job_metadata.write().await;
        if metadata.remove(task_id).is_some() {
            info!(task_id = %task_id, "Job removed from scheduler");
        } else {
            // Already removed from store, but not in local metadata
            // This can happen during recovery. Not an error.
            info!(task_id = %task_id, "Job removed from store (not in local metadata)");
        }
        drop(metadata);

        // Remove from tokio-cron-scheduler if running
        #[cfg(feature = "scheduler")]
        self.unregister_runtime_job(task_id).await;

        Ok(())
    }

    /// Pause (`enabled = false`) or resume (`enabled = true`) a scheduled job
    /// without deleting it.
    ///
    /// Pausing is the non-destructive alternative to removal: removal deletes the
    /// durable record *and* cascades its execution history away, while pausing
    /// keeps both. It unregisters the job from the runtime scheduler and clears
    /// the advertised `next_execution`, so nothing continues to promise a run
    /// that will never happen. `last_execution` and history stay untouched.
    ///
    /// Resuming recomputes `next_execution` from the current clock — never the
    /// stale pre-pause value — and registers the job with the runtime scheduler
    /// again when the service is started. A one-shot whose `execute_after` has
    /// already passed behaves exactly as it does at creation time: the job is
    /// enabled again, has no pending run, and nothing is registered.
    ///
    /// The resulting state is written through the store, so a paused job is
    /// still paused *and still listed* after a gateway restart (see `start`),
    /// which is what makes it resumable later.
    ///
    /// Toggling a job to the state it is already in is a no-op: no scheduler
    /// churn, no durable write, no refreshed timestamps.
    pub async fn set_job_enabled(
        &self,
        task_id: &Uuid,
        enabled: bool,
    ) -> Result<ScheduledTask, EngineError> {
        let current = {
            let metadata = self.job_metadata.read().await;
            metadata
                .get(task_id)
                .map(|m| m.task.clone())
                .ok_or_else(|| {
                    EngineError::TaskError(format!("Scheduled task not found: {}", task_id))
                })?
        };

        if current.enabled == enabled {
            return Ok(current);
        }

        let mut candidate = current.clone();
        candidate.enabled = enabled;
        candidate.next_execution = if enabled {
            match Self::next_execution_for_task(&candidate, Utc::now()) {
                Ok(next) => next,
                Err(error) => {
                    // A legacy or malformed record cannot be recalculated. The
                    // job is still resumable — it just has no advertised run
                    // until it dispatches successfully once more.
                    warn!(
                        task_id = %task_id,
                        error = %error,
                        "Failed to calculate next execution while resuming scheduler job"
                    );
                    None
                }
            }
        } else {
            None
        };
        candidate.updated_at = Utc::now();

        // Apply the runtime registration *before* committing the visible
        // snapshot: a resume whose registration fails leaves the job exactly as
        // it was rather than half-applied (enabled but never firing).
        #[cfg(feature = "scheduler")]
        {
            if enabled {
                let js = self.job_scheduler.read().await;
                if let Some(scheduler) = js.as_ref() {
                    let registered = self.register_runtime_job(scheduler, &candidate).await;
                    drop(js);
                    // An expired one-shot has nothing left to register.
                    if let Some(scheduler_job_id) = registered? {
                        self.scheduler_job_ids
                            .write()
                            .await
                            .insert(*task_id, scheduler_job_id);
                    }
                }
            } else {
                self.unregister_runtime_job(task_id).await;
            }
        }

        {
            let mut metadata = self.job_metadata.write().await;
            if let Some(job) = metadata.get_mut(task_id) {
                job.task = candidate.clone();
            }
        }

        // Same persistence contract as the dispatch metadata update: a failed
        // durable write is logged, not fatal, because the in-memory snapshot and
        // the runtime scheduler already agree on the new state.
        if let Err(error) = self.store.update_task(&candidate).await {
            warn!(
                task_id = %task_id,
                error = %error,
                "Failed to persist scheduler job enabled state"
            );
        }

        info!(
            task_id = %task_id,
            enabled = enabled,
            description = %candidate.description,
            "Scheduler job enabled state updated"
        );

        Ok(candidate)
    }

    /// List all registered jobs in stable creation order.
    pub async fn list_jobs(&self) -> Vec<ScheduledTask> {
        let metadata = self.job_metadata.read().await;
        let mut jobs: Vec<_> = metadata.values().map(|m| m.task.clone()).collect();
        jobs.sort_by(|left, right| {
            left.created_at
                .cmp(&right.created_at)
                .then_with(|| left.id.cmp(&right.id))
        });
        jobs
    }

    /// Get a specific job by ID.
    pub async fn get_job(&self, task_id: &Uuid) -> Option<ScheduledTask> {
        let metadata = self.job_metadata.read().await;
        metadata.get(task_id).map(|m| m.task.clone())
    }

    /// Get the configured night window, if any.
    ///
    /// Returns the `NightWindowConfig` (start, end, timezone) that was set via
    /// `set_night_window`. Returns `None` if no night window is configured
    /// (execution allowed at any time). Used by `EngineApi::get_scheduler_status`
    /// to report the service-level window to the dashboard.
    pub async fn get_night_window_config(&self) -> Option<uc_types::NightWindowConfig> {
        let nw = self.night_window.read().await;
        nw.as_ref().map(|window| {
            uc_types::NightWindowConfig::new(window.start, window.end, window.tz.to_string())
        })
    }

    /// When the night window reopens, if it is currently closed.
    ///
    /// `None` means execution may proceed right now — either we are inside the
    /// window or none is configured. `Some(instant)` is the moment it reopens,
    /// which is what a deferred one-shot has to be re-armed for; the prose in
    /// `check_night_window` alone cannot schedule anything.
    async fn next_window_opening(&self) -> Option<chrono::DateTime<Utc>> {
        let nw = self.night_window.read().await;
        let window = nw.as_ref()?;
        let now = chrono::Utc::now().with_timezone(&window.tz);
        if window.is_within_window(now) {
            return None;
        }
        Some(window.next_window_start(now).with_timezone(&Utc))
    }

    /// The deferral error, shared by the public guard check and the dispatch
    /// path so the two cannot drift apart.
    fn outside_window_error(next_start: chrono::DateTime<Utc>) -> EngineError {
        EngineError::TaskError(format!(
            "Outside night window. Next window starts at {}",
            next_start
        ))
    }

    /// Check if a task should be executed now based on the night window guard.
    ///
    /// Returns Ok(()) if execution should proceed, or Err with a deferral reason
    /// if the task should be deferred to the next window.
    pub async fn check_night_window(&self) -> Result<(), EngineError> {
        match self.next_window_opening().await {
            None => {
                // Within the window, or no night window configured at all.
                Ok(())
            }
            Some(next_start) => Err(Self::outside_window_error(next_start)),
        }
    }

    /// Dispatch a task, respecting the night window guard.
    ///
    /// If the task is within the night window (or no window is configured),
    /// the task is dispatched immediately. Otherwise, an execution history
    /// record is created with Deferred status.
    pub async fn dispatch_with_guard(&self, task_id: &Uuid) -> Result<(), EngineError> {
        self.dispatch_with_guard_from(task_id, DispatchSource::Scheduled)
            .await
    }

    /// `dispatch_with_guard` with the origin of the attempt.
    ///
    /// Only a `Scheduled` fire may reschedule itself — the retry after a failed
    /// dispatch, or the re-arm after a night-window deferral. `Manual` records
    /// the outcome and leaves the plan alone: an operator pressing "Trigger" is
    /// asking for an extra run now, not for a schedule move.
    pub async fn dispatch_with_guard_from(
        &self,
        task_id: &Uuid,
        source: DispatchSource,
    ) -> Result<(), EngineError> {
        let task = {
            let metadata = self.job_metadata.read().await;
            metadata
                .get(task_id)
                .map(|m| m.task.clone())
                .ok_or_else(|| EngineError::TaskError(format!("Job {} not found", task_id)))?
        };

        // Check night window guard. Asking for the reopening *instant* (rather
        // than the prose error `check_night_window` returns) is what makes a
        // deferred one-shot re-armable.
        match self.next_window_opening().await {
            None => {
                // Within window — dispatch the task
                let started_at = Utc::now();
                let dispatcher = self.dispatcher.read().await.clone();
                match dispatcher.dispatch(&task) {
                    Ok(()) => {
                        self.mark_execution_started(
                            task_id,
                            started_at,
                            DispatchOutcome::Succeeded,
                        )
                        .await;
                        let history = ExecutionHistory {
                            id: Uuid::new_v4(),
                            scheduled_task_id: *task_id,
                            started_at,
                            completed_at: Some(Utc::now()),
                            status: ExecutionStatus::Completed,
                            result_summary: Some("Task dispatched successfully".to_string()),
                            deferred_reason: None,
                        };
                        self.record_execution(&history).await;
                        Ok(())
                    }
                    Err(e) => {
                        // Dispatch returned Err — NATS unavailable / no worker
                        // received the task. Record as Skipped (not Failed): the
                        // task itself is valid, it just never reached one. Only a
                        // one-shot can *lose* its single promised run that way; a
                        // cron job's next tick is its own retry, so it never
                        // spends the budget.
                        let spends_budget =
                            source == DispatchSource::Scheduled && task.is_one_shot();
                        let outcome = if spends_budget {
                            DispatchOutcome::FailedRetryable
                        } else {
                            DispatchOutcome::FailedObserved
                        };
                        let attempts = self
                            .mark_execution_started(task_id, started_at, outcome)
                            .await;

                        let retry_at = (spends_budget && attempts <= MAX_DISPATCH_RETRIES)
                            .then(|| started_at + Self::retry_delay_for(attempts));
                        let summary = match (retry_at, spends_budget) {
                            (Some(at), true) => format!(
                                "Dispatch skipped (no worker): {}; retry {} of {} at {}",
                                e, attempts, MAX_DISPATCH_RETRIES, at
                            ),
                            (None, true) => format!(
                                "Dispatch skipped (no worker): {}; retries exhausted after {} attempts",
                                e, attempts
                            ),
                            _ => format!("Dispatch skipped (no worker): {}", e),
                        };
                        let history = ExecutionHistory {
                            id: Uuid::new_v4(),
                            scheduled_task_id: *task_id,
                            started_at,
                            completed_at: Some(Utc::now()),
                            status: ExecutionStatus::Skipped,
                            result_summary: Some(summary),
                            deferred_reason: None,
                        };
                        self.record_execution(&history).await;

                        if let Some(at) = retry_at {
                            self.rearm_one_shot_at(task_id, at).await;
                        }
                        Err(e)
                    }
                }
            }
            Some(next_start) => {
                // Outside window — defer.
                let reason = Self::outside_window_error(next_start);
                let history = ExecutionHistory::deferred(*task_id, reason.to_string());
                self.record_execution(&history).await;
                warn!(
                    task_id = %task_id,
                    reason = %reason,
                    "Task deferred (outside night window)"
                );
                // A cron job has a next tick anyway, so the deferral costs it
                // nothing. A one-shot has just been consumed by the runtime
                // scheduler without ever dispatching — unless it is re-armed
                // for the moment the window reopens, the promised run is lost
                // while the Deferred record claims otherwise.
                if source == DispatchSource::Scheduled {
                    self.rearm_one_shot_at(task_id, next_start).await;
                }
                Err(reason)
            }
        }
    }

    /// Backoff delay before retry `attempt` (1-based) of a failed dispatch.
    ///
    /// Exponential from a minute, capped at fifteen: long enough that a worker
    /// rolling restart recovers on its own, short enough that the last retry is
    /// not hours after the cluster came back.
    fn retry_delay_for(attempt: u32) -> chrono::Duration {
        let factor = 1u64
            .checked_shl(attempt.saturating_sub(1))
            .unwrap_or(u64::MAX);
        let seconds = DISPATCH_RETRY_BASE_SECS
            .saturating_mul(factor)
            .min(DISPATCH_RETRY_MAX_SECS);
        chrono::Duration::seconds(seconds as i64)
    }

    /// Record an execution history entry (both in-memory and to the store).
    ///
    /// Public so that late-binding dispatchers (e.g., `EngineSubmitDispatcher`)
    /// can append a `Failed` entry when a fire-and-forget `submit_task` fails
    /// asynchronously — `dispatch_with_guard` will have already recorded a
    /// `Completed` entry (spawn succeeded), and this appends the failure
    /// outcome so the history is not misleading.
    pub async fn record_execution(&self, history: &ExecutionHistory) {
        // Save to store (best-effort; log errors but don't fail the dispatch)
        if let Err(e) = self.store.save_execution(history).await {
            warn!(error = %e, "Failed to persist execution history to store");
        }
        // Also save to in-memory cache
        self.execution_history.write().await.push(history.clone());
    }

    /// Get the execution history for all tasks, or a specific task from the
    /// dashboard cache. The cache is hydrated from persistence during startup
    /// and updated as dispatch attempts complete.
    pub async fn get_execution_history(&self, task_id: Option<&Uuid>) -> Vec<ExecutionHistory> {
        let history = self.execution_history.read().await;
        match task_id {
            Some(id) => history
                .iter()
                .filter(|h| &h.scheduled_task_id == id)
                .cloned()
                .collect(),
            None => history.clone(),
        }
    }

    /// Get execution history from the store for a specific task.
    pub async fn get_execution_history_from_store(
        &self,
        task_id: &Uuid,
        limit: i64,
    ) -> Result<Vec<ExecutionHistory>, EngineError> {
        self.store.list_executions(task_id, limit).await
    }

    /// Restore recent persisted execution history into the in-memory cache.
    ///
    /// `ScheduleStore::list_executions` intentionally returns newest-first,
    /// while the live cache is appended in dispatch order. Merge all task
    /// slices and sort ascending so the cache has one deterministic ordering
    /// regardless of whether entries came from a live dispatch or a restart.
    /// History is best-effort during recovery: an unavailable task slice must
    /// not prevent healthy scheduled jobs from starting.
    async fn recover_execution_history(&self, tasks: &[ScheduledTask]) {
        let mut recovered_history = Vec::new();

        for task in tasks {
            match self
                .store
                .list_executions(&task.id, EXECUTION_HISTORY_RECOVERY_LIMIT)
                .await
            {
                Ok(mut history) => recovered_history.append(&mut history),
                Err(error) => {
                    warn!(
                        task_id = %task.id,
                        error = %error,
                        "Failed to recover scheduler execution history"
                    );
                }
            }
        }

        recovered_history.sort_by(|left, right| {
            left.started_at
                .cmp(&right.started_at)
                .then_with(|| left.id.cmp(&right.id))
        });

        // A task can contribute up to the recovery limit, so cap the merged
        // dashboard cache as well instead of allowing many tasks to expand it
        // without bound during a restart.
        let cache_limit = EXECUTION_HISTORY_RECOVERY_LIMIT as usize;
        if recovered_history.len() > cache_limit {
            let drop_count = recovered_history.len() - cache_limit;
            recovered_history.drain(..drop_count);
        }

        *self.execution_history.write().await = recovered_history;
    }

    /// Start the scheduler.
    ///
    /// When the `scheduler` feature is enabled, this creates a `JobScheduler`
    /// and registers all persisted enabled tasks with it.
    /// When the feature is disabled, it just marks the service as started.
    pub async fn start(&self) -> Result<(), EngineError> {
        let mut started = self.started.write().await;
        if *started {
            warn!("Scheduler is already started");
            return Ok(());
        }

        // Load persisted tasks into local metadata. Every persisted job is
        // recovered — paused ones included — so a pause survives a restart as a
        // visible, resumable job rather than silently disappearing from the
        // registry (and from the dashboard that reads it). Enabled jobs are
        // additionally registered with the runtime scheduler below.
        //
        // Recompute the next occurrence from the current clock so a restart
        // never exposes a stale timestamp left behind by the previous process.
        let persisted_tasks = self.store.list_tasks(false).await?;
        self.recover_execution_history(&persisted_tasks).await;
        let recovery_now = Utc::now();
        let mut recovered_tasks = Vec::with_capacity(persisted_tasks.len());
        for task in persisted_tasks {
            let mut recovered = task.clone();
            let refreshed = if task.enabled {
                Self::next_execution_for_task(&task, recovery_now)
            } else {
                // Paused jobs advertise no pending run. Self-heal records that
                // were paused by an older build, or whose durable write raced.
                Ok(None)
            };
            match refreshed {
                Ok(next_execution) if next_execution != task.next_execution => {
                    recovered.next_execution = next_execution;
                    recovered.updated_at = Utc::now();
                    if let Err(error) = self.store.update_task(&recovered).await {
                        warn!(
                            task_id = %task.id,
                            error = %error,
                            "Failed to persist refreshed scheduler recovery metadata"
                        );
                    }
                }
                Ok(_) => {}
                Err(error) => {
                    // Keep a previously persisted value when a legacy or
                    // malformed record cannot be recalculated. Registration
                    // will report the same error without preventing startup.
                    warn!(
                        task_id = %task.id,
                        error = %error,
                        "Failed to refresh scheduler recovery metadata"
                    );
                }
            }
            // A one-shot whose instant passed while the gateway was down must
            // not vanish silently — record the miss, so the run the operator
            // promised themselves still shows up in the history the dashboard
            // already renders.
            if recovered.enabled {
                self.record_missed_one_shot(&recovered).await;
            }

            recovered_tasks.push(recovered);
        }

        let mut metadata = self.job_metadata.write().await;
        for task in &recovered_tasks {
            metadata.insert(task.id, JobMetadata { task: task.clone() });
        }
        drop(metadata); // Release lock before starting scheduler

        #[cfg(feature = "scheduler")]
        {
            let job_scheduler = tokio_cron_scheduler::JobScheduler::new()
                .await
                .map_err(|e| {
                    EngineError::InternalError(format!("Failed to create job scheduler: {:?}", e))
                })?;

            // Register the enabled tasks with the job scheduler. Paused tasks
            // are loaded into the registry so they stay visible and resumable,
            // but they must not occupy the runtime scheduler.
            for task in &recovered_tasks {
                if !task.enabled {
                    continue;
                }
                match self.register_runtime_job(&job_scheduler, task).await {
                    Ok(Some(scheduler_job_id)) => {
                        self.scheduler_job_ids
                            .write()
                            .await
                            .insert(task.id, scheduler_job_id);
                    }
                    Ok(None) => {}
                    Err(e) => {
                        warn!(
                            task_id = %task.id,
                            error = %e,
                            "Failed to register persisted task with scheduler during recovery"
                        );
                    }
                }
            }

            // Start the job scheduler
            job_scheduler.start().await.map_err(|e| {
                EngineError::InternalError(format!("Failed to start job scheduler: {:?}", e))
            })?;

            let mut js = self.job_scheduler.write().await;
            *js = Some(job_scheduler);
        }

        *started = true;
        info!(
            task_count = recovered_tasks.len(),
            "Scheduler service started (recovered persisted tasks)"
        );
        Ok(())
    }

    /// Stop the scheduler.
    pub async fn stop(&self) -> Result<(), EngineError> {
        let mut started = self.started.write().await;
        if !*started {
            warn!("Scheduler is not started");
            return Ok(());
        }

        #[cfg(feature = "scheduler")]
        {
            let mut js = self.job_scheduler.write().await;
            if let Some(scheduler) = js.take() {
                let mut scheduler = scheduler;
                scheduler.shutdown().await.map_err(|e| {
                    EngineError::InternalError(format!("Failed to stop job scheduler: {:?}", e))
                })?;
            }
            self.scheduler_job_ids.write().await.clear();
        }

        *started = false;
        info!("Scheduler service stopped");
        Ok(())
    }

    /// Whether the scheduler is currently running.
    pub async fn is_running(&self) -> bool {
        *self.started.read().await
    }

    /// Get the number of registered jobs.
    pub async fn job_count(&self) -> usize {
        self.job_metadata.read().await.len()
    }

    /// Calculate the next cron occurrence after `after` using the same
    /// six-field representation passed to `tokio-cron-scheduler`.
    ///
    /// Keeping this calculation behind the scheduler service's interface
    /// gives callers a consistent `next_execution` value without exposing
    /// the runtime scheduler's opaque job type.
    fn next_cron_execution(
        cron_expression: &str,
        after: chrono::DateTime<Utc>,
    ) -> Result<chrono::DateTime<Utc>, EngineError> {
        let cron = croner::Cron::from_str(&Self::cron_to_6field(cron_expression)).map_err(|e| {
            EngineError::ConfigError(format!(
                "Invalid cron expression '{}': {:?}",
                cron_expression, e
            ))
        })?;

        cron.find_next_occurrence(&after, false).map_err(|e| {
            EngineError::ConfigError(format!(
                "Unable to calculate next execution for cron '{}': {:?}",
                cron_expression, e
            ))
        })
    }

    /// Return the next persisted occurrence for either supported schedule
    /// shape. A one-shot in the past is intentionally represented as `None`;
    /// it is no longer pending even though its history remains queryable.
    fn next_execution_for_task(
        task: &ScheduledTask,
        after: chrono::DateTime<Utc>,
    ) -> Result<Option<chrono::DateTime<Utc>>, EngineError> {
        if let Some(cron_expression) = task.cron_expression.as_deref() {
            return Self::next_cron_execution(cron_expression, after).map(Some);
        }

        Ok(Self::one_shot_attempt_at(task, after))
    }

    /// The instant the scheduler still owes this one-shot an attempt.
    ///
    /// Normally that is the operator's `execute_after`. But after a night-window
    /// deferral the original instant has passed and the *only* thing pointing at
    /// a real future attempt is the re-armed `next_execution`, so recovery and
    /// runtime registration must prefer it — otherwise a job deferred at 03:59
    /// silently loses the retry the deferral record just promised.
    fn one_shot_attempt_at(
        task: &ScheduledTask,
        after: chrono::DateTime<Utc>,
    ) -> Option<chrono::DateTime<Utc>> {
        task.next_execution
            .filter(|next| *next > after)
            .or_else(|| {
                task.execute_after
                    .filter(|execute_after| *execute_after > after)
            })
    }

    /// Convert a one-shot timestamp into the runtime scheduler delay while
    /// preserving sub-second precision. Returning `None` is intentional for
    /// an already-expired task: it remains persisted, but cannot be scheduled
    /// in the past.
    #[cfg(feature = "scheduler")]
    fn one_shot_duration(
        execute_after: chrono::DateTime<Utc>,
        now: chrono::DateTime<Utc>,
    ) -> Result<Option<std::time::Duration>, EngineError> {
        let duration = execute_after.signed_duration_since(now);
        if duration <= chrono::Duration::zero() {
            return Ok(None);
        }

        duration.to_std().map(Some).map_err(|e| {
            EngineError::ConfigError(format!(
                "Invalid one-shot delay until {}: {}",
                execute_after, e
            ))
        })
    }

    /// Update the durable and in-memory schedule snapshot after a dispatch
    /// attempt, and return the job's attempt count afterwards. The dispatcher is
    /// synchronous and may only acknowledge that a background submission was
    /// started, so this timestamp represents the scheduler's dispatch boundary
    /// rather than eventual worker completion.
    async fn mark_execution_started(
        &self,
        task_id: &Uuid,
        started_at: chrono::DateTime<Utc>,
        outcome: DispatchOutcome,
    ) -> u32 {
        let updated_task = {
            let mut metadata = self.job_metadata.write().await;
            let Some(job) = metadata.get_mut(task_id) else {
                // Gone from the registry (removed concurrently). Nothing to
                // account for, and the caller's retry re-arm is a no-op.
                return 0;
            };

            // A manual trigger can race a cron callback. Never let a slower
            // completion move the visible last-run timestamp backwards. The
            // retry budget is updated under the same guard, so an out-of-order
            // completion cannot double-count an attempt.
            if job
                .task
                .last_execution
                .map(|last| started_at >= last)
                .unwrap_or(true)
            {
                job.task.last_execution = Some(started_at);
                job.task.dispatch_attempts = match outcome {
                    DispatchOutcome::Succeeded => 0,
                    DispatchOutcome::FailedRetryable => {
                        job.task.dispatch_attempts.saturating_add(1)
                    }
                    DispatchOutcome::FailedObserved => job.task.dispatch_attempts,
                };
                job.task.next_execution = match Self::next_execution_for_task(&job.task, started_at)
                {
                    Ok(next) => next,
                    Err(error) => {
                        warn!(
                            task_id = %task_id,
                            error = %error,
                            "Failed to calculate next scheduler execution after dispatch"
                        );
                        None
                    }
                };
                // A paused job can still be triggered manually (that is an
                // explicit operator action), but it must not start advertising a
                // run the runtime scheduler is no longer registered to fire.
                if !job.task.enabled {
                    job.task.next_execution = None;
                }
            }
            job.task.updated_at = Utc::now();
            job.task.clone()
        };

        // The scheduler must remain usable if a secondary persistence write
        // fails. The in-memory snapshot is already updated and recovery will
        // reconcile it on the next successful store read.
        if let Err(error) = self.store.update_task(&updated_task).await {
            warn!(
                task_id = %task_id,
                error = %error,
                "Failed to persist scheduler execution metadata"
            );
        }

        updated_task.dispatch_attempts
    }

    /// Surface a one-shot whose scheduled instant passed while the gateway was
    /// not running.
    ///
    /// `one_shot_duration` cannot schedule into the past, so a one-shot that
    /// expired during downtime is simply not registered. Deliberately *not*
    /// firing it late either: an operator picked that instant for a reason (a
    /// maintenance window, a quiet hour), and the moment a gateway happens to
    /// boot is not a good one to spring a scheduled run.
    ///
    /// What must not happen is the silent disappearance of a promised run, so
    /// append one `Skipped` record stamped with the instant it should have
    /// fired — which is exactly what the dashboard's execution-history list and
    /// the `uc_scheduler` status output already render.
    ///
    /// Suppressed once anything proves a dispatch attempt at or after that
    /// instant: `last_execution` moved, or a `Completed` / `Failed` / `Skipped`
    /// history row exists — including the miss a previous restart recorded,
    /// which is what makes this idempotent across repeated restarts. A
    /// `Deferred` row does **not** suppress: the guard refused to dispatch, and
    /// a one-shot is never retried after that, so the promised run is still
    /// missing. Cron jobs never produce a miss: a cron expression is a standing
    /// schedule rather than a single promised run, so recovery only recomputes
    /// its next occurrence.
    async fn record_missed_one_shot(&self, task: &ScheduledTask) {
        let Some(execute_after) = task.execute_after else {
            return;
        };
        if task.cron_expression.is_some() {
            return;
        }
        if Self::one_shot_attempt_at(task, Utc::now()).is_some() {
            // Still owed a future attempt — the original instant, or a retry
            // re-armed by a previous deferral. Nothing has been missed yet.
            return;
        }
        if task
            .last_execution
            .is_some_and(|last| last >= execute_after)
        {
            return; // already dispatched at or after the scheduled instant
        }

        match self.store.list_executions(&task.id, 1).await {
            Ok(latest) => {
                if latest.iter().any(|entry| {
                    entry.started_at >= execute_after
                        // `Deferred` is deliberately NOT a suppression signal:
                        // it means the night-window guard refused to dispatch,
                        // and a one-shot is never retried after that — so the
                        // promised run is still missing. Completed / Failed /
                        // Skipped all prove an attempt was made (and the miss we
                        // recorded on an earlier restart suppresses a repeat).
                        && !matches!(entry.status, ExecutionStatus::Deferred)
                }) {
                    return;
                }
            }
            Err(error) => {
                // Without a dedup signal, writing could duplicate the record on
                // every restart. Leave it unwritten and say so.
                warn!(
                    task_id = %task.id,
                    error = %error,
                    "Skipped recording a missed one-shot: execution history unavailable"
                );
                return;
            }
        }

        let missed = ExecutionHistory {
            id: Uuid::new_v4(),
            scheduled_task_id: task.id,
            // The instant it should have run, so the timeline shows when the
            // promise was broken rather than when this process noticed.
            started_at: execute_after,
            // Intentionally unset: pairing it with "now" would render as a run
            // duration, not as the length of the downtime.
            completed_at: None,
            status: ExecutionStatus::Skipped,
            result_summary: Some(format!(
                "Missed: execute_after {} passed while the gateway scheduler was not running",
                execute_after
            )),
            deferred_reason: None,
        };

        self.insert_execution_history_ordered(&missed).await;

        warn!(
            task_id = %task.id,
            execute_after = %execute_after,
            description = %task.description,
            "One-shot job expired while the gateway was down — recorded as Skipped"
        );
    }

    /// Persist a recovered history entry and place it at its chronological
    /// position in the dashboard cache.
    ///
    /// `recover_execution_history` keeps the cache ascending by `started_at`,
    /// while `record_execution` appends — correct for live dispatches, which
    /// carry the current time. A recovered miss is stamped with a past instant,
    /// so appending it would break that ordering for every reader of the
    /// snapshot.
    async fn insert_execution_history_ordered(&self, history: &ExecutionHistory) {
        if let Err(error) = self.store.save_execution(history).await {
            warn!(
                error = %error,
                "Failed to persist recovered scheduler execution history"
            );
        }
        let mut cache = self.execution_history.write().await;
        let position = cache.partition_point(|entry| entry.started_at <= history.started_at);
        cache.insert(position, history.clone());
    }

    /// Re-arm a one-shot for a future attempt the scheduler now owes.
    ///
    /// Two callers, same shape: the night-window guard refused a run the consumed
    /// runtime job will never retry (the window `opening`), and a transport
    /// failure entitles the job to one backoff retry (the `retry_at`). Without
    /// it, the promised run disappears behind a record worded as if it will
    /// happen. Committing the instant as the owed attempt makes the retry real,
    /// visible (`next_run` shows when it will actually be retried) and durable —
    /// recovery reads the same field, so a restart in between does not lose it.
    ///
    /// Reads the current registry snapshot rather than trusting a caller's copy:
    /// `mark_execution_started` has just written the attempt count, and
    /// overwriting the whole task from a stale clone would erase it. An already
    /// owed attempt is also never moved *earlier*, so a later event cannot pull
    /// a pending retry forward.
    ///
    /// Cron jobs need nothing — their next tick is already a future attempt.
    async fn rearm_one_shot_at(&self, task_id: &Uuid, when: chrono::DateTime<Utc>) {
        let rearmed = {
            let mut metadata = self.job_metadata.write().await;
            let Some(job) = metadata.get_mut(task_id) else {
                return;
            };
            if job.task.cron_expression.is_some() || job.task.execute_after.is_none() {
                return;
            }
            let target = match Self::one_shot_attempt_at(&job.task, Utc::now()) {
                Some(owed) if owed > when => owed,
                _ => when,
            };
            job.task.next_execution = Some(target);
            job.task.updated_at = Utc::now();
            job.task.clone()
        };

        if let Err(error) = self.store.update_task(&rearmed).await {
            warn!(
                task_id = %task_id,
                error = %error,
                "Failed to persist re-armed one-shot retry time"
            );
        }

        #[cfg(feature = "scheduler")]
        {
            // Detached rather than awaited (see `rearm_registration_task`). The
            // retry time is already committed above, so a process that dies
            // before this task runs still re-registers on the next boot.
            tokio::spawn(Self::rearm_registration_task(self.clone(), rearmed.clone()));
        }
    }

    /// Build the detached task that registers a re-armed one-shot.
    ///
    /// The declared `BoxFuture` return type is load-bearing, not stylistic. This
    /// path otherwise closes a type cycle: `register_one_shot_with_scheduler`'s
    /// future contains the fired-callback future, which awaits
    /// `dispatch_with_guard`, which awaits `rearm_deferred_one_shot`, which needs
    /// a registration future again. Naming an erased type here is what lets
    /// inference terminate.
    #[cfg(feature = "scheduler")]
    fn rearm_registration_task(
        svc: SchedulerService,
        task: ScheduledTask,
    ) -> futures::future::BoxFuture<'static, ()> {
        Box::pin(async move {
            let task_id = task.id;
            let js = svc.job_scheduler.read().await;
            let Some(scheduler) = js.as_ref() else {
                // Not started (or stopped): the persisted retry time alone is
                // enough, since `start()` re-registers from it on the next boot.
                return;
            };
            let registered = svc.register_one_shot_with_scheduler(scheduler, &task).await;

            match registered {
                Ok(Some(scheduler_job_id)) => {
                    // Replace rather than overwrite. A concurrent re-arm of the
                    // same task (an operator trigger racing the due callback)
                    // would otherwise leave a runtime job that keeps firing while
                    // `remove_job` can no longer find it.
                    let previous = svc
                        .scheduler_job_ids
                        .write()
                        .await
                        .insert(task_id, scheduler_job_id);
                    if let Some(previous) = previous {
                        if let Err(error) = scheduler.remove(&previous).await {
                            warn!(
                                task_id = %task_id,
                                error = ?error,
                                "Failed to drop a superseded re-armed runtime job"
                            );
                        }
                    }
                    info!(
                        task_id = %task_id,
                        retry_at = %task
                            .next_execution
                            .map(|next| next.to_rfc3339())
                            .unwrap_or_default(),
                        "Deferred one-shot re-armed for the next window"
                    );
                }
                Ok(None) => {}
                Err(error) => {
                    warn!(
                        task_id = %task_id,
                        error = %error,
                        "Failed to re-arm a deferred one-shot with the runtime scheduler"
                    );
                }
            }
        })
    }

    /// Forget the runtime UUID for a one-shot job after it fires.
    ///
    /// One-shot jobs are removed by `tokio-cron-scheduler` after execution;
    /// clearing our lookup here prevents a later explicit removal from trying
    /// to delete an already-consumed runtime job.
    #[cfg(feature = "scheduler")]
    async fn forget_scheduler_job_id(&self, task_id: &Uuid) {
        self.scheduler_job_ids.write().await.remove(task_id);
    }

    // ── tokio-cron-scheduler integration ─────────────────────────

    /// Register a task with the runtime scheduler, picking the job shape from
    /// the persisted definition (cron expression wins over `execute_after`).
    ///
    /// Returns `None` when the task carries neither shape, or when a one-shot's
    /// target time has already passed and therefore cannot be scheduled.
    #[cfg(feature = "scheduler")]
    async fn register_runtime_job(
        &self,
        scheduler: &tokio_cron_scheduler::JobScheduler,
        task: &ScheduledTask,
    ) -> Result<Option<Uuid>, EngineError> {
        if task.cron_expression.is_some() {
            self.register_cron_with_scheduler(scheduler, task)
                .await
                .map(Some)
        } else if task.execute_after.is_some() {
            self.register_one_shot_with_scheduler(scheduler, task).await
        } else {
            Ok(None)
        }
    }

    /// Drop a task's runtime registration, if any, and forget its scheduler UUID.
    ///
    /// Used by both removal and pause, so the two can never drift apart: a job
    /// that leaves the active schedule must also stop occupying the runtime
    /// scheduler. A missing registration is not an error.
    #[cfg(feature = "scheduler")]
    async fn unregister_runtime_job(&self, task_id: &Uuid) {
        let js = self.job_scheduler.read().await;
        let scheduler_job_id = self.scheduler_job_ids.read().await.get(task_id).copied();
        if let (Some(scheduler), Some(scheduler_job_id)) = (js.as_ref(), scheduler_job_id) {
            if let Err(e) = scheduler.remove(&scheduler_job_id).await {
                warn!(task_id = %task_id, error = ?e, "Failed to remove job from tokio-cron-scheduler");
            }
        }
        drop(js);
        self.scheduler_job_ids.write().await.remove(task_id);
    }

    /// Convert a 5-field cron expression (standard: min hour day month dow)
    /// to a 6-field expression (with seconds) as required by tokio-cron-scheduler.
    ///
    /// If the expression already has 6+ fields, it is returned as-is.
    fn cron_to_6field(expr: &str) -> String {
        let parts: Vec<&str> = expr.split_whitespace().collect();
        if parts.len() == 5 {
            format!("0 {}", expr)
        } else {
            expr.to_string()
        }
    }

    /// Register a cron task with the job scheduler.
    #[cfg(feature = "scheduler")]
    async fn register_cron_with_scheduler(
        &self,
        scheduler: &tokio_cron_scheduler::JobScheduler,
        task: &ScheduledTask,
    ) -> Result<Uuid, EngineError> {
        let cron_expr = task
            .cron_expression
            .as_ref()
            .ok_or_else(|| EngineError::ConfigError("Cron expression missing".to_string()))?;

        let cron_6field = Self::cron_to_6field(cron_expr);

        let task_id = task.id;
        let task_description = task.description.clone();
        // Clone the service handle into the closure so it can call
        // dispatch_with_guard on fire. All fields are Arc, so this is cheap.
        let svc = self.clone();

        let job = tokio_cron_scheduler::Job::new_async(cron_6field, move |uuid, _l| {
            let task_id = task_id;
            let description = task_description.clone();
            let svc = svc.clone();
            Box::pin(async move {
                tracing::info!(
                    job_uuid = %uuid,
                    task_id = %task_id,
                    description = %description,
                    "Cron job triggered by scheduler — acquiring lock"
                );

                // Acquire distributed lock so only one gateway instance fires
                // this cron tick. Lock key = scheduler:{task_id}:{tick_timestamp}.
                // The tick timestamp is truncated to the second — all instances
                // fire at the same wall-clock second, so the key is deterministic.
                // TTL = 30s (auto-release on crash). NoOpLockProvider (default)
                // always acquires (single-instance = no coordination needed).
                let tick_ts = Utc::now().timestamp();
                let lock_key = format!("scheduler:{}:{}", task_id, tick_ts);
                let lock_ttl = Duration::from_secs(30);
                let lock_provider = svc.lock_provider.read().await.clone();
                if !lock_provider.try_acquire(&lock_key, lock_ttl) {
                    tracing::info!(
                        task_id = %task_id,
                        lock_key = %lock_key,
                        "Cron tick skipped — another instance holds the lock"
                    );
                    return;
                }

                // Lock acquired — dispatch with night-window guard. This calls the
                // EngineSubmitDispatcher (or whatever dispatcher is set),
                // which spawns engine.submit_task as fire-and-forget.
                if let Err(e) = svc.dispatch_with_guard(&task_id).await {
                    tracing::warn!(
                        task_id = %task_id,
                        error = %e,
                        "Cron dispatch_with_guard failed (deferred or error)"
                    );
                }
            })
        })
        .map_err(|e| EngineError::InternalError(format!("Failed to create cron job: {:?}", e)))?;

        scheduler.add(job).await.map_err(|e| {
            EngineError::InternalError(format!("Failed to add cron job to scheduler: {:?}", e))
        })
    }

    /// Register a one-shot task with the job scheduler.
    ///
    /// Calculates the duration from now until `execute_after` and schedules
    /// a one-shot job. If the time has already passed, logs a warning and
    /// does not schedule the job.
    #[cfg(feature = "scheduler")]
    async fn register_one_shot_with_scheduler(
        &self,
        scheduler: &tokio_cron_scheduler::JobScheduler,
        task: &ScheduledTask,
    ) -> Result<Option<Uuid>, EngineError> {
        if task.execute_after.is_none() {
            return Err(EngineError::ConfigError(
                "execute_after missing".to_string(),
            ));
        }

        // The owed attempt, not necessarily the original request: a one-shot
        // already deferred by the night window carries its retry time in
        // `next_execution`, and scheduling against `execute_after` alone would
        // drop exactly the retry the deferral promised.
        let now = Utc::now();
        let Some(attempt_at) = Self::one_shot_attempt_at(task, now) else {
            warn!(
                task_id = %task.id,
                execute_after = %task.execute_after.map(|t| t.to_rfc3339()).unwrap_or_default(),
                "One-shot job has no future attempt instant, skipping scheduler registration"
            );
            return Ok(None);
        };
        let Some(duration_std) = Self::one_shot_duration(attempt_at, now)? else {
            return Ok(None);
        };
        let task_id = task.id;
        let task_description = task.description.clone();
        // Clone the service handle into the closure so it can call
        // dispatch_with_guard on fire. All fields are Arc, so this is cheap.
        let svc = self.clone();

        let job = tokio_cron_scheduler::Job::new_one_shot_async(duration_std, move |uuid, _l| {
            let task_id = task_id;
            let description = task_description.clone();
            let svc = svc.clone();
            Box::pin(async move {
                tracing::info!(
                    job_uuid = %uuid,
                    task_id = %task_id,
                    description = %description,
                    "One-shot job triggered by scheduler — acquiring lock"
                );

                // The runtime one-shot is consumed the moment it fires, so
                // forget its UUID *before* doing anything else: a retry
                // re-armed later in this callback (see
                // `rearm_deferred_one_shot`) would otherwise have its fresh
                // mapping erased by a trailing forget here.
                svc.forget_scheduler_job_id(&task_id).await;

                // Acquire distributed lock (same as cron callback).
                let tick_ts = Utc::now().timestamp();
                let lock_key = format!("scheduler:{}:{}", task_id, tick_ts);
                let lock_ttl = Duration::from_secs(30);
                let lock_provider = svc.lock_provider.read().await.clone();
                if !lock_provider.try_acquire(&lock_key, lock_ttl) {
                    tracing::info!(
                        task_id = %task_id,
                        lock_key = %lock_key,
                        "One-shot tick skipped — another instance holds the lock"
                    );
                    return;
                }

                // Lock acquired — dispatch with night-window guard.
                if let Err(e) = svc.dispatch_with_guard(&task_id).await {
                    tracing::warn!(
                        task_id = %task_id,
                        error = %e,
                        "One-shot dispatch_with_guard failed (deferred or error)"
                    );
                }
            })
        })
        .map_err(|e| {
            EngineError::InternalError(format!("Failed to create one-shot job: {:?}", e))
        })?;

        let scheduler_job_id = scheduler.add(job).await.map_err(|e| {
            EngineError::InternalError(format!("Failed to add one-shot job to scheduler: {:?}", e))
        })?;

        Ok(Some(scheduler_job_id))
    }
}

impl Default for SchedulerService {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{DateTime, NaiveTime, Utc};

    fn make_cron_task(cron: &str) -> ScheduledTask {
        ScheduledTask::cron(
            "Test task".to_string(),
            "test-project".to_string(),
            cron.to_string(),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        )
    }

    fn make_one_shot_task(execute_after: DateTime<Utc>) -> ScheduledTask {
        ScheduledTask::one_shot(
            "Test one-shot".to_string(),
            "test-project".to_string(),
            execute_after,
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        )
    }

    #[tokio::test]
    async fn scheduler_service_create() {
        let service = SchedulerService::new();
        assert!(!service.is_running().await);
        assert_eq!(service.job_count().await, 0);
    }

    #[tokio::test]
    async fn scheduler_service_start_stop() {
        let service = SchedulerService::new();
        service.start().await.unwrap();
        assert!(service.is_running().await);
        service.stop().await.unwrap();
        assert!(!service.is_running().await);
    }

    #[tokio::test]
    async fn add_cron_job() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();
        assert!(!result.task_id.to_string().is_empty());
        assert_eq!(service.job_count().await, 1);
    }

    #[tokio::test]
    async fn add_cron_job_populates_next_execution() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        let stored = service.get_job(&result.task_id).await.unwrap();
        assert!(
            stored.next_execution.is_some(),
            "cron jobs should expose their next occurrence immediately"
        );
        assert!(stored.next_execution.unwrap() > Utc::now());
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn disabled_cron_job_is_not_registered_with_runtime_scheduler() {
        let service = SchedulerService::new();
        service.start().await.unwrap();

        let mut task = make_cron_task("* * * * * *");
        task.enabled = false;
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();

        assert!(service.get_job(&task_id).await.is_some());
        assert!(
            !service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "disabled jobs must remain persisted/visible but must not be scheduled"
        );

        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn add_cron_job_invalid_expression() {
        let service = SchedulerService::new();
        let task = ScheduledTask::cron(
            "Bad task".to_string(),
            "test-project".to_string(),
            "invalid cron".to_string(),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        );
        let result = service.add_cron_job(task).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn add_one_shot_job() {
        let service = SchedulerService::new();
        let later = Utc::now() + chrono::Duration::hours(8);
        let task = make_one_shot_task(later);
        let result = service.add_one_shot_job(task).await.unwrap();
        assert!(!result.task_id.to_string().is_empty());
        assert_eq!(service.job_count().await, 1);
    }

    #[tokio::test]
    async fn add_one_shot_job_populates_next_execution() {
        let service = SchedulerService::new();
        let later = Utc::now() + chrono::Duration::hours(8);
        let task = make_one_shot_task(later);
        let result = service.add_one_shot_job(task).await.unwrap();

        let stored = service.get_job(&result.task_id).await.unwrap();
        assert_eq!(stored.next_execution, Some(later));
    }

    #[tokio::test]
    async fn add_one_shot_job_missing_execute_after() {
        let service = SchedulerService::new();
        let task = ScheduledTask::new(
            "No execute_after".to_string(),
            "test-project".to_string(),
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        );
        let result = service.add_one_shot_job(task).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn remove_job() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();
        assert_eq!(service.job_count().await, 1);

        service.remove_job(&result.task_id).await.unwrap();
        assert_eq!(service.job_count().await, 0);
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn remove_job_unregisters_runtime_scheduler_job() {
        let service = SchedulerService::new();
        service.start().await.unwrap();

        let task = make_cron_task("* * * * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();
        assert!(
            service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "enabled jobs must retain the runtime scheduler UUID"
        );

        service.remove_job(&task_id).await.unwrap();
        assert!(
            !service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "removing a task must also forget its runtime scheduler UUID"
        );
        assert!(service.get_job(&task_id).await.is_none());

        service.stop().await.unwrap();
    }

    // ── Pause / resume (set_job_enabled) tests ───────────────────

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn pause_unregisters_runtime_job_and_clears_next_execution() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();
        assert!(
            service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "precondition: enabled job is registered with the runtime scheduler"
        );

        let paused = service.set_job_enabled(&task_id, false).await.unwrap();
        assert!(!paused.enabled);
        assert!(
            paused.next_execution.is_none(),
            "a paused job must not advertise a run that will never happen"
        );
        assert!(
            !service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "pausing must unregister the runtime scheduler job"
        );
        assert!(
            service.get_job(&task_id).await.is_some(),
            "pausing must keep the job listed, unlike removal"
        );

        let persisted = store.load_task(&task_id).await.unwrap().unwrap();
        assert!(!persisted.enabled);
        assert!(persisted.next_execution.is_none());

        service.stop().await.unwrap();
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn resume_registers_runtime_job_and_recomputes_next_execution() {
        let service = SchedulerService::new();
        service.start().await.unwrap();

        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();
        service.set_job_enabled(&task_id, false).await.unwrap();

        let resumed = service.set_job_enabled(&task_id, true).await.unwrap();
        assert!(resumed.enabled);
        let next = resumed
            .next_execution
            .expect("a resumed cron job needs a next run");
        assert!(
            next > Utc::now(),
            "resume must recompute from the current clock, not reuse a stale value"
        );
        assert!(
            service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "resuming must re-register the runtime scheduler job"
        );

        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn pause_keeps_last_execution_and_history() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();
        service.dispatch_with_guard(&task_id).await.unwrap();

        let paused = service.set_job_enabled(&task_id, false).await.unwrap();
        assert!(
            paused.last_execution.is_some(),
            "pause must not rewind the last-run timestamp"
        );
        assert_eq!(
            service.get_execution_history(Some(&task_id)).await.len(),
            1,
            "pause must preserve execution history (unlike removal, which cascades it away)"
        );
    }

    #[tokio::test]
    async fn set_job_enabled_toggling_to_same_state_is_noop() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();

        service.set_job_enabled(&task_id, false).await.unwrap();
        let first = service.get_job(&task_id).await.unwrap();
        let again = service.set_job_enabled(&task_id, false).await.unwrap();

        assert_eq!(
            again.updated_at, first.updated_at,
            "a no-op toggle must not churn the record's timestamps"
        );
    }

    #[tokio::test]
    async fn set_job_enabled_unknown_job_is_reported() {
        let service = SchedulerService::new();
        let error = service
            .set_job_enabled(&Uuid::new_v4(), false)
            .await
            .expect_err("toggling an unknown job must fail");
        assert!(
            matches!(error, EngineError::TaskError(_)),
            "unexpected error variant: {error:?}"
        );
    }

    #[tokio::test]
    async fn resume_expired_one_shot_stays_enabled_without_next_run() {
        let service = SchedulerService::new();
        let task = make_one_shot_task(Utc::now() - chrono::Duration::hours(1));
        let task_id = task.id;
        service.add_one_shot_job(task).await.unwrap();

        service.set_job_enabled(&task_id, false).await.unwrap();
        let resumed = service.set_job_enabled(&task_id, true).await.unwrap();

        assert!(
            resumed.enabled,
            "resuming an expired one-shot still flips the enabled flag"
        );
        assert!(
            resumed.next_execution.is_none(),
            "an expired one-shot cannot advertise a future run"
        );
    }

    #[tokio::test]
    async fn manual_trigger_of_paused_job_does_not_advertise_next_run() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();
        service.set_job_enabled(&task_id, false).await.unwrap();

        // An explicit operator trigger is still allowed on a paused job, but
        // the post-dispatch metadata refresh must not resurrect its next run.
        service.dispatch_with_guard(&task_id).await.unwrap();

        let job = service.get_job(&task_id).await.unwrap();
        assert!(job.last_execution.is_some());
        assert!(
            job.next_execution.is_none(),
            "a paused job must stay without an advertised next run"
        );
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn paused_job_survives_restart_and_can_be_resumed() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let writer = SchedulerService::with_store(store.clone());
        writer.start().await.unwrap();

        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        writer.add_cron_job(task).await.unwrap();
        writer.set_job_enabled(&task_id, false).await.unwrap();
        writer.stop().await.unwrap();

        // A fresh service sharing the same store represents a gateway restart.
        let recovered = SchedulerService::with_store(store.clone());
        recovered.start().await.unwrap();

        let job = recovered
            .get_job(&task_id)
            .await
            .expect("a paused job must stay listed after a restart, or it could never be resumed");
        assert!(!job.enabled);
        assert!(job.next_execution.is_none());
        assert!(
            !recovered
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "restart must not register a paused job with the runtime scheduler"
        );

        let resumed = recovered.set_job_enabled(&task_id, true).await.unwrap();
        assert!(resumed.enabled);
        assert!(resumed.next_execution.is_some());
        assert!(
            recovered
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "resuming after a restart must register the runtime job"
        );
        assert!(store.load_task(&task_id).await.unwrap().unwrap().enabled);

        recovered.stop().await.unwrap();
    }

    #[cfg(feature = "scheduler")]
    #[test]
    fn one_shot_duration_preserves_subsecond_precision() {
        let now = Utc::now();
        let execute_after = now + chrono::Duration::milliseconds(250);
        let delay = SchedulerService::one_shot_duration(execute_after, now)
            .unwrap()
            .expect("future one-shot should have a runtime delay");

        assert_eq!(delay, std::time::Duration::from_millis(250));
    }

    #[cfg(feature = "scheduler")]
    #[test]
    fn one_shot_duration_skips_expired_timestamp() {
        let now = Utc::now();
        let execute_after = now - chrono::Duration::milliseconds(1);
        assert_eq!(
            SchedulerService::one_shot_duration(execute_after, now).unwrap(),
            None
        );
    }

    #[tokio::test]
    async fn remove_nonexistent_job() {
        let service = SchedulerService::new();
        let result = service.remove_job(&Uuid::new_v4()).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn list_jobs() {
        let service = SchedulerService::new();
        let task1 = make_cron_task("0 22 * * *");
        let task2 = make_cron_task("0 23 * * *");

        service.add_cron_job(task1).await.unwrap();
        service.add_cron_job(task2).await.unwrap();

        let jobs = service.list_jobs().await;
        assert_eq!(jobs.len(), 2);
    }

    #[tokio::test]
    async fn list_jobs_is_stable_after_hash_map_recovery() {
        let service = SchedulerService::new();
        let base = Utc::now();
        let mut older = make_cron_task("0 22 * * *");
        older.created_at = base;
        let older_id = older.id;
        let mut newer = make_cron_task("0 23 * * *");
        newer.created_at = base + chrono::Duration::seconds(1);
        let newer_id = newer.id;

        // Insert newest first so insertion order cannot accidentally make the
        // test pass without exercising the explicit ordering contract.
        service.add_cron_job(newer).await.unwrap();
        service.add_cron_job(older).await.unwrap();

        let jobs = service.list_jobs().await;
        assert_eq!(
            jobs.iter().map(|job| job.id).collect::<Vec<_>>(),
            vec![older_id, newer_id]
        );
    }

    #[tokio::test]
    async fn get_job() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        let retrieved = service.get_job(&result.task_id).await;
        assert!(retrieved.is_some());
        assert_eq!(retrieved.unwrap().description, "Test task");
    }

    #[tokio::test]
    async fn get_job_not_found() {
        let service = SchedulerService::new();
        let retrieved = service.get_job(&Uuid::new_v4()).await;
        assert!(retrieved.is_none());
    }

    #[tokio::test]
    async fn set_night_window_valid() {
        let service = SchedulerService::new();
        let config = uc_types::NightWindowConfig::default_utc();
        service.set_night_window(&config).await.unwrap();
    }

    #[tokio::test]
    async fn set_night_window_invalid_timezone() {
        let service = SchedulerService::new();
        let config = uc_types::NightWindowConfig::new(
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "Invalid/Tz".to_string(),
        );
        let result = service.set_night_window(&config).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn clear_night_window() {
        let service = SchedulerService::new();
        let config = uc_types::NightWindowConfig::default_utc();
        service.set_night_window(&config).await.unwrap();
        service.clear_night_window().await;

        // After clearing, check_night_window should always succeed
        assert!(service.check_night_window().await.is_ok());
    }

    #[tokio::test]
    async fn check_night_window_no_config() {
        let service = SchedulerService::new();
        // No night window configured — should always allow
        assert!(service.check_night_window().await.is_ok());
    }

    #[tokio::test]
    async fn get_night_window_config_returns_configured_window() {
        let service = SchedulerService::new();
        // No window set initially
        assert!(service.get_night_window_config().await.is_none());

        let config = uc_types::NightWindowConfig::new(
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "Asia/Shanghai".to_string(),
        );
        service.set_night_window(&config).await.unwrap();

        let retrieved = service.get_night_window_config().await;
        assert!(retrieved.is_some());
        let nw = retrieved.unwrap();
        assert_eq!(nw.start, NaiveTime::from_hms_opt(22, 0, 0).unwrap());
        assert_eq!(nw.end, NaiveTime::from_hms_opt(6, 0, 0).unwrap());
        assert_eq!(nw.timezone, "Asia/Shanghai");
    }

    #[tokio::test]
    async fn execution_history() {
        let service = SchedulerService::new();
        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        // Dispatch (will succeed since no night window is configured)
        service.dispatch_with_guard(&result.task_id).await.unwrap();

        let history = service.get_execution_history(None).await;
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].status, ExecutionStatus::Completed);
        assert_eq!(history[0].scheduled_task_id, result.task_id);
    }

    #[tokio::test]
    async fn execution_history_for_specific_task() {
        let service = SchedulerService::new();
        let task1 = make_cron_task("0 22 * * *");
        let task2 = make_cron_task("0 23 * * *");
        let result1 = service.add_cron_job(task1).await.unwrap();
        let result2 = service.add_cron_job(task2).await.unwrap();

        service.dispatch_with_guard(&result1.task_id).await.unwrap();
        service.dispatch_with_guard(&result2.task_id).await.unwrap();

        let history1 = service.get_execution_history(Some(&result1.task_id)).await;
        assert_eq!(history1.len(), 1);
        assert_eq!(history1[0].scheduled_task_id, result1.task_id);
    }

    #[tokio::test]
    async fn dispatch_nonexistent_task() {
        let service = SchedulerService::new();
        let result = service.dispatch_with_guard(&Uuid::new_v4()).await;
        assert!(result.is_err());
    }

    #[test]
    fn logging_dispatcher() {
        let dispatcher = LoggingDispatcher;
        let task = make_cron_task("0 22 * * *");
        let result = dispatcher.dispatch(&task);
        assert!(result.is_ok());
    }

    // ── Cron 6-field conversion tests ────────────────────────────

    #[cfg(feature = "scheduler")]
    #[test]
    fn cron_5field_to_6field() {
        assert_eq!(
            SchedulerService::cron_to_6field("0 22 * * *"),
            "0 0 22 * * *"
        );
        assert_eq!(
            SchedulerService::cron_to_6field("30 4 * * 1"),
            "0 30 4 * * 1"
        );
    }

    #[cfg(feature = "scheduler")]
    #[test]
    fn cron_6field_unchanged() {
        assert_eq!(
            SchedulerService::cron_to_6field("0 0 22 * * *"),
            "0 0 22 * * *"
        );
        assert_eq!(
            SchedulerService::cron_to_6field("0 30 4 * * 1"),
            "0 30 4 * * 1"
        );
    }

    // ── Store integration tests ──────────────────────────────────

    #[tokio::test]
    async fn add_cron_job_persists_to_store() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        // Verify persisted in store
        let loaded = store.load_task(&result.task_id).await.unwrap();
        assert!(loaded.is_some());
        assert_eq!(loaded.unwrap().description, "Test task");
    }

    #[tokio::test]
    async fn add_one_shot_job_persists_to_store() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        let later = Utc::now() + chrono::Duration::hours(8);
        let task = make_one_shot_task(later);
        let result = service.add_one_shot_job(task).await.unwrap();

        // Verify persisted in store
        let loaded = store.load_task(&result.task_id).await.unwrap();
        assert!(loaded.is_some());
        assert!(loaded.unwrap().is_one_shot());
    }

    #[tokio::test]
    async fn verify_command_persists_through_store() {
        // Regression: PostgresScheduleStore previously dropped verify_command
        // on save/load/update (the column didn't exist). The in-memory store
        // is the contract reference — verify the field round-trips so the
        // trait contract is locked, and the Postgres impl (which mirrors this
        // shape) is held to the same standard.
        let store = super::super::store::InMemoryScheduleStore::new();
        let mut task = make_cron_task("0 22 * * *");
        task.verify_command = Some("cargo check".to_string());

        store.save_task(&task).await.unwrap();
        let loaded = store.load_task(&task.id).await.unwrap().unwrap();
        assert_eq!(
            loaded.verify_command.as_deref(),
            Some("cargo check"),
            "verify_command must survive save→load"
        );

        // Update with a different command
        let mut updated = loaded.clone();
        updated.verify_command = Some("cargo test".to_string());
        store.update_task(&updated).await.unwrap();
        let reloaded = store.load_task(&task.id).await.unwrap().unwrap();
        assert_eq!(
            reloaded.verify_command.as_deref(),
            Some("cargo test"),
            "verify_command must survive update→load"
        );

        // Clearing to None must also persist
        let mut cleared = reloaded.clone();
        cleared.verify_command = None;
        store.update_task(&cleared).await.unwrap();
        let final_load = store.load_task(&task.id).await.unwrap().unwrap();
        assert!(
            final_load.verify_command.is_none(),
            "verify_command=None must survive update→load"
        );
    }

    #[tokio::test]
    async fn remove_job_deletes_from_store() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        service.remove_job(&result.task_id).await.unwrap();

        // Verify removed from store
        let loaded = store.load_task(&result.task_id).await.unwrap();
        assert!(loaded.is_none());
    }

    #[tokio::test]
    async fn dispatch_persists_execution_history_to_store() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        service.dispatch_with_guard(&result.task_id).await.unwrap();

        // Verify execution history persisted in store
        let executions = store.list_executions(&result.task_id, 10).await.unwrap();
        assert_eq!(executions.len(), 1);
        assert_eq!(executions[0].status, ExecutionStatus::Completed);
    }

    #[tokio::test]
    async fn dispatch_updates_execution_metadata() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();
        let before = service.get_job(&result.task_id).await.unwrap();
        assert!(before.last_execution.is_none());
        assert!(before.next_execution.is_some());

        service.dispatch_with_guard(&result.task_id).await.unwrap();

        let after = service.get_job(&result.task_id).await.unwrap();
        assert!(after.last_execution.is_some());
        assert!(after.next_execution.is_some());
        assert!(after.updated_at >= after.last_execution.unwrap());

        let persisted = store.load_task(&result.task_id).await.unwrap().unwrap();
        assert_eq!(persisted.last_execution, after.last_execution);
        assert_eq!(persisted.next_execution, after.next_execution);
    }

    #[tokio::test]
    async fn failed_dispatch_still_updates_execution_metadata() {
        struct FailingDispatcher;
        impl ScheduleDispatcher for FailingDispatcher {
            fn dispatch(&self, _task: &ScheduledTask) -> Result<(), EngineError> {
                Err(EngineError::ConnectionError(
                    "worker unavailable".to_string(),
                ))
            }
        }

        let service = SchedulerService::with_dispatcher(Arc::new(FailingDispatcher));
        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();

        assert!(service.dispatch_with_guard(&task_id).await.is_err());

        let updated = service.get_job(&task_id).await.unwrap();
        assert!(
            updated.last_execution.is_some(),
            "failed dispatch attempts still represent a scheduler run"
        );
        assert!(updated.next_execution.is_some());
        assert_eq!(
            service
                .get_execution_history(Some(&task_id))
                .await
                .first()
                .map(|history| &history.status),
            Some(&ExecutionStatus::Skipped)
        );
    }

    #[tokio::test]
    async fn start_reloads_persisted_tasks() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());

        // Pre-populate store with tasks
        let task1 = make_cron_task("0 22 * * *");
        let task2 = make_cron_task("0 23 * * *");
        store.save_task(&task1).await.unwrap();
        store.save_task(&task2).await.unwrap();

        // Create service and start — should recover tasks
        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        // Jobs should be loaded
        assert_eq!(service.job_count().await, 2);
    }

    #[tokio::test]
    async fn start_recovers_persisted_execution_history() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let writer = SchedulerService::with_store(store.clone());
        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        let mut disabled_task = make_cron_task("0 23 * * *");
        disabled_task.enabled = false;
        let disabled_task_id = disabled_task.id;

        writer.add_cron_job(task).await.unwrap();
        writer.add_cron_job(disabled_task).await.unwrap();
        writer.dispatch_with_guard(&task_id).await.unwrap();
        let mut disabled_history = ExecutionHistory::started(disabled_task_id);
        disabled_history.started_at = Utc::now() - chrono::Duration::hours(1);
        store.save_execution(&disabled_history).await.unwrap();

        // A fresh service instance represents a gateway restart while sharing
        // the same durable store.
        let recovered = SchedulerService::with_store(store);
        recovered.start().await.unwrap();

        let history = recovered.get_execution_history(None).await;
        assert_eq!(
            recovered.job_count().await,
            2,
            "recovery lists both the enabled and the paused job"
        );
        assert_eq!(history.len(), 2);
        assert_eq!(history[0].scheduled_task_id, disabled_task_id);
        assert_eq!(history[1].scheduled_task_id, task_id);
        assert_eq!(history[1].status, ExecutionStatus::Completed);
        recovered.stop().await.unwrap();
    }

    #[tokio::test]
    async fn start_refreshes_stale_next_execution() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let mut task = make_cron_task("* * * * * *");
        task.next_execution = Some(Utc::now() - chrono::Duration::hours(1));
        store.save_task(&task).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        let recovered = service.get_job(&task.id).await.unwrap();
        assert!(
            recovered
                .next_execution
                .is_some_and(|next_execution| next_execution > Utc::now()),
            "recovery should replace an expired next-run timestamp"
        );
        let persisted = store.load_task(&task.id).await.unwrap().unwrap();
        assert_eq!(persisted.next_execution, recovered.next_execution);
        assert!(persisted.updated_at >= task.updated_at);
        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn start_clears_a_stale_one_shot_run_but_keeps_an_owed_retry() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());

        // A stale (already-passed) next-run on an expired one-shot advertises
        // nothing real, so recovery clears it.
        let mut stale = make_one_shot_task(Utc::now() - chrono::Duration::hours(2));
        stale.next_execution = Some(Utc::now() - chrono::Duration::minutes(30));
        let stale_id = stale.id;
        store.save_task(&stale).await.unwrap();

        // A *future* next-run on an expired one-shot is meaningful state: the
        // only code that produces that shape is `rearm_deferred_one_shot`, so
        // recovery must honour it as the retry it owes rather than clear it.
        let retry_at = Utc::now() + chrono::Duration::hours(1);
        let mut owed = make_one_shot_task(Utc::now() - chrono::Duration::hours(1));
        owed.next_execution = Some(retry_at);
        let owed_id = owed.id;
        store.save_task(&owed).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        let recovered_stale = service.get_job(&stale_id).await.unwrap();
        assert!(
            recovered_stale.next_execution.is_none(),
            "an expired one-shot with nothing owed must not advertise a future run"
        );
        let persisted_stale = store.load_task(&stale_id).await.unwrap().unwrap();
        assert!(
            persisted_stale.next_execution.is_none(),
            "the cleared value must reach the durable record too"
        );

        let recovered_owed = service.get_job(&owed_id).await.unwrap();
        assert_eq!(
            recovered_owed.next_execution,
            Some(retry_at),
            "a re-armed retry survives recovery"
        );
        service.stop().await.unwrap();
    }

    // ── Missed one-shot recovery tests ───────────────────────────

    #[tokio::test]
    async fn recovery_records_missed_one_shot_as_skipped() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let execute_after = Utc::now() - chrono::Duration::hours(1);
        let task = make_one_shot_task(execute_after);
        let task_id = task.id;
        store.save_task(&task).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        let history = service
            .get_execution_history_from_store(&task_id, 10)
            .await
            .unwrap();
        assert_eq!(
            history.len(),
            1,
            "an expired one-shot must leave exactly one trace of the promised run"
        );
        let missed = &history[0];
        assert!(
            matches!(missed.status, ExecutionStatus::Skipped),
            "unexpected status: {:?}",
            missed.status
        );
        assert_eq!(
            missed.started_at, execute_after,
            "the record is stamped with the instant that was missed, not the time it was noticed"
        );
        assert!(
            missed.completed_at.is_none(),
            "no completed_at — pairing the missed instant with 'now' renders as a fake run duration"
        );
        assert!(missed
            .result_summary
            .as_deref()
            .unwrap_or_default()
            .contains("Missed"));

        let recovered = service.get_job(&task_id).await.unwrap();
        assert!(
            recovered.next_execution.is_none(),
            "the expired one-shot stays listed but pending nothing"
        );
        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn recovery_does_not_duplicate_missed_one_shot_history() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let task = make_one_shot_task(Utc::now() - chrono::Duration::hours(1));
        let task_id = task.id;
        store.save_task(&task).await.unwrap();

        let first = SchedulerService::with_store(store.clone());
        first.start().await.unwrap();
        first.stop().await.unwrap();

        let second = SchedulerService::with_store(store.clone());
        second.start().await.unwrap();

        assert_eq!(
            store.list_executions(&task_id, 10).await.unwrap().len(),
            1,
            "repeated restarts must not repeat the same miss"
        );
        second.stop().await.unwrap();
    }

    #[tokio::test]
    async fn recovery_does_not_record_an_already_dispatched_one_shot() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let execute_after = Utc::now() - chrono::Duration::hours(1);
        let mut task = make_one_shot_task(execute_after);
        // It fired on time in the previous process, then the gateway restarted.
        task.last_execution = Some(execute_after);
        let task_id = task.id;
        store.save_task(&task).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        assert!(
            service
                .get_execution_history(Some(&task_id))
                .await
                .is_empty(),
            "a one-shot that already ran has nothing to report as missed"
        );
        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn recovery_does_not_record_a_miss_for_a_paused_one_shot() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let mut task = make_one_shot_task(Utc::now() - chrono::Duration::hours(1));
        let task_id = task.id;
        task.enabled = false;
        store.save_task(&task).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        assert!(
            service
                .get_execution_history(Some(&task_id))
                .await
                .is_empty(),
            "a paused job was stopped deliberately; a skipped-by-request run is not a missed run"
        );
        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn recovery_records_no_miss_for_cron_jobs_and_keeps_history_ordered() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let cron = make_cron_task("0 22 * * *");
        let cron_id = cron.id;
        store.save_task(&cron).await.unwrap();

        let older = make_one_shot_task(Utc::now() - chrono::Duration::hours(2));
        let older_id = older.id;
        store.save_task(&older).await.unwrap();
        let newer = make_one_shot_task(Utc::now() - chrono::Duration::hours(1));
        let newer_id = newer.id;
        store.save_task(&newer).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        assert!(
            service
                .get_execution_history(Some(&cron_id))
                .await
                .is_empty(),
            "a cron expression is a standing schedule, not a single promised run"
        );

        let history = service.get_execution_history(None).await;
        assert_eq!(history.len(), 2, "both misses are recorded");
        assert_eq!(history[0].scheduled_task_id, older_id);
        assert_eq!(history[1].scheduled_task_id, newer_id);
        assert!(
            history[0].started_at <= history[1].started_at,
            "recovered misses must land in chronological order, not append order"
        );
        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn recovery_records_the_miss_behind_a_deferred_one_shot() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let execute_after = Utc::now() - chrono::Duration::hours(3);
        let task = make_one_shot_task(execute_after);
        let task_id = task.id;
        store.save_task(&task).await.unwrap();

        // It came due and the night-window guard refused it. A one-shot is never
        // retried after that, so the deferral must not read as "already handled".
        let mut deferred = ExecutionHistory::deferred(task_id, "Outside night window".to_string());
        deferred.started_at = execute_after;
        store.save_execution(&deferred).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        let history = service
            .get_execution_history_from_store(&task_id, 10)
            .await
            .unwrap();
        assert_eq!(
            history.len(),
            2,
            "a deferral that was never retried still ends in a missed run"
        );
        assert!(history.iter().any(|entry| {
            matches!(entry.status, ExecutionStatus::Skipped) && entry.started_at == execute_after
        }));
        service.stop().await.unwrap();

        let again = SchedulerService::with_store(store.clone());
        again.start().await.unwrap();
        assert_eq!(
            store.list_executions(&task_id, 10).await.unwrap().len(),
            2,
            "the miss is recorded once, not once per restart"
        );
        again.stop().await.unwrap();
    }

    // ── Deferred one-shot re-arm tests ───────────────────────────

    /// A window whose start equals its end is never open (`start <= t < end`
    /// cannot hold), which makes the deferral branch deterministic rather than
    /// dependent on the wall clock.
    fn always_closed_window() -> uc_types::NightWindowConfig {
        let at = NaiveTime::from_hms_opt(2, 0, 0).expect("valid time");
        uc_types::NightWindowConfig::new(at, at, "UTC".to_string())
    }

    #[tokio::test]
    async fn deferred_one_shot_rearms_for_the_next_window_opening() {
        let service = SchedulerService::new();
        service
            .set_night_window(&always_closed_window())
            .await
            .unwrap();

        let task = make_one_shot_task(Utc::now());
        let task_id = task.id;
        service.add_one_shot_job(task).await.unwrap();

        let result = service.dispatch_with_guard(&task_id).await;
        assert!(
            result.is_err(),
            "a closed window must refuse the dispatch, not run it"
        );

        let deferred = service.get_job(&task_id).await.unwrap();
        let retry_at = deferred
            .next_execution
            .expect("a deferred one-shot must carry the retry it was promised");
        assert!(
            retry_at > Utc::now(),
            "the retry has to lie in the future, or nothing can honour it"
        );

        let history = service.get_execution_history(Some(&task_id)).await;
        assert_eq!(history.len(), 1, "one deferral, one record");
        assert!(
            matches!(history[0].status, ExecutionStatus::Deferred),
            "unexpected status: {:?}",
            history[0].status
        );
    }

    #[tokio::test]
    async fn recovery_preserves_a_pending_rearm_and_records_no_miss() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());
        service
            .set_night_window(&always_closed_window())
            .await
            .unwrap();

        let task = make_one_shot_task(Utc::now());
        let task_id = task.id;
        service.add_one_shot_job(task).await.unwrap();
        assert!(service.dispatch_with_guard(&task_id).await.is_err());
        let retry_at = service
            .get_job(&task_id)
            .await
            .unwrap()
            .next_execution
            .expect("re-armed");
        drop(service); // the gateway exits before the retry ever came due

        let recovered = SchedulerService::with_store(store.clone());
        recovered.start().await.unwrap();

        let job = recovered.get_job(&task_id).await.unwrap();
        assert_eq!(
            job.next_execution,
            Some(retry_at),
            "recovery must not clobber an attempt the scheduler still owes"
        );
        assert!(
            recovered
                .get_execution_history(Some(&task_id))
                .await
                .iter()
                .all(|entry| !matches!(entry.status, ExecutionStatus::Skipped)),
            "a pending retry is not a missed run"
        );
        recovered.stop().await.unwrap();
    }

    #[test]
    fn one_shot_attempt_at_prefers_a_rearmed_retry() {
        let now = Utc::now();
        let retry_at = now + chrono::Duration::hours(3);
        let mut task = make_one_shot_task(now - chrono::Duration::hours(1));
        task.next_execution = Some(retry_at);
        assert_eq!(
            SchedulerService::one_shot_attempt_at(&task, now),
            Some(retry_at),
            "the owed retry outranks an already-passed original instant"
        );

        // Nothing re-armed yet: the original future instant is the attempt.
        let mut pending = make_one_shot_task(now + chrono::Duration::hours(1));
        pending.next_execution = None;
        assert_eq!(
            SchedulerService::one_shot_attempt_at(&pending, now),
            pending.execute_after
        );

        // Expired and never re-armed: nothing is owed.
        let exhausted = make_one_shot_task(now - chrono::Duration::hours(1));
        assert_eq!(SchedulerService::one_shot_attempt_at(&exhausted, now), None);
    }

    /// A dispatcher whose transport is down (NATS unavailable / no worker).
    struct DownDispatcher;

    impl ScheduleDispatcher for DownDispatcher {
        fn dispatch(&self, _task: &ScheduledTask) -> Result<(), EngineError> {
            Err(EngineError::TaskError("nats unavailable".to_string()))
        }
    }

    #[tokio::test]
    async fn failed_dispatch_rearms_one_shot_with_backoff() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service =
            SchedulerService::with_store_and_dispatcher(store.clone(), Arc::new(DownDispatcher));

        let task = make_one_shot_task(Utc::now() - chrono::Duration::minutes(5));
        let task_id = task.id;
        service.add_one_shot_job(task).await.unwrap();

        let error = service
            .dispatch_with_guard(&task_id)
            .await
            .expect_err("a dead transport must surface as an error");
        assert!(error.to_string().contains("nats unavailable"));

        let job = service.get_job(&task_id).await.unwrap();
        assert_eq!(
            job.dispatch_attempts, 1,
            "a scheduled failure spends exactly one retry"
        );
        let retry_at = job
            .next_execution
            .expect("the one-shot whose only run failed must be re-armed");
        assert!(
            retry_at > Utc::now(),
            "the retry has to be scheduled for the future"
        );

        let history = service.get_execution_history(Some(&task_id)).await;
        assert_eq!(history.len(), 1);
        let summary = history[0].result_summary.as_deref().unwrap_or_default();
        assert!(
            summary.contains("retry 1 of"),
            "the record must name the coming retry, got: {summary}"
        );
    }

    #[tokio::test]
    async fn dispatch_retries_are_bounded_and_then_exhaust() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service =
            SchedulerService::with_store_and_dispatcher(store.clone(), Arc::new(DownDispatcher));

        let mut task = make_one_shot_task(Utc::now() - chrono::Duration::minutes(5));
        task.dispatch_attempts = MAX_DISPATCH_RETRIES;
        let task_id = task.id;
        service.add_one_shot_job(task).await.unwrap();

        assert!(service.dispatch_with_guard(&task_id).await.is_err());

        let job = service.get_job(&task_id).await.unwrap();
        assert_eq!(job.dispatch_attempts, MAX_DISPATCH_RETRIES + 1);
        assert!(
            job.next_execution.is_none(),
            "an exhausted budget must stop re-arming rather than retry forever"
        );
        let history = service.get_execution_history(Some(&task_id)).await;
        assert!(history[0]
            .result_summary
            .as_deref()
            .unwrap_or_default()
            .contains("exhausted"));
    }

    #[tokio::test]
    async fn manual_trigger_neither_reschedules_nor_spends_the_budget() {
        let service = SchedulerService::with_dispatcher(Arc::new(DownDispatcher));
        let planned = Utc::now() + chrono::Duration::hours(2);
        let task = make_one_shot_task(planned);
        let task_id = task.id;
        service.add_one_shot_job(task).await.unwrap();

        service
            .dispatch_with_guard_from(&task_id, DispatchSource::Manual)
            .await
            .expect_err("transport is down");

        let job = service.get_job(&task_id).await.unwrap();
        assert_eq!(
            job.dispatch_attempts, 0,
            "an operator poke must not eat the scheduled retry budget"
        );
        assert_eq!(
            job.next_execution,
            Some(planned),
            "nor move the standing plan it was not asked to move"
        );
        let history = service.get_execution_history(Some(&task_id)).await;
        assert!(
            !history[0]
                .result_summary
                .as_deref()
                .unwrap_or_default()
                .contains("retry"),
            "a manual failure announces no automatic retry"
        );
    }

    #[tokio::test]
    async fn successful_dispatch_resets_the_retry_budget() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        let mut task = make_one_shot_task(Utc::now() - chrono::Duration::minutes(5));
        task.dispatch_attempts = 2;
        let task_id = task.id;
        service.add_one_shot_job(task).await.unwrap();

        service.dispatch_with_guard(&task_id).await.unwrap();

        assert_eq!(
            service.get_job(&task_id).await.unwrap().dispatch_attempts,
            0,
            "a fresh schedule gets a fresh budget"
        );
        assert_eq!(
            store
                .load_task(&task_id)
                .await
                .unwrap()
                .unwrap()
                .dispatch_attempts,
            0,
            "the reset must reach the durable record, or a restart re-spends it"
        );
    }

    #[tokio::test]
    async fn cron_failure_relies_on_its_next_tick_not_the_retry_budget() {
        let service = SchedulerService::with_dispatcher(Arc::new(DownDispatcher));
        let task = make_cron_task("0 22 * * *");
        let task_id = task.id;
        service.add_cron_job(task).await.unwrap();

        assert!(service.dispatch_with_guard(&task_id).await.is_err());

        let job = service.get_job(&task_id).await.unwrap();
        assert_eq!(
            job.dispatch_attempts, 0,
            "a cron job's next tick already is its retry"
        );
        assert!(
            job.next_execution.is_some(),
            "and that next tick stays advertised"
        );
    }

    #[test]
    fn dispatch_retry_backoff_doubles_and_caps() {
        assert_eq!(SchedulerService::retry_delay_for(1).num_seconds(), 60);
        assert_eq!(SchedulerService::retry_delay_for(2).num_seconds(), 120);
        assert_eq!(SchedulerService::retry_delay_for(3).num_seconds(), 240);
        assert_eq!(
            SchedulerService::retry_delay_for(64).num_seconds(),
            900,
            "the doubling must hit the ceiling, not overflow"
        );
    }

    #[tokio::test]
    async fn retry_budget_and_owed_retry_survive_a_restart() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        {
            let service = SchedulerService::with_store_and_dispatcher(
                store.clone(),
                Arc::new(DownDispatcher),
            );
            let task = make_one_shot_task(Utc::now() - chrono::Duration::minutes(5));
            let task_id = task.id;
            service.add_one_shot_job(task).await.unwrap();
            assert!(service.dispatch_with_guard(&task_id).await.is_err());
        } // gateway dies between the failure and its retry

        let recovered = SchedulerService::with_store(store.clone());
        recovered.start().await.unwrap();

        let job = recovered
            .get_job(&store.list_tasks(false).await.unwrap()[0].id)
            .await
            .expect("the job is still listed");
        assert_eq!(
            job.dispatch_attempts, 1,
            "the budget is durable, so restarts cannot launder it"
        );
        assert!(
            job.next_execution.is_some(),
            "the owed retry survives recovery"
        );
        recovered.stop().await.unwrap();
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn recovery_registers_a_pending_retry_with_the_runtime_scheduler() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let retry_at = Utc::now() + chrono::Duration::hours(1);
        let mut task = make_one_shot_task(Utc::now() - chrono::Duration::minutes(5));
        // Exactly the shape a re-arm leaves behind: the original instant has
        // passed, and the owed retry sits in the future.
        task.next_execution = Some(retry_at);
        let task_id = task.id;
        store.save_task(&task).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        assert!(
            service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "an owed retry must actually be re-registered after a restart, not merely recorded"
        );
        assert_eq!(
            service.get_job(&task_id).await.unwrap().next_execution,
            Some(retry_at)
        );
        service.stop().await.unwrap();
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn start_reloads_paused_tasks_without_registering_them() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());

        // Pre-populate store with tasks
        let task1 = make_cron_task("0 22 * * *");
        let mut task2 = make_cron_task("0 23 * * *");
        task2.enabled = false;
        store.save_task(&task1).await.unwrap();
        store.save_task(&task2).await.unwrap();

        // Create service and start — every persisted task becomes visible again
        // (so a paused job can be resumed later), but only the enabled one is
        // handed to the runtime scheduler.
        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        assert_eq!(service.job_count().await, 2);
        let paused = service.get_job(&task2.id).await.unwrap();
        assert!(!paused.enabled);
        assert!(
            paused.next_execution.is_none(),
            "recovery must not advertise a run for a paused task"
        );
        assert!(
            service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task1.id),
            "enabled tasks are registered on recovery"
        );
        assert!(
            !service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task2.id),
            "paused tasks must stay out of the runtime scheduler"
        );
        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn get_execution_history_from_store() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        let task = make_cron_task("0 22 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        service.dispatch_with_guard(&result.task_id).await.unwrap();

        // Get from store
        let history = service
            .get_execution_history_from_store(&result.task_id, 10)
            .await
            .unwrap();
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].status, ExecutionStatus::Completed);
    }

    // ── Night window + persistence integration test ──────────────

    #[tokio::test]
    async fn night_window_guard_with_store() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let service = SchedulerService::with_store(store.clone());

        // Set a night window that is definitely in the past (06:00-06:01)
        // so we're outside it at most times of day
        let config = uc_types::NightWindowConfig::new(
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 1, 0).unwrap(),
            "UTC".to_string(),
        );
        service.set_night_window(&config).await.unwrap();

        let task = make_cron_task("0 6 * * *");
        let result = service.add_cron_job(task).await.unwrap();

        // Attempt dispatch — will likely be deferred depending on current time
        let dispatch_result = service.dispatch_with_guard(&result.task_id).await;

        // Either it's within the 1-minute window (Completed) or outside (Deferred)
        // Either way, execution history should be recorded in the store
        let executions = store.list_executions(&result.task_id, 10).await.unwrap();
        assert_eq!(executions.len(), 1);

        if dispatch_result.is_err() {
            assert_eq!(executions[0].status, ExecutionStatus::Deferred);
        } else {
            assert_eq!(executions[0].status, ExecutionStatus::Completed);
        }
    }

    // ── Cron-callback dispatch wiring tests ───────────────────────

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn cron_callback_calls_dispatch_with_guard() {
        // Verify that when a cron job fires, the callback calls
        // dispatch_with_guard (which records ExecutionHistory).
        // We use a very short interval cron expression and wait for it
        // to fire. The LoggingDispatcher is used (default), so dispatch
        // succeeds and records Completed.
        use std::time::Duration;

        let service = SchedulerService::new();
        service.start().await.unwrap();

        // Create a cron job that fires every second
        let task = ScheduledTask::cron(
            "Callback test".to_string(),
            "test-project".to_string(),
            "* * * * * *".to_string(), // 6-field: every second
            NaiveTime::from_hms_opt(0, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(23, 59, 59).unwrap(),
            "UTC".to_string(),
        );

        let result = service.add_cron_job(task).await.unwrap();
        let task_id = result.task_id;

        // Wait for the cron to fire (2 seconds to be safe)
        tokio::time::sleep(Duration::from_secs(2)).await;

        // Check that dispatch_with_guard was called (execution history recorded)
        let history = service.get_execution_history(Some(&task_id)).await;
        assert!(
            !history.is_empty(),
            "Cron callback should have called dispatch_with_guard, recording execution history"
        );

        service.stop().await.unwrap();
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn one_shot_callback_calls_dispatch_with_guard() {
        // Verify that when a one-shot job fires, the callback calls
        // dispatch_with_guard (which records ExecutionHistory).
        use std::time::Duration;

        let service = SchedulerService::new();
        service.start().await.unwrap();

        // Create a one-shot job that fires in 2 seconds
        let execute_after = Utc::now() + chrono::Duration::seconds(2);
        let task = ScheduledTask::one_shot(
            "One-shot callback test".to_string(),
            "test-project".to_string(),
            execute_after,
            NaiveTime::from_hms_opt(0, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(23, 59, 59).unwrap(),
            "UTC".to_string(),
        );

        let result = service.add_one_shot_job(task).await.unwrap();
        let task_id = result.task_id;

        // Wait for the one-shot to fire (3 seconds to be safe)
        tokio::time::sleep(Duration::from_secs(3)).await;

        // Check that dispatch_with_guard was called
        let history = service.get_execution_history(Some(&task_id)).await;
        assert!(
            !history.is_empty(),
            "One-shot callback should have called dispatch_with_guard, recording execution history"
        );
        assert!(
            !service
                .scheduler_job_ids
                .read()
                .await
                .contains_key(&task_id),
            "one-shot jobs should release their runtime scheduler UUID after firing"
        );

        service.stop().await.unwrap();
    }

    // ── EngineApi scheduler trait method tests ────────────────────

    #[tokio::test]
    async fn local_engine_get_scheduler_status() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let status = engine.get_scheduler_status().await.unwrap();

        assert!(
            status.available,
            "LocalEngine scheduler should be available"
        );
        assert!(
            !status.is_running,
            "Scheduler should not be running until started"
        );
        assert!(status.jobs.is_empty(), "No jobs configured");
        assert!(
            status.night_window.is_none(),
            "No night window configured by default"
        );
    }

    #[tokio::test]
    async fn local_engine_get_scheduler_status_with_night_window() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let config = uc_types::NightWindowConfig::new(
            NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(6, 0, 0).unwrap(),
            "UTC".to_string(),
        );
        engine
            .scheduler_service()
            .set_night_window(&config)
            .await
            .unwrap();

        let status = engine.get_scheduler_status().await.unwrap();
        assert!(status.night_window.is_some(), "Night window should be set");
        let nw = status.night_window.unwrap();
        assert_eq!(nw.start, NaiveTime::from_hms_opt(22, 0, 0).unwrap());
        assert_eq!(nw.end, NaiveTime::from_hms_opt(6, 0, 0).unwrap());
    }

    #[tokio::test]
    async fn local_engine_trigger_scheduler_job_not_found() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let result = engine.trigger_scheduler_job("nonexistent-uuid").await;

        // Invalid UUID string should return an error
        assert!(result.is_err(), "Invalid UUID should return Err");
    }

    #[tokio::test]
    async fn local_engine_trigger_scheduler_job_valid_uuid_not_found() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let valid_uuid = uuid::Uuid::new_v4().to_string();
        let result = engine.trigger_scheduler_job(&valid_uuid).await.unwrap();

        assert!(!result.success, "Triggering a non-existent job should fail");
        assert!(result.error.is_some(), "Error message should be present");
    }

    #[tokio::test]
    async fn local_engine_remove_job_invalid_uuid() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let result = engine.remove_job("not-a-uuid").await;

        // Invalid UUID string should return an error (ConfigError)
        assert!(result.is_err(), "Invalid UUID should return Err, not Ok");
    }

    #[tokio::test]
    async fn local_engine_remove_job_nonexistent() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let valid_uuid = uuid::Uuid::new_v4().to_string();
        let result = engine.remove_job(&valid_uuid).await.unwrap();

        // Valid UUID but job doesn't exist — remove_job calls store.delete_task
        // which returns Err for nonexistent. The engine converts to success=false.
        assert!(
            !result.success,
            "Removing a non-existent job should return success=false"
        );
        assert!(
            result.error.is_some(),
            "Error message should be present for non-existent job"
        );
    }

    #[tokio::test]
    async fn local_engine_remove_job_success() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();

        // First add a job via the engine API so it exists in the scheduler.
        let add_result = engine
            .add_cron_job(uc_types::AddCronJobApiRequest {
                description: "To be removed".to_string(),
                cron_expression: "0 22 * * *".to_string(),
                project_id: "test-project".to_string(),
                night_window_start: None,
                night_window_end: None,
                timezone: "UTC".to_string(),
                enabled: true,
            })
            .await
            .unwrap();

        assert!(add_result.success, "Job should be created successfully");
        let job_id = add_result.job_id.clone();
        assert!(!job_id.is_empty(), "Job ID should not be empty");

        // Verify the job exists
        assert_eq!(
            engine.scheduler_service().job_count().await,
            1,
            "Should have 1 job after adding"
        );

        // Now remove it
        let remove_result = engine.remove_job(&job_id).await.unwrap();

        assert!(
            remove_result.success,
            "Removing an existing job should succeed"
        );
        assert!(
            remove_result.error.is_none(),
            "No error should be present on success"
        );

        // Verify the job is gone
        assert_eq!(
            engine.scheduler_service().job_count().await,
            0,
            "Should have 0 jobs after removal"
        );
    }

    #[tokio::test]
    async fn local_engine_pause_and_resume_job_via_api() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let add_result = engine
            .add_cron_job(uc_types::AddCronJobApiRequest {
                description: "Nightly build".to_string(),
                cron_expression: "0 22 * * *".to_string(),
                project_id: "test-project".to_string(),
                night_window_start: None,
                night_window_end: None,
                timezone: "UTC".to_string(),
                enabled: true,
            })
            .await
            .unwrap();
        let job_id = add_result.job_id;

        let paused = engine
            .set_scheduler_job_enabled(&job_id, false)
            .await
            .unwrap();
        assert!(paused.success, "pausing an existing job should succeed");
        assert!(!paused.enabled, "the result echoes the settled state");
        assert!(paused.error.is_none());

        // The dashboard reads the status snapshot: a paused job must remain
        // listed there (so it can be resumed) and must stop advertising a run.
        let status = engine.get_scheduler_status().await.unwrap();
        let job = status
            .jobs
            .iter()
            .find(|job| job.id.to_string() == job_id)
            .expect("a paused job must stay visible in the scheduler status");
        assert!(!job.enabled);
        assert!(job.next_execution.is_none());

        let resumed = engine
            .set_scheduler_job_enabled(&job_id, true)
            .await
            .unwrap();
        assert!(resumed.success, "resuming an existing job should succeed");
        assert!(resumed.enabled);
        assert!(engine
            .get_scheduler_status()
            .await
            .unwrap()
            .jobs
            .iter()
            .find(|job| job.id.to_string() == job_id)
            .and_then(|job| job.next_execution)
            .is_some());
    }

    #[tokio::test]
    async fn local_engine_set_scheduler_job_enabled_invalid_uuid() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let result = engine.set_scheduler_job_enabled("not-a-uuid", false).await;

        assert!(
            result.is_err(),
            "An invalid job ID should return Err, like the sibling operations"
        );
    }

    #[tokio::test]
    async fn local_engine_set_scheduler_job_enabled_nonexistent() {
        use uc_types::EngineApi;

        let engine = crate::local::LocalEngine::new_fallback();
        let valid_uuid = uuid::Uuid::new_v4().to_string();
        let result = engine
            .set_scheduler_job_enabled(&valid_uuid, false)
            .await
            .unwrap();

        assert!(
            !result.success,
            "Toggling a non-existent job should return success=false"
        );
        assert!(result.error.is_some(), "Error message should be present");
        assert!(
            !result.enabled,
            "The echoed state must not claim a change that did not happen"
        );
    }

    #[tokio::test]
    async fn default_remove_job_returns_false() {
        use uc_types::EngineApi;

        /// Minimal engine that uses all default scheduler trait impls.
        struct NoSchedulerEngine;

        #[async_trait::async_trait]
        impl EngineApi for NoSchedulerEngine {
            async fn search(
                &self,
                _query: uc_types::SearchQuery,
            ) -> Result<uc_types::SearchResult, EngineError> {
                unreachable!()
            }
            async fn index_repo(
                &self,
                _request: uc_types::IndexRequest,
            ) -> Result<uc_types::IndexResponse, EngineError> {
                unreachable!()
            }
            async fn get_index_state(
                &self,
                _repo_id: &str,
            ) -> Result<uc_types::RepoIndexState, EngineError> {
                unreachable!()
            }
            async fn remove_index(&self, _repo_id: &str) -> Result<(), EngineError> {
                unreachable!()
            }
            async fn read_memory(
                &self,
                _request: uc_types::MemoryReadRequest,
            ) -> Result<Option<uc_types::MemoryEntry>, EngineError> {
                unreachable!()
            }
            async fn write_memory(
                &self,
                _request: uc_types::MemoryWriteRequest,
            ) -> Result<uc_types::MemoryEntry, EngineError> {
                unreachable!()
            }
            async fn delete_memory(&self, _key: &uc_types::MemoryKey) -> Result<(), EngineError> {
                unreachable!()
            }
            async fn search_memory(
                &self,
                _request: uc_types::MemorySearchRequest,
            ) -> Result<uc_types::MemorySearchResponse, EngineError> {
                unreachable!()
            }
            async fn health(&self) -> Result<uc_types::HealthStatus, EngineError> {
                unreachable!()
            }
            async fn batch_write_memory(
                &self,
                _requests: Vec<uc_types::MemoryWriteRequest>,
            ) -> Result<Vec<uc_types::MemoryEntry>, EngineError> {
                unreachable!()
            }
            async fn list_repos(
                &self,
                _workspace_id: Option<&str>,
            ) -> Result<Vec<uc_types::RepoIndexState>, EngineError> {
                unreachable!()
            }
            async fn list_dir(
                &self,
                _repo_id: &str,
                _path: &str,
            ) -> Result<uc_types::agent::DirListing, EngineError> {
                unreachable!()
            }
            async fn get_file(
                &self,
                _repo_id: &str,
                _path: &str,
            ) -> Result<uc_types::agent::FileContent, EngineError> {
                unreachable!()
            }
            async fn search_stream(
                &self,
                _query: uc_types::SearchQuery,
            ) -> Result<uc_types::SearchStream, EngineError> {
                unreachable!()
            }
            async fn submit_task(
                &self,
                _description: String,
                _project_id: String,
            ) -> Result<uc_types::Task, EngineError> {
                unreachable!()
            }
            async fn get_task(&self, _task_id: &str) -> Result<uc_types::Task, EngineError> {
                unreachable!()
            }
            async fn list_tasks(&self) -> Result<Vec<uc_types::Task>, EngineError> {
                unreachable!()
            }
            async fn pause_task(&self, _task_id: &str) -> Result<uc_types::Task, EngineError> {
                unreachable!()
            }
            async fn resume_task(&self, _task_id: &str) -> Result<uc_types::Task, EngineError> {
                unreachable!()
            }
        }

        let engine = NoSchedulerEngine;
        let result = engine.remove_job("any-id").await.unwrap();

        assert!(
            !result.success,
            "Default remove_job impl should return success=false"
        );
        assert!(
            result.error.is_some(),
            "Default remove_job impl should return an error message"
        );

        let toggle = engine
            .set_scheduler_job_enabled("any-id", false)
            .await
            .unwrap();
        assert!(
            !toggle.success,
            "Default set_scheduler_job_enabled impl should return success=false"
        );
        assert!(
            toggle.error.is_some(),
            "Default set_scheduler_job_enabled impl should return an error message"
        );
        assert!(
            !toggle.enabled,
            "Default impl should echo the requested state"
        );
    }

    // ── LockProvider tests ─────────────────────────────────────────

    /// A mock lock provider whose acquire result can be controlled in tests.
    struct MockLockProvider {
        acquire_result: bool,
    }

    impl LockProvider for MockLockProvider {
        fn try_acquire(&self, _key: &str, _ttl: Duration) -> bool {
            self.acquire_result
        }
    }

    #[tokio::test]
    async fn default_lock_provider_is_noop() {
        // SchedulerService::new() defaults to NoOpLockProvider, which always
        // acquires. This is the single-instance fallback — no regression.
        let service = SchedulerService::new();
        let provider = service.lock_provider.read().await.clone();
        assert!(
            provider.try_acquire("any-key", Duration::from_secs(30)),
            "Default lock provider should always acquire"
        );
    }

    #[tokio::test]
    async fn set_lock_provider_replaces_default() {
        // Verify that set_lock_provider swaps in a custom provider.
        let service = SchedulerService::new();

        // Default acquires
        {
            let provider = service.lock_provider.read().await.clone();
            assert!(provider.try_acquire("key", Duration::from_secs(30)));
        }

        // Replace with a provider that always fails
        let mock = Arc::new(MockLockProvider {
            acquire_result: false,
        }) as Arc<dyn LockProvider>;
        service.set_lock_provider(mock).await;

        // Now it should not acquire
        let provider = service.lock_provider.read().await.clone();
        assert!(
            !provider.try_acquire("key", Duration::from_secs(30)),
            "Replaced lock provider should reflect the new behavior"
        );
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn cron_callback_skips_on_lock_failure() {
        // When the lock provider returns false (another instance holds the lock),
        // the cron callback should skip dispatch_with_guard entirely — no
        // ExecutionHistory is recorded.
        use std::sync::Arc;
        use std::time::Duration;

        let service = SchedulerService::new();

        // Set a lock provider that always fails (simulates another instance
        // holding the lock).
        let mock = Arc::new(MockLockProvider {
            acquire_result: false,
        }) as Arc<dyn LockProvider>;
        service.set_lock_provider(mock).await;

        service.start().await.unwrap();

        // Create a cron job that fires every second
        let task = ScheduledTask::cron(
            "Lock skip test".to_string(),
            "test-project".to_string(),
            "* * * * * *".to_string(), // 6-field: every second
            NaiveTime::from_hms_opt(0, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(23, 59, 59).unwrap(),
            "UTC".to_string(),
        );

        let result = service.add_cron_job(task).await.unwrap();
        let task_id = result.task_id;

        // Wait for the cron to fire (2 seconds to be safe)
        tokio::time::sleep(Duration::from_secs(2)).await;

        // No execution history should be recorded — the lock failed, so
        // dispatch_with_guard was never called.
        let history = service.get_execution_history(Some(&task_id)).await;
        assert!(
            history.is_empty(),
            "Cron callback should skip dispatch when lock acquisition fails"
        );

        service.stop().await.unwrap();
    }

    #[cfg(feature = "scheduler")]
    #[tokio::test]
    async fn cron_callback_fires_when_lock_acquired() {
        // When the lock provider returns true (lock acquired), the cron
        // callback should proceed to dispatch_with_guard and record
        // ExecutionHistory.
        use std::sync::Arc;
        use std::time::Duration;

        let service = SchedulerService::new();

        // Explicitly set NoOp (always acquire) — this is the default, but
        // be explicit for test clarity.
        let noop = Arc::new(NoOpLockProvider) as Arc<dyn LockProvider>;
        service.set_lock_provider(noop).await;

        service.start().await.unwrap();

        // Create a cron job that fires every second
        let task = ScheduledTask::cron(
            "Lock acquire test".to_string(),
            "test-project".to_string(),
            "* * * * * *".to_string(), // 6-field: every second
            NaiveTime::from_hms_opt(0, 0, 0).unwrap(),
            NaiveTime::from_hms_opt(23, 59, 59).unwrap(),
            "UTC".to_string(),
        );

        let result = service.add_cron_job(task).await.unwrap();
        let task_id = result.task_id;

        // Wait for the cron to fire (2 seconds to be safe)
        tokio::time::sleep(Duration::from_secs(2)).await;

        // Execution history should be recorded — the lock acquired, so
        // dispatch_with_guard was called.
        let history = service.get_execution_history(Some(&task_id)).await;
        assert!(
            !history.is_empty(),
            "Cron callback should dispatch when lock acquisition succeeds"
        );

        service.stop().await.unwrap();
    }
}
