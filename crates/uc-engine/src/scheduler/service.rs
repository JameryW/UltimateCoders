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
use tokio::sync::RwLock;
use tracing::{info, warn};
use uuid::Uuid;

use uc_types::{EngineError, ExecutionHistory, ExecutionStatus, ScheduledTask};

use super::night_window::NightWindow;
use super::store::ScheduleStore;

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
    /// Whether to enforce the night window guard.
    #[allow(dead_code)] // Used in PR4 for per-job night window control
    enforce_night_window: bool,
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
pub struct SchedulerService {
    /// Night window configuration (if any).
    night_window: Arc<RwLock<Option<NightWindow>>>,
    /// Job metadata indexed by task ID.
    job_metadata: Arc<RwLock<HashMap<Uuid, JobMetadata>>>,
    /// Execution history records (in-memory cache; store is source of truth).
    execution_history: Arc<RwLock<Vec<ExecutionHistory>>>,
    /// The dispatcher for executing tasks.
    dispatcher: Arc<dyn ScheduleDispatcher>,
    /// The persistence store for scheduled tasks and execution history.
    store: Arc<dyn ScheduleStore>,
    /// Whether the scheduler has been started.
    started: Arc<RwLock<bool>>,
    /// The tokio-cron-scheduler instance (when the `scheduler` feature is enabled).
    #[cfg(feature = "scheduler")]
    job_scheduler: Arc<RwLock<Option<tokio_cron_scheduler::JobScheduler>>>,
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
            dispatcher,
            store,
            started: Arc::new(RwLock::new(false)),
            #[cfg(feature = "scheduler")]
            job_scheduler: Arc::new(RwLock::new(None)),
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
    pub async fn add_cron_job(&self, task: ScheduledTask) -> Result<AddJobResult, EngineError> {
        let cron_expr = task.cron_expression.clone().ok_or_else(|| {
            EngineError::ConfigError("Cron expression required for cron job".to_string())
        })?;

        // Validate the cron expression using croner
        croner::Cron::from_str(&cron_expr).map_err(|e| {
            EngineError::ConfigError(format!("Invalid cron expression '{}': {:?}", cron_expr, e))
        })?;

        let task_id = task.id;

        // Persist to store
        self.store.save_task(&task).await?;

        // Register with job scheduler (if feature enabled and started)
        #[cfg(feature = "scheduler")]
        {
            let js = self.job_scheduler.read().await;
            if let Some(scheduler) = js.as_ref() {
                self.register_cron_with_scheduler(scheduler, &task).await?;
            }
        }

        // Store metadata locally
        let metadata = JobMetadata {
            task,
            enforce_night_window: true,
        };
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
    pub async fn add_one_shot_job(&self, task: ScheduledTask) -> Result<AddJobResult, EngineError> {
        let execute_after = task.execute_after.ok_or_else(|| {
            EngineError::ConfigError("execute_after required for one-shot job".to_string())
        })?;

        let task_id = task.id;

        // Persist to store
        self.store.save_task(&task).await?;

        // Register with job scheduler (if feature enabled and started)
        #[cfg(feature = "scheduler")]
        {
            let js = self.job_scheduler.read().await;
            if let Some(scheduler) = js.as_ref() {
                self.register_one_shot_with_scheduler(scheduler, &task)
                    .await?;
            }
        }

        // Store metadata locally
        let metadata = JobMetadata {
            task,
            enforce_night_window: true,
        };
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
            if let Some(scheduler) = js.as_ref() {
                if let Err(e) = scheduler.remove(task_id).await {
                    warn!(task_id = %task_id, error = ?e, "Failed to remove job from tokio-cron-scheduler");
                }
            }
        }

        Ok(())
    }

    /// List all registered jobs.
    pub async fn list_jobs(&self) -> Vec<ScheduledTask> {
        let metadata = self.job_metadata.read().await;
        metadata.values().map(|m| m.task.clone()).collect()
    }

