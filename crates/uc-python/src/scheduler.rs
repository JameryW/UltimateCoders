//! PySchedulerService — Python-facing Scheduler class.
//!
//! Wraps the Rust `SchedulerService` and exposes it via PyO3.
//! Provides Python API for cron-based and one-shot job scheduling,
//! night window configuration, and execution history queries.
//!
//! Uses `py.allow_threads()` + `block_on()` for sync wrappers that
//! call async Rust methods, following the same pattern as `PyEngine`.

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3_async_runtimes::tokio::future_into_py;

use uc_engine::SchedulerService;

use crate::async_support;

/// Convert EngineError to a Python exception.
///
/// Shared with engine.rs — duplicated here to avoid circular refactoring.
fn engine_error_to_pyerr(err: uc_types::EngineError) -> PyErr {
    use uc_types::EngineError::*;
    match err {
        NotFound(msg) => pyo3::exceptions::PyKeyError::new_err(msg),
        SearchError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        IndexError(msg) => pyo3::exceptions::PyKeyError::new_err(msg),
        MemoryReadError(msg) | MemoryWriteError(msg) => {
            pyo3::exceptions::PyRuntimeError::new_err(msg)
        }
        IndexingError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        ConnectionError(msg) => pyo3::exceptions::PyConnectionError::new_err(msg),
        TimeoutError(msg) => pyo3::exceptions::PyTimeoutError::new_err(msg),
        RateLimited(secs) => pyo3::exceptions::PyRuntimeError::new_err(format!(
            "Rate limited, retry after {}s",
            secs
        )),
        ConflictError { path, details } => {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Conflict in {}: {}", path, details))
        }
        TaskError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
        WorkerUnavailable(msg) => pyo3::exceptions::PyConnectionError::new_err(msg),
        SandboxError(msg) => pyo3::exceptions::PyPermissionError::new_err(msg),
        ConfigError(msg) => pyo3::exceptions::PyValueError::new_err(msg),
        InternalError(msg) => pyo3::exceptions::PyRuntimeError::new_err(msg),
    }
}

/// Parse a time string in "HH:MM" format to a `chrono::NaiveTime`.
fn parse_time_str(s: &str) -> PyResult<chrono::NaiveTime> {
    let parts: Vec<&str> = s.split(':').collect();
    if parts.len() != 2 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Invalid time format '{}'. Expected HH:MM",
            s
        )));
    }
    let hour: u32 = parts[0]
        .parse()
        .map_err(|_| pyo3::exceptions::PyValueError::new_err(format!("Invalid hour in '{}'", s)))?;
    let minute: u32 = parts[1].parse().map_err(|_| {
        pyo3::exceptions::PyValueError::new_err(format!("Invalid minute in '{}'", s))
    })?;
    chrono::NaiveTime::from_hms_opt(hour, minute, 0).ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Invalid time '{}'. Hour must be 0-23, minute must be 0-59",
            s
        ))
    })
}

/// Parse an ISO 8601 datetime string to a `chrono::DateTime<Utc>`.
fn parse_datetime_str(s: &str) -> PyResult<chrono::DateTime<chrono::Utc>> {
    // Try parsing as ISO 8601 with timezone info first
    if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(s) {
        return Ok(dt.with_timezone(&chrono::Utc));
    }
    // Try parsing as naive datetime and assume UTC
    if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S") {
        return Ok(naive.and_utc());
    }
    if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S") {
        return Ok(naive.and_utc());
    }
    // Try date only (midnight UTC)
    if let Ok(naive_date) = chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d") {
        let naive = naive_date.and_hms_opt(0, 0, 0).unwrap();
        return Ok(naive.and_utc());
    }
    Err(pyo3::exceptions::PyValueError::new_err(format!(
        "Invalid datetime format '{}'. Expected ISO 8601 (e.g., '2024-01-15T22:00:00Z')",
        s
    )))
}

// ── Python-facing type wrappers ──────────────────────────────────

/// Python wrapper for `ScheduledTask`.
///
/// Represents a scheduled task (cron or one-shot) registered with the scheduler.
#[pyclass]
#[derive(Clone)]
pub struct PyScheduledTask {
    inner: uc_types::ScheduledTask,
}

