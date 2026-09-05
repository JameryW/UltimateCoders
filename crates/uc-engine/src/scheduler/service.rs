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
        task.next_execution = Self::next_execution_for_task(&task, Utc::now())?;

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

        // Remove from tokio-cron-scheduler if running
        #[cfg(feature = "scheduler")]
        {
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

        Ok(())
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

    /// Check if a task should be executed now based on the night window guard.
    ///
    /// Returns Ok(()) if execution should proceed, or Err with a deferral reason
    /// if the task should be deferred to the next window.
    pub async fn check_night_window(&self) -> Result<(), EngineError> {
        let nw = self.night_window.read().await;
        match nw.as_ref() {
            Some(window) => {
                let now = chrono::Utc::now().with_timezone(&window.tz);
                if window.is_within_window(now) {
                    Ok(())
                } else {
                    let next_start = window.next_window_start(now);
                    Err(EngineError::TaskError(format!(
                        "Outside night window. Next window starts at {}",
                        next_start
                    )))
                }
            }
            None => {
                // No night window configured — allow execution at any time
                Ok(())
            }
        }
    }

    /// Dispatch a task, respecting the night window guard.
    ///
    /// If the task is within the night window (or no window is configured),
    /// the task is dispatched immediately. Otherwise, an execution history
    /// record is created with Deferred status.
    pub async fn dispatch_with_guard(&self, task_id: &Uuid) -> Result<(), EngineError> {
        let task = {
            let metadata = self.job_metadata.read().await;
            metadata
                .get(task_id)
                .map(|m| m.task.clone())
                .ok_or_else(|| EngineError::TaskError(format!("Job {} not found", task_id)))?
        };

        // Check night window guard
        match self.check_night_window().await {
            Ok(()) => {
                // Within window — dispatch the task
                let started_at = Utc::now();
                let dispatcher = self.dispatcher.read().await.clone();
                match dispatcher.dispatch(&task) {
                    Ok(()) => {
                        self.mark_execution_started(task_id, started_at).await;
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
                        self.mark_execution_started(task_id, started_at).await;
                        // Dispatch returned Err — NATS unavailable / no worker
                        // received the task. Record as Skipped (not Failed):
                        // the task itself is valid, it just didn't execute.
                        // Returning Err lets the caller decide to retry.
                        let history = ExecutionHistory {
                            id: Uuid::new_v4(),
                            scheduled_task_id: *task_id,
                            started_at,
                            completed_at: Some(Utc::now()),
                            status: ExecutionStatus::Skipped,
                            result_summary: Some(format!("Dispatch skipped (no worker): {}", e)),
                            deferred_reason: None,
                        };
                        self.record_execution(&history).await;
                        Err(e)
                    }
                }
            }
            Err(reason) => {
                // Outside window — defer
                let history = ExecutionHistory::deferred(*task_id, reason.to_string());
                self.record_execution(&history).await;
                warn!(
                    task_id = %task_id,
                    reason = %reason,
                    "Task deferred (outside night window)"
                );
                Err(reason)
            }
        }
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

        // Load persisted tasks into local metadata. Recompute the next
        // occurrence from the current clock so a restart never exposes a
        // stale timestamp left behind by the previous process.
        let persisted_tasks = self.store.list_tasks(true).await?;
        // Recover history for disabled jobs too: they are intentionally absent
        // from the active job registry, but their completed executions still
        // belong in the dashboard's cross-job history view.
        let history_tasks = match self.store.list_tasks(false).await {
            Ok(tasks) => tasks,
            Err(error) => {
                warn!(
                    error = %error,
                    "Failed to list all scheduler tasks for history recovery"
                );
                persisted_tasks.clone()
            }
        };
        self.recover_execution_history(&history_tasks).await;
        let recovery_now = Utc::now();
        let mut recovered_tasks = Vec::with_capacity(persisted_tasks.len());
        for task in persisted_tasks {
            let mut recovered = task.clone();
            match Self::next_execution_for_task(&task, recovery_now) {
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

            // Register all persisted tasks with the job scheduler
            for task in &recovered_tasks {
                if task.cron_expression.is_some() {
                    match self
                        .register_cron_with_scheduler(&job_scheduler, task)
                        .await
                    {
                        Ok(scheduler_job_id) => {
                            self.scheduler_job_ids
                                .write()
                                .await
                                .insert(task.id, scheduler_job_id);
                        }
                        Err(e) => {
                            warn!(
                                task_id = %task.id,
                                error = %e,
                                "Failed to register persisted cron task with scheduler during recovery"
                            );
                        }
                    }
                } else if task.execute_after.is_some() {
                    match self
                        .register_one_shot_with_scheduler(&job_scheduler, task)
                        .await
                    {
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
                                "Failed to register persisted one-shot task with scheduler during recovery"
                            );
                        }
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

        Ok(task
            .execute_after
            .filter(|execute_after| *execute_after > after))
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
    /// attempt. The dispatcher is synchronous and may only acknowledge that a
    /// background submission was started, so this timestamp represents the
    /// scheduler's dispatch boundary rather than eventual worker completion.
    async fn mark_execution_started(&self, task_id: &Uuid, started_at: chrono::DateTime<Utc>) {
        let updated_task = {
            let mut metadata = self.job_metadata.write().await;
            let Some(job) = metadata.get_mut(task_id) else {
                return;
            };

            // A manual trigger can race a cron callback. Never let a slower
            // completion move the visible last-run timestamp backwards.
            if job
                .task
                .last_execution
                .map(|last| started_at >= last)
                .unwrap_or(true)
            {
                job.task.last_execution = Some(started_at);
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
        let execute_after = task
            .execute_after
            .ok_or_else(|| EngineError::ConfigError("execute_after missing".to_string()))?;

        let now = Utc::now();
        let Some(duration_std) = Self::one_shot_duration(execute_after, now)? else {
            warn!(
                task_id = %task.id,
                execute_after = %execute_after,
                "One-shot job execute_after is in the past, skipping scheduler registration"
            );
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
                    svc.forget_scheduler_job_id(&task_id).await;
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
                svc.forget_scheduler_job_id(&task_id).await;
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
        assert_eq!(recovered.job_count().await, 1);
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
    async fn start_clears_expired_one_shot_next_execution() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());
        let mut task = make_one_shot_task(Utc::now() - chrono::Duration::hours(1));
        task.next_execution = Some(Utc::now() + chrono::Duration::hours(1));
        store.save_task(&task).await.unwrap();

        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        let recovered = service.get_job(&task.id).await.unwrap();
        assert!(
            recovered.next_execution.is_none(),
            "expired one-shot tasks must not advertise a future run"
        );
        let persisted = store.load_task(&task.id).await.unwrap().unwrap();
        assert!(persisted.next_execution.is_none());
        service.stop().await.unwrap();
    }

    #[tokio::test]
    async fn start_reloads_only_enabled_tasks() {
        let store = Arc::new(super::super::store::InMemoryScheduleStore::new());

        // Pre-populate store with tasks
        let task1 = make_cron_task("0 22 * * *");
        let mut task2 = make_cron_task("0 23 * * *");
        task2.enabled = false;
        store.save_task(&task1).await.unwrap();
        store.save_task(&task2).await.unwrap();

        // Create service and start — should recover only enabled tasks
        let service = SchedulerService::with_store(store.clone());
        service.start().await.unwrap();

        // Only the enabled task should be loaded
        assert_eq!(service.job_count().await, 1);
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