    /// Get a specific job by ID.
    pub async fn get_job(&self, task_id: &Uuid) -> Option<ScheduledTask> {
        let metadata = self.job_metadata.read().await;
        metadata.get(task_id).map(|m| m.task.clone())
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
                match self.dispatcher.dispatch(&task) {
                    Ok(()) => {
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
    async fn record_execution(&self, history: &ExecutionHistory) {
        // Save to store (best-effort; log errors but don't fail the dispatch)
        if let Err(e) = self.store.save_execution(history).await {
            warn!(error = %e, "Failed to persist execution history to store");
        }
        // Also save to in-memory cache
        self.execution_history.write().await.push(history.clone());
    }

    /// Get the execution history for all tasks, or a specific task (from in-memory cache).
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

        // Load persisted tasks into local metadata
        let persisted_tasks = self.store.list_tasks(true).await?;
        let mut metadata = self.job_metadata.write().await;
        for task in &persisted_tasks {
            metadata.insert(
                task.id,
                JobMetadata {
                    task: task.clone(),
                    enforce_night_window: true,
                },
            );
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
            for task in &persisted_tasks {
                if task.cron_expression.is_some() {
                    if let Err(e) = self
                        .register_cron_with_scheduler(&job_scheduler, task)
                        .await
                    {
                        warn!(
                            task_id = %task.id,
                            error = %e,
                            "Failed to register persisted cron task with scheduler during recovery"
                        );
                    }
                } else if task.execute_after.is_some() {
                    if let Err(e) = self
                        .register_one_shot_with_scheduler(&job_scheduler, task)
                        .await
                    {
                        warn!(
                            task_id = %task.id,
                            error = %e,
                            "Failed to register persisted one-shot task with scheduler during recovery"
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
            task_count = persisted_tasks.len(),
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

    // ── tokio-cron-scheduler integration ─────────────────────────

    /// Convert a 5-field cron expression (standard: min hour day month dow)
    /// to a 6-field expression (with seconds) as required by tokio-cron-scheduler.
    ///
    /// If the expression already has 6+ fields, it is returned as-is.
    #[cfg(feature = "scheduler")]
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
    ) -> Result<(), EngineError> {
        let cron_expr = task
            .cron_expression
            .as_ref()
            .ok_or_else(|| EngineError::ConfigError("Cron expression missing".to_string()))?;

        let cron_6field = Self::cron_to_6field(cron_expr);

        let task_id = task.id;
        let task_description = task.description.clone();

        let job = tokio_cron_scheduler::Job::new_async(cron_6field, move |uuid, _l| {
            let task_id = task_id;
            let description = task_description.clone();
            Box::pin(async move {
                tracing::info!(
                    job_uuid = %uuid,
                    task_id = %task_id,
                    description = %description,
                    "Cron job triggered by scheduler"
                );
                // The actual dispatch happens via dispatch_with_guard
                // which is called externally. This callback serves as
                // the trigger notification.
            })
        })
        .map_err(|e| EngineError::InternalError(format!("Failed to create cron job: {:?}", e)))?;

        scheduler.add(job).await.map_err(|e| {
            EngineError::InternalError(format!("Failed to add cron job to scheduler: {:?}", e))
        })?;

        Ok(())
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
    ) -> Result<(), EngineError> {
        let execute_after = task
            .execute_after
            .ok_or_else(|| EngineError::ConfigError("execute_after missing".to_string()))?;

        let now = Utc::now();
        let duration = execute_after.signed_duration_since(now);
        if duration.num_seconds() <= 0 {
            warn!(
                task_id = %task.id,
                execute_after = %execute_after,
                "One-shot job execute_after is in the past, skipping scheduler registration"
            );
            return Ok(());
        }

        let duration_std = std::time::Duration::from_secs(duration.num_seconds().max(0) as u64);
        let task_id = task.id;
        let task_description = task.description.clone();

        let job = tokio_cron_scheduler::Job::new_one_shot_async(duration_std, move |uuid, _l| {
            let task_id = task_id;
            let description = task_description.clone();
            Box::pin(async move {
                tracing::info!(
                    job_uuid = %uuid,
                    task_id = %task_id,
                    description = %description,
                    "One-shot job triggered by scheduler"
                );
                // The actual dispatch happens via dispatch_with_guard
                // which is called externally.
            })
        })
        .map_err(|e| {
            EngineError::InternalError(format!("Failed to create one-shot job: {:?}", e))
        })?;

        scheduler.add(job).await.map_err(|e| {
            EngineError::InternalError(format!("Failed to add one-shot job to scheduler: {:?}", e))
        })?;

        Ok(())
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
}