#[pymethods]
impl PyScheduledTask {
    /// Unique identifier for this scheduled task.
    #[getter]
    pub fn id(&self) -> String {
        self.inner.id.to_string()
    }

    /// Human-readable description of the task.
    #[getter]
    pub fn description(&self) -> &str {
        &self.inner.description
    }

    /// The project/repository context for the task.
    #[getter]
    pub fn project_id(&self) -> &str {
        &self.inner.project_id
    }

    /// Cron expression for recurring tasks (None for one-shot tasks).
    #[getter]
    pub fn cron_expression(&self) -> Option<&str> {
        self.inner.cron_expression.as_deref()
    }

    /// Execute after this timestamp for one-shot tasks (None for cron tasks).
    /// Returns ISO 8601 string.
    #[getter]
    pub fn execute_after(&self) -> Option<String> {
        self.inner.execute_after.map(|dt| dt.to_rfc3339())
    }

    /// Start of the night execution window (HH:MM format).
    #[getter]
    pub fn night_window_start(&self) -> String {
        self.inner.night_window_start.format("%H:%M").to_string()
    }

    /// End of the night execution window (HH:MM format).
    #[getter]
    pub fn night_window_end(&self) -> String {
        self.inner.night_window_end.format("%H:%M").to_string()
    }

    /// IANA timezone name for the night window.
    #[getter]
    pub fn timezone(&self) -> &str {
        &self.inner.timezone
    }

    /// Whether this scheduled task is enabled.
    #[getter]
    pub fn enabled(&self) -> bool {
        self.inner.enabled
    }

    /// Timestamp of the last execution (ISO 8601 string, or None).
    #[getter]
    pub fn last_execution(&self) -> Option<String> {
        self.inner.last_execution.map(|dt| dt.to_rfc3339())
    }

    /// Timestamp of the next scheduled execution (ISO 8601 string, or None).
    #[getter]
    pub fn next_execution(&self) -> Option<String> {
        self.inner.next_execution.map(|dt| dt.to_rfc3339())
    }

    /// When this scheduled task was created (ISO 8601 string).
    #[getter]
    pub fn created_at(&self) -> String {
        self.inner.created_at.to_rfc3339()
    }

    /// When this scheduled task was last updated (ISO 8601 string).
    #[getter]
    pub fn updated_at(&self) -> String {
        self.inner.updated_at.to_rfc3339()
    }

    /// Whether this is a recurring cron task.
    pub fn is_cron(&self) -> bool {
        self.inner.is_cron()
    }

    /// Whether this is a one-shot delayed task.
    pub fn is_one_shot(&self) -> bool {
        self.inner.is_one_shot()
    }

    fn __repr__(&self) -> String {
        let kind = if self.is_cron() {
            format!("cron={}", self.cron_expression().unwrap_or("?"))
        } else {
            format!(
                "one_shot_after={}",
                self.execute_after().unwrap_or_else(|| "N/A".to_string())
            )
        };
        format!(
            "ScheduledTask(id={}, description={}, {})",
            self.id(),
            self.description(),
            kind
        )
    }
}

impl From<uc_types::ScheduledTask> for PyScheduledTask {
    fn from(task: uc_types::ScheduledTask) -> Self {
        Self { inner: task }
    }
}

/// Python wrapper for `ExecutionHistory`.
///
/// Represents a record of a scheduled task execution.
#[pyclass]
#[derive(Clone)]
pub struct PyExecutionHistory {
    inner: uc_types::ExecutionHistory,
}

#[pymethods]
impl PyExecutionHistory {
    /// Unique identifier for this execution record.
    #[getter]
    pub fn id(&self) -> String {
        self.inner.id.to_string()
    }

    /// The scheduled task ID that was executed.
    #[getter]
    pub fn scheduled_task_id(&self) -> String {
        self.inner.scheduled_task_id.to_string()
    }

    /// When the execution started (ISO 8601 string).
    #[getter]
    pub fn started_at(&self) -> String {
        self.inner.started_at.to_rfc3339()
    }

    /// When the execution completed (ISO 8601 string, or None if still running).
    #[getter]
    pub fn completed_at(&self) -> Option<String> {
        self.inner.completed_at.map(|dt| dt.to_rfc3339())
    }

    /// The status of this execution ("Completed", "Failed", "Skipped", or "Deferred").
    #[getter]
    pub fn status(&self) -> &str {
        match self.inner.status {
            uc_types::ExecutionStatus::Completed => "Completed",
            uc_types::ExecutionStatus::Failed => "Failed",
            uc_types::ExecutionStatus::Skipped => "Skipped",
            uc_types::ExecutionStatus::Deferred => "Deferred",
        }
    }

    /// Summary of the execution result.
    #[getter]
    pub fn result_summary(&self) -> Option<&str> {
        self.inner.result_summary.as_deref()
    }

    /// Reason for deferral (if status is "Deferred").
    #[getter]
    pub fn deferred_reason(&self) -> Option<&str> {
        self.inner.deferred_reason.as_deref()
    }

    fn __repr__(&self) -> String {
        format!(
            "ExecutionHistory(id={}, task_id={}, status={})",
            self.id(),
            self.scheduled_task_id(),
            self.status()
        )
    }
}

impl From<uc_types::ExecutionHistory> for PyExecutionHistory {
    fn from(history: uc_types::ExecutionHistory) -> Self {
        Self { inner: history }
    }
}

// ── PySchedulerService ──────────────────────────────────────────

/// Python-facing Scheduler Service.
///
/// Wraps the Rust `SchedulerService` and provides methods for
/// cron-based and one-shot job scheduling, night window configuration,
/// and execution history queries.
///
/// Usage in Python:
///   scheduler = SchedulerService()
///   scheduler.set_night_window("22:00", "06:00", "Asia/Shanghai")
///   task = scheduler.create_cron_job("Rebuild index", "0 22 * * *")
///   scheduler.start()
#[pyclass]
pub struct PySchedulerService {
    inner: Arc<SchedulerService>,
}

#[pymethods]
impl PySchedulerService {
    /// Create a new SchedulerService instance.
    ///
    /// Uses in-memory store by default. For production use with PostgreSQL,
    /// configure the store via the Rust API directly.
    #[new]
    #[pyo3(signature = ())]
    pub fn new() -> PyResult<Self> {
        let service = SchedulerService::new();
        Ok(PySchedulerService {
            inner: Arc::new(service),
        })
    }

    /// Create a cron-based recurring job.
    ///
    /// Args:
    ///     description: Human-readable description of the task.
    ///     cron_expression: Standard cron expression (e.g., "0 22 * * *").
    ///     project_id: Project/repository context (default: "").
    ///     night_window_start: Night window start time in HH:MM (default: "22:00").
    ///     night_window_end: Night window end time in HH:MM (default: "06:00").
    ///     timezone: IANA timezone name (default: "UTC").
    ///
    /// Returns:
    ///     PyScheduledTask with the created task details.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (description, cron_expression, project_id=None, night_window_start=None, night_window_end=None, timezone="UTC".to_string()))]
    #[pyo3(
        text_signature = "(description, cron_expression, project_id=None, night_window_start=None, night_window_end=None, timezone='UTC')"
    )]
    pub fn create_cron_job(
        &self,
        py: Python<'_>,
        description: String,
        cron_expression: String,
        project_id: Option<String>,
        night_window_start: Option<String>,
        night_window_end: Option<String>,
        timezone: String,
    ) -> PyResult<PyScheduledTask> {
        let nw_start = parse_time_str(night_window_start.as_deref().unwrap_or("22:00"))?;
        let nw_end = parse_time_str(night_window_end.as_deref().unwrap_or("06:00"))?;

        let task = uc_types::ScheduledTask::cron(
            description,
            project_id.unwrap_or_default(),
            cron_expression,
            nw_start,
            nw_end,
            timezone,
        );

        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.add_cron_job(task)))
            .map_err(engine_error_to_pyerr)?;

        // Fetch the task back from the scheduler to return full details
        let inner2 = self.inner.clone();
        let fetched = py.allow_threads(|| async_support::block_on(inner2.get_job(&result.task_id)));
        Ok(fetched
            .map(PyScheduledTask::from)
            .unwrap_or_else(|| PyScheduledTask {
                inner: uc_types::ScheduledTask::new(
                    "unknown".to_string(),
                    "".to_string(),
                    nw_start,
                    nw_end,
                    "UTC".to_string(),
                ),
            }))
    }

    /// Create a one-shot delayed job.
    ///
    /// Args:
    ///     description: Human-readable description of the task.
    ///     execute_after: ISO 8601 datetime string (e.g., "2024-01-15T22:00:00Z").
    ///     project_id: Project/repository context (default: "").
    ///     night_window_start: Night window start time in HH:MM (default: "22:00").
    ///     night_window_end: Night window end time in HH:MM (default: "06:00").
    ///     timezone: IANA timezone name (default: "UTC").
    ///
    /// Returns:
    ///     PyScheduledTask with the created task details.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (description, execute_after, project_id=None, night_window_start=None, night_window_end=None, timezone="UTC".to_string()))]
    #[pyo3(
        text_signature = "(description, execute_after, project_id=None, night_window_start=None, night_window_end=None, timezone='UTC')"
    )]
    pub fn create_one_shot_job(
        &self,
        py: Python<'_>,
        description: String,
        execute_after: String,
        project_id: Option<String>,
        night_window_start: Option<String>,
        night_window_end: Option<String>,
        timezone: String,
    ) -> PyResult<PyScheduledTask> {
        let execute_after_dt = parse_datetime_str(&execute_after)?;
        let nw_start = parse_time_str(night_window_start.as_deref().unwrap_or("22:00"))?;
        let nw_end = parse_time_str(night_window_end.as_deref().unwrap_or("06:00"))?;

        let task = uc_types::ScheduledTask::one_shot(
            description,
            project_id.unwrap_or_default(),
            execute_after_dt,
            nw_start,
            nw_end,
            timezone,
        );

        let inner = self.inner.clone();
        let result = py
            .allow_threads(|| async_support::block_on(inner.add_one_shot_job(task)))
            .map_err(engine_error_to_pyerr)?;

        // Fetch the task back from the scheduler to return full details
        let inner2 = self.inner.clone();
        let fetched = py.allow_threads(|| async_support::block_on(inner2.get_job(&result.task_id)));
        Ok(fetched
            .map(PyScheduledTask::from)
            .unwrap_or_else(|| PyScheduledTask {
                inner: uc_types::ScheduledTask::new(
                    "unknown".to_string(),
                    "".to_string(),
                    nw_start,
                    nw_end,
                    "UTC".to_string(),
                ),
            }))
    }

    /// Cancel (remove) a scheduled job.
    ///
    /// Args:
    ///     task_id: The UUID string of the task to cancel.
    ///
    /// Returns:
    ///     True if the job was successfully cancelled.
    pub fn cancel_job(&self, py: Python<'_>, task_id: String) -> PyResult<bool> {
        let uuid = uuid::Uuid::parse_str(&task_id).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid UUID: '{}'", task_id))
        })?;
        let inner = self.inner.clone();
        py.allow_threads(|| async_support::block_on(inner.remove_job(&uuid)))
            .map_err(engine_error_to_pyerr)?;
        Ok(true)
    }

    /// List all registered jobs.
    ///
    /// Returns:
    ///     List of PyScheduledTask objects.
    pub fn list_jobs(&self, py: Python<'_>) -> PyResult<Vec<PyScheduledTask>> {
        let inner = self.inner.clone();
        let jobs = py.allow_threads(|| async_support::block_on(inner.list_jobs()));
        Ok(jobs.into_iter().map(PyScheduledTask::from).collect())
    }

    /// Get a specific job by ID.
    ///
    /// Args:
    ///     task_id: The UUID string of the task.
    ///
    /// Returns:
    ///     PyScheduledTask if found, None otherwise.
    pub fn get_job(&self, py: Python<'_>, task_id: String) -> PyResult<Option<PyScheduledTask>> {
        let uuid = uuid::Uuid::parse_str(&task_id).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid UUID: '{}'", task_id))
        })?;
        let inner = self.inner.clone();
        let job = py.allow_threads(|| async_support::block_on(inner.get_job(&uuid)));
        Ok(job.map(PyScheduledTask::from))
    }

    /// Get execution history for a specific task.
    ///
    /// Args:
    ///     task_id: The UUID string of the task.
    ///     limit: Maximum number of records to return (default: 50).
    ///
    /// Returns:
    ///     List of PyExecutionHistory objects.
    #[pyo3(signature = (task_id, limit=50))]
    #[pyo3(text_signature = "(task_id, limit=50)")]
    pub fn get_execution_history(
        &self,
        py: Python<'_>,
        task_id: String,
        limit: i64,
    ) -> PyResult<Vec<PyExecutionHistory>> {
        let uuid = uuid::Uuid::parse_str(&task_id).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid UUID: '{}'", task_id))
        })?;
        let inner = self.inner.clone();
        let history = py
            .allow_threads(|| {
                async_support::block_on(inner.get_execution_history_from_store(&uuid, limit))
            })
            .map_err(engine_error_to_pyerr)?;
        Ok(history.into_iter().map(PyExecutionHistory::from).collect())
    }

    /// Set the night window configuration.
    ///
    /// Args:
    ///     start_time: Night window start time in HH:MM (e.g., "22:00").
    ///     end_time: Night window end time in HH:MM (e.g., "06:00").
    ///     timezone: IANA timezone name (default: "UTC").
    #[pyo3(signature = (start_time, end_time, timezone="UTC".to_string()))]
    #[pyo3(text_signature = "(start_time, end_time, timezone='UTC')")]
    pub fn set_night_window(
        &self,
        py: Python<'_>,
        start_time: String,
        end_time: String,
        timezone: String,
    ) -> PyResult<()> {
        let start = parse_time_str(&start_time)?;
        let end = parse_time_str(&end_time)?;
        let config = uc_types::NightWindowConfig::new(start, end, timezone);
        let inner = self.inner.clone();
        py.allow_threads(|| async_support::block_on(inner.set_night_window(&config)))
            .map_err(engine_error_to_pyerr)?;
        Ok(())
    }

    /// Clear the night window configuration (allow execution at any time).
    pub fn clear_night_window(&self, py: Python<'_>) -> PyResult<()> {
        let inner = self.inner.clone();
        py.allow_threads(|| async_support::block_on(inner.clear_night_window()));
        Ok(())
    }

    /// Start the scheduler.
    ///
    /// Loads persisted tasks and begins the scheduling loop.
    pub fn start(&self, py: Python<'_>) -> PyResult<()> {
        let inner = self.inner.clone();
        py.allow_threads(|| async_support::block_on(inner.start()))
            .map_err(engine_error_to_pyerr)?;
        Ok(())
    }

    /// Stop the scheduler.
    pub fn stop(&self, py: Python<'_>) -> PyResult<()> {
        let inner = self.inner.clone();
        py.allow_threads(|| async_support::block_on(inner.stop()))
            .map_err(engine_error_to_pyerr)?;
        Ok(())
    }

    /// Whether the scheduler is currently running.
    pub fn is_running(&self, py: Python<'_>) -> PyResult<bool> {
        let inner = self.inner.clone();
        Ok(py.allow_threads(|| async_support::block_on(inner.is_running())))
    }

    // ── Async methods ─────────────────────────────────────────

    /// Async version of create_cron_job().
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (description, cron_expression, project_id=None, night_window_start=None, night_window_end=None, timezone="UTC".to_string()))]
    pub fn create_cron_job_async<'py>(
        &self,
        py: Python<'py>,
        description: String,
        cron_expression: String,
        project_id: Option<String>,
        night_window_start: Option<String>,
        night_window_end: Option<String>,
        timezone: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let nw_start = parse_time_str(night_window_start.as_deref().unwrap_or("22:00"))?;
        let nw_end = parse_time_str(night_window_end.as_deref().unwrap_or("06:00"))?;

        let task = uc_types::ScheduledTask::cron(
            description,
            project_id.unwrap_or_default(),
            cron_expression,
            nw_start,
            nw_end,
            timezone,
        );

        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner
                .add_cron_job(task)
                .await
                .map_err(engine_error_to_pyerr)?;
            // Fetch back to get full details
            let fetched = inner.get_job(&result.task_id).await;
            Ok(fetched
                .map(PyScheduledTask::from)
                .unwrap_or_else(|| PyScheduledTask {
                    inner: uc_types::ScheduledTask::new(
                        "unknown".to_string(),
                        "".to_string(),
                        nw_start,
                        nw_end,
                        "UTC".to_string(),
                    ),
                }))
        })
    }

    /// Async version of create_one_shot_job().
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (description, execute_after, project_id=None, night_window_start=None, night_window_end=None, timezone="UTC".to_string()))]
    pub fn create_one_shot_job_async<'py>(
        &self,
        py: Python<'py>,
        description: String,
        execute_after: String,
        project_id: Option<String>,
        night_window_start: Option<String>,
        night_window_end: Option<String>,
        timezone: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let execute_after_dt = parse_datetime_str(&execute_after)?;
        let nw_start = parse_time_str(night_window_start.as_deref().unwrap_or("22:00"))?;
        let nw_end = parse_time_str(night_window_end.as_deref().unwrap_or("06:00"))?;

        let task = uc_types::ScheduledTask::one_shot(
            description,
            project_id.unwrap_or_default(),
            execute_after_dt,
            nw_start,
            nw_end,
            timezone,
        );

        let inner = self.inner.clone();
        future_into_py(py, async move {
            let result = inner
                .add_one_shot_job(task)
                .await
                .map_err(engine_error_to_pyerr)?;
            let fetched = inner.get_job(&result.task_id).await;
            Ok(fetched
                .map(PyScheduledTask::from)
                .unwrap_or_else(|| PyScheduledTask {
                    inner: uc_types::ScheduledTask::new(
                        "unknown".to_string(),
                        "".to_string(),
                        nw_start,
                        nw_end,
                        "UTC".to_string(),
                    ),
                }))
        })
    }

    /// Async version of cancel_job().
    pub fn cancel_job_async<'py>(
        &self,
        py: Python<'py>,
        task_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let uuid = uuid::Uuid::parse_str(&task_id).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid UUID: '{}'", task_id))
        })?;
        let inner = self.inner.clone();
        future_into_py(py, async move {
            inner
                .remove_job(&uuid)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(true)
        })
    }

    /// Async version of list_jobs().
    pub fn list_jobs_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let jobs = inner.list_jobs().await;
            Ok(jobs
                .into_iter()
                .map(PyScheduledTask::from)
                .collect::<Vec<_>>())
        })
    }

    /// Async version of get_job().
    pub fn get_job_async<'py>(
        &self,
        py: Python<'py>,
        task_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let uuid = uuid::Uuid::parse_str(&task_id).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid UUID: '{}'", task_id))
        })?;
        let inner = self.inner.clone();
        future_into_py(py, async move {
            Ok(inner.get_job(&uuid).await.map(PyScheduledTask::from))
        })
    }

    /// Async version of get_execution_history().
    #[pyo3(signature = (task_id, limit=50))]
    pub fn get_execution_history_async<'py>(
        &self,
        py: Python<'py>,
        task_id: String,
        limit: i64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let uuid = uuid::Uuid::parse_str(&task_id).map_err(|_| {
            pyo3::exceptions::PyValueError::new_err(format!("Invalid UUID: '{}'", task_id))
        })?;
        let inner = self.inner.clone();
        future_into_py(py, async move {
            let history = inner
                .get_execution_history_from_store(&uuid, limit)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(history
                .into_iter()
                .map(PyExecutionHistory::from)
                .collect::<Vec<_>>())
        })
    }

    /// Async version of set_night_window().
    #[pyo3(signature = (start_time, end_time, timezone="UTC".to_string()))]
    pub fn set_night_window_async<'py>(
        &self,
        py: Python<'py>,
        start_time: String,
        end_time: String,
        timezone: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let start = parse_time_str(&start_time)?;
        let end = parse_time_str(&end_time)?;
        let config = uc_types::NightWindowConfig::new(start, end, timezone);
        let inner = self.inner.clone();
        future_into_py(py, async move {
            inner
                .set_night_window(&config)
                .await
                .map_err(engine_error_to_pyerr)?;
            Ok(())
        })
    }

    /// Async version of clear_night_window().
    pub fn clear_night_window_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            inner.clear_night_window().await;
            Ok(())
        })
    }

    /// Async version of start().
    pub fn start_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            inner.start().await.map_err(engine_error_to_pyerr)?;
            Ok(())
        })
    }

    /// Async version of stop().
    pub fn stop_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move {
            inner.stop().await.map_err(engine_error_to_pyerr)?;
            Ok(())
        })
    }

    /// Async version of is_running().
    pub fn is_running_async<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = self.inner.clone();
        future_into_py(py, async move { Ok(inner.is_running().await) })
    }
}
