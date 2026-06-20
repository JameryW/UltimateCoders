//! Local worker bridge — manages a Python subprocess for task execution.
//!
//! Spawns ``python -m ultimate_coders.local_worker`` as a long-running child
//! process and communicates via JSON-RPC 2.0 over stdin/stdout (newline-delimited).
//!
//! When the worker is unavailable (Python not installed, extension not built),
//! the bridge reports ``is_available() == false`` and the gRPC server falls
//! back to local (newline-split) decomposition.
//!
//! ## Async architecture
//!
//! The bridge is designed for async operation:
//! - ``send_submit_task()`` sends a JSON-RPC request and returns immediately
//! - A background **notification reader** task reads stdout, parses JSON-RPC
//!   notifications and responses, applies updates to the TaskStore, and
//!   broadcasts events via a ``broadcast::Sender<TaskEvent>``
//! - ``WatchTask`` streams subscribe to the broadcast channel for real-time delivery
//!
//! ## Worker lifecycle
//!
//! - **Spawn**: ``ensure_worker()`` spawns the process and health-checks with ping
//! - **Restart**: ``restart()`` kills the old process and spawns a new one
//! - **Graceful shutdown**: ``graceful_shutdown()`` sends a ``shutdown`` JSON-RPC
//!   method, waits 5 seconds, then SIGKILL
//! - **Crash recovery**: The notification reader detects stdout EOF, marks tasks
//!   as Failed, and attempts auto-restart with exponential backoff (max 3 retries)

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

/// JSON-RPC request ID counter (shared across all calls).
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

/// Maximum number of auto-restart attempts after worker crash.
const MAX_RESTART_ATTEMPTS: u32 = 3;

/// Base delay for exponential backoff on restart (milliseconds).
const RESTART_BASE_DELAY_MS: u64 = 1000;

/// Graceful shutdown timeout before SIGKILL (seconds).
const GRACEFUL_SHUTDOWN_TIMEOUT_SECS: u64 = 5;

// ── JSON-RPC types ────────────────────────────────────────────

#[derive(Serialize)]
struct JsonRpcRequest {
    jsonrpc: &'static str,
    id: u64,
    method: &'static str,
    params: Value,
}

#[derive(Deserialize, Debug)]
struct JsonRpcResponse {
    #[allow(dead_code)]
    id: Option<u64>,
    #[allow(dead_code)]
    result: Option<Value>,
    #[allow(dead_code)]
    error: Option<JsonRpcError>,
}

#[derive(Deserialize, Debug)]
struct JsonRpcNotification {
    #[allow(dead_code)]
    method: String,
    params: Option<Value>,
}

#[derive(Deserialize, Debug)]
struct JsonRpcError {
    #[allow(dead_code)]
    code: i64,
    #[allow(dead_code)]
    message: String,
}

/// Task update from the worker (maps to ``task_update`` notification params).
#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct WorkerTaskUpdate {
    pub task_id: String,
    pub description: String,
    #[serde(default)]
    pub project_id: String,
    pub status: String,
    #[serde(default)]
    pub subtasks: Vec<WorkerSubtaskUpdate>,
    #[serde(default)]
    pub result: Option<String>,
}

/// Subtask update within a worker notification.
#[derive(Deserialize, Serialize, Debug, Clone)]
pub struct WorkerSubtaskUpdate {
    pub id: String,
    pub description: String,
    pub status: String,
    #[serde(default)]
    pub assigned_worker: Option<String>,
    #[serde(default)]
    pub depends_on: Vec<String>,
}

// ── Bridge ────────────────────────────────────────────────────

/// Manages the local Python worker subprocess.
///
/// The worker is spawned lazily (via ``ensure_worker``) on the first task
/// submission. If the spawn fails (e.g. Python not installed),
/// ``is_available()`` returns false and all submit calls fall back to local
/// decomposition.
///
/// A background notification reader task reads the worker's stdout and
/// dispatches updates to the TaskStore and broadcast channel.
///
/// ## Crash recovery
///
/// When the notification reader detects stdout EOF (worker process died),
/// it calls the ``on_worker_dead`` callback and then attempts to restart
/// the worker with exponential backoff (up to 3 attempts). If restart
/// succeeds, the notification reader is restarted for the new process.
/// If all restart attempts fail, the bridge stays in unavailable state
/// and the server falls back to local decomposition.
pub struct LocalWorkerBridge {
    stdin: Arc<Mutex<Option<ChildStdin>>>,
    stdout: Arc<Mutex<Option<BufReader<tokio::process::ChildStdout>>>>,
    /// Tracks whether the child process is alive.
    alive: Arc<AtomicBool>,
    /// Handle to the child process (for killing on drop).
    child: Arc<Mutex<Option<Child>>>,
    /// Whether the notification reader has been started.
    reader_started: Arc<AtomicBool>,
    /// Whether a graceful shutdown is in progress (prevents auto-restart).
    shutting_down: Arc<AtomicBool>,
}

impl LocalWorkerBridge {
    /// Create a new bridge in a not-yet-spawned state.
    ///
    /// The worker process is not started until ``ensure_worker`` is called.
    /// This allows the gRPC server to be created synchronously and defer
    /// the async worker spawn to first task submission.
    pub fn new() -> Self {
        Self {
            stdin: Arc::new(Mutex::new(None)),
            stdout: Arc::new(Mutex::new(None)),
            alive: Arc::new(AtomicBool::new(false)),
            child: Arc::new(Mutex::new(None)),
            reader_started: Arc::new(AtomicBool::new(false)),
            shutting_down: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Try to spawn the local Python worker.
    ///
    /// Returns ``Ok(())`` if the process started and passed a ``ping``
    /// health check. Returns ``Err`` with a human-readable message if the
    /// worker could not be started.
    ///
    /// If the worker is already alive, this is a no-op.
    pub async fn ensure_worker(&self) -> Result<(), String> {
        self.ensure_worker_with_env(&[]).await
    }

    /// Try to spawn the local Python worker with extra environment variables.
    ///
    /// Each element of ``env`` is a ``(key, value)`` pair that is set on the
    /// child process in addition to the current environment.
    ///
    /// This is primarily useful for testing (e.g. setting ``UC_MOCK_MODE=1``
    /// to use mock decomposition without LLM).
    ///
    /// The worker module path defaults to ``ultimate_coders.local_worker``.
    /// Override with the ``UC_WORKER_MODULE`` environment variable.
    ///
    /// If the worker is already alive, this is a no-op.
    pub async fn ensure_worker_with_env(&self, env: &[(&str, &str)]) -> Result<(), String> {
        if self.alive.load(Ordering::Relaxed) {
            return Ok(());
        }

        // Allow overriding the worker module for testing
        let worker_module = std::env::var("UC_WORKER_MODULE")
            .unwrap_or_else(|_| "ultimate_coders.local_worker".to_string());

        // ponytail: allow overriding python binary (e.g. .venv/bin/python3)
        let python_bin = std::env::var("UC_WORKER_PYTHON").unwrap_or_else(|_| "python3".to_string());
        let mut cmd = Command::new(&python_bin);
        cmd.arg("-m")
            .arg(&worker_module)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit()); // logs go to stderr

        for (key, value) in env {
            cmd.env(key, value);
        }

        let mut child = cmd
            .spawn()
            .map_err(|e| format!("Failed to spawn local_worker: {}", e))?;

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "Failed to get stdin of local_worker".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "Failed to get stdout of local_worker".to_string())?;

        // Store stdin and stdout
        {
            let mut stdin_guard = self.stdin.lock().await;
            *stdin_guard = Some(stdin);
        }
        {
            let mut stdout_guard = self.stdout.lock().await;
            *stdout_guard = Some(BufReader::new(stdout));
        }

        // Store child handle
        {
            let mut child_guard = self.child.lock().await;
            *child_guard = Some(child);
        }

        // Mark as alive before health check so ping can write
        self.alive.store(true, Ordering::Relaxed);

        // Health check: send ping, expect response within 5s
        match tokio::time::timeout(std::time::Duration::from_secs(5), self.ping()).await {
            Ok(Ok(())) => {
                tracing::info!("LocalWorkerBridge: worker healthy");
                Ok(())
            }
            Ok(Err(e)) => {
                self.cleanup_on_failure().await;
                Err(format!("Worker health check failed: {}", e))
            }
            Err(_) => {
                self.cleanup_on_failure().await;
                Err("Worker health check timed out (5s)".to_string())
            }
        }
    }

    /// Clean up resources after a failed spawn or health check.
    async fn cleanup_on_failure(&self) {
        self.alive.store(false, Ordering::Relaxed);
        {
            let mut stdin_guard = self.stdin.lock().await;
            *stdin_guard = None;
        }
        {
            let mut stdout_guard = self.stdout.lock().await;
            *stdout_guard = None;
        }
        let mut child_guard = self.child.lock().await;
        if let Some(mut child) = child_guard.take() {
            let _ = child.kill().await;
        }
    }

    /// Whether the worker subprocess is alive and healthy.
    pub fn is_available(&self) -> bool {
        self.alive.load(Ordering::Relaxed)
    }

    /// Send a ping to the worker and wait for the response.
    ///
    /// This is called during ``ensure_worker`` before the notification reader
    /// is started. It reads directly from the stored stdout.
    pub async fn ping(&self) -> Result<(), String> {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id,
            method: "ping",
            params: serde_json::json!({}),
        };

        // Write request
        let mut line = serde_json::to_string(&req).map_err(|e| format!("Serialize: {}", e))?;
        line.push('\n');

        let mut stdin_guard = self.stdin.lock().await;
        let stdin = stdin_guard
            .as_mut()
            .ok_or_else(|| "Worker stdin not available".to_string())?;
        stdin
            .write_all(line.as_bytes())
            .await
            .map_err(|e| format!("Write: {}", e))?;
        stdin.flush().await.map_err(|e| format!("Flush: {}", e))?;
        drop(stdin_guard);

        // Read response from stored stdout
        let mut stdout_guard = self.stdout.lock().await;
        let reader = stdout_guard
            .as_mut()
            .ok_or_else(|| "Worker stdout not available".to_string())?;

        let mut buf = String::new();
        match reader.read_line(&mut buf).await {
            Ok(0) => {
                self.alive.store(false, Ordering::Relaxed);
                Err("Worker process exited (stdout closed)".to_string())
            }
            Ok(_) => {
                let trimmed = buf.trim();
                let resp: JsonRpcResponse =
                    serde_json::from_str(trimmed).map_err(|e| format!("Parse response: {}", e))?;
                if let Some(err) = resp.error {
                    Err(format!("ping error {}: {}", err.code, err.message))
                } else {
                    Ok(())
                }
            }
            Err(e) => {
                self.alive.store(false, Ordering::Relaxed);
                Err(format!("Read: {}", e))
            }
        }
    }

    /// Send a submit_task request to the worker without waiting for a response.
    ///
    /// The worker will process the task asynchronously, sending ``task_update``
    /// notifications and a final response via stdout. The notification reader
    /// task (started via ``start_notification_reader``) handles these.
    pub async fn send_submit_task(
        &self,
        description: &str,
        project_id: &str,
        task_id: &str,
    ) -> Result<(), String> {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id,
            method: "submit_task",
            params: serde_json::json!({
                "description": description,
                "project_id": project_id,
                "task_id": task_id,
            }),
        };

        let mut line = serde_json::to_string(&req).map_err(|e| format!("Serialize: {}", e))?;
        line.push('\n');

        let mut stdin_guard = self.stdin.lock().await;
        let stdin = stdin_guard
            .as_mut()
            .ok_or_else(|| "Worker stdin not available (worker not running)".to_string())?;
        stdin
            .write_all(line.as_bytes())
            .await
            .map_err(|e| format!("Write error: {}", e))?;
        stdin
            .flush()
            .await
            .map_err(|e| format!("Flush error: {}", e))?;

        tracing::info!(
            id,
            description = %description,
            "Sent submit_task request to local worker"
        );
        Ok(())
    }

    /// Start the background notification reader task.
    ///
    /// Spawns a tokio task that reads the worker's stdout line by line,
    /// parses JSON-RPC notifications and responses, and:
    /// - For ``task_update`` notifications: calls ``apply_fn`` with the update
    /// - For responses matching a request ID: parses as WorkerTaskUpdate and
    ///   calls ``apply_fn``
    /// - On stdout EOF: marks the worker as dead, calls ``on_worker_dead``,
    ///   then attempts auto-restart with exponential backoff (up to 3 attempts).
    ///   On successful restart, calls ``on_restart`` so the caller can restart
    ///   the notification reader for the new process.
    ///
    /// This method takes ownership of the stored stdout. It should be called
    /// exactly once after ``ensure_worker`` succeeds.
    ///
    /// **Important**: The ``apply_fn`` closure is called for each notification.
    /// It receives the ``WorkerTaskUpdate`` and is responsible for updating the
    /// TaskStore and broadcasting events.
    pub fn start_notification_reader<
        F: Fn(WorkerTaskUpdate) + Send + Sync + 'static,
        G: Fn() + Send + Sync + 'static,
        H: Fn() + Send + Sync + 'static,
    >(
        &self,
        apply_fn: F,
        on_worker_dead: G,
        on_restart: H,
    ) {
        if self.reader_started.swap(true, Ordering::Relaxed) {
            tracing::warn!("Notification reader already started, skipping");
            return;
        }

        let alive = self.alive.clone();
        let stdout = self.stdout.clone();
        let stdin = self.stdin.clone();
        let child = self.child.clone();
        let reader_started = self.reader_started.clone();
        let shutting_down = self.shutting_down.clone();

        tokio::spawn(async move {
            // Take stdout from the bridge
            let reader = {
                let mut stdout_guard = stdout.lock().await;
                stdout_guard.take()
            };

            let Some(reader) = reader else {
                tracing::warn!("Cannot start notification reader: no stdout");
                return;
            };

            tracing::info!("LocalWorkerBridge: notification reader started");
            let mut lines = reader.lines();

            loop {
                match lines.next_line().await {
                    Ok(Some(line)) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() {
                            continue;
                        }

                        // Try parsing as notification first
                        if let Ok(notif) = serde_json::from_str::<JsonRpcNotification>(trimmed) {
                            if notif.method == "task_update" {
                                if let Some(params) = notif.params {
                                    match serde_json::from_value::<WorkerTaskUpdate>(params) {
                                        Ok(update) => {
                                            tracing::debug!(
                                                task_id = %update.task_id,
                                                status = %update.status,
                                                "Received task_update notification from worker"
                                            );
                                            apply_fn(update);
                                        }
                                        Err(e) => {
                                            tracing::warn!(
                                                error = %e,
                                                "Failed to parse task_update params from worker"
                                            );
                                        }
                                    }
                                }
                            }
                            continue;
                        }

                        // Try parsing as response (for submit_task completion, ping, etc.)
                        if let Ok(resp) = serde_json::from_str::<JsonRpcResponse>(trimmed) {
                            if let Some(result) = resp.result {
                                // Check if this is a submit_task response (has task_id)
                                if result.get("task_id").and_then(|v| v.as_str()).is_some() {
                                    tracing::info!("Worker completed submit_task response");
                                    // The final response is also a task update.
                                    match serde_json::from_value::<WorkerTaskUpdate>(result) {
                                        Ok(update) => {
                                            apply_fn(update);
                                        }
                                        Err(e) => {
                                            tracing::warn!(
                                                error = %e,
                                                "Failed to parse submit_task response as WorkerTaskUpdate"
                                            );
                                        }
                                    }
                                } else if result
                                    .get("status")
                                    .is_some_and(|v| v.as_str() == Some("ok"))
                                {
                                    // ping or shutdown response
                                    tracing::debug!("Worker response received (status=ok)");
                                }
                            } else if let Some(err) = resp.error {
                                tracing::warn!(
                                    code = err.code,
                                    message = %err.message,
                                    "Worker returned error response"
                                );
                            }
                            continue;
                        }

                        // Unrecognized line
                        tracing::debug!(
                            line = %trimmed,
                            "Skipping unrecognized line from worker stdout"
                        );
                    }
                    Ok(None) => {
                        // stdout EOF — worker process exited
                        tracing::warn!("LocalWorkerBridge: worker stdout closed (process exited)");
                        alive.store(false, Ordering::Relaxed);
                        on_worker_dead();

                        // Attempt auto-restart (unless we're shutting down)
                        if shutting_down.load(Ordering::Relaxed) {
                            tracing::info!("Shutting down, not attempting auto-restart");
                            break;
                        }

                        attempt_auto_restart(
                            &alive,
                            &stdin,
                            &stdout,
                            &child,
                            &reader_started,
                            &shutting_down,
                            &on_restart,
                        )
                        .await;

                        break;
                    }
                    Err(e) => {
                        tracing::error!(error = %e, "Error reading worker stdout");
                        alive.store(false, Ordering::Relaxed);
                        on_worker_dead();

                        // Attempt auto-restart (unless we're shutting down)
                        if shutting_down.load(Ordering::Relaxed) {
                            break;
                        }

                        attempt_auto_restart(
                            &alive,
                            &stdin,
                            &stdout,
                            &child,
                            &reader_started,
                            &shutting_down,
                            &on_restart,
                        )
                        .await;

                        break;
                    }
                }
            }

            tracing::info!("LocalWorkerBridge: notification reader exiting");
        });
    }

    /// Kill the worker subprocess.
    pub async fn kill(&self) {
        self.shutting_down.store(true, Ordering::Relaxed);
        self.alive.store(false, Ordering::Relaxed);
        let mut child_guard = self.child.lock().await;
        if let Some(mut child) = child_guard.take() {
            let _ = child.kill().await;
        }
    }

    /// Restart the worker subprocess.
    ///
    /// Kills the existing process (if any), resets state, and spawns a new
    /// worker. Returns ``Ok(())`` if the new worker passes the health check.
    /// Returns ``Err`` if the restart fails.
    ///
    /// After a successful restart, the caller must call
    /// ``start_notification_reader()`` again for the new process.
    pub async fn restart(&self) -> Result<(), String> {
        tracing::info!("LocalWorkerBridge: restarting worker");

        // Kill existing process
        self.kill().await;

        // Clean up state
        {
            let mut stdin_guard = self.stdin.lock().await;
            *stdin_guard = None;
        }
        {
            let mut stdout_guard = self.stdout.lock().await;
            *stdout_guard = None;
        }

        // Reset reader_started so a new reader can be started
        self.reader_started.store(false, Ordering::Relaxed);
        self.shutting_down.store(false, Ordering::Relaxed);

        // Spawn new worker
        self.ensure_worker().await
    }

    /// Gracefully shut down the worker process.
    ///
    /// Sends a ``shutdown`` JSON-RPC method to the worker, waits up to 5
    /// seconds for it to exit, then sends SIGKILL if it hasn't exited.
    pub async fn graceful_shutdown(&self) {
        self.shutting_down.store(true, Ordering::Relaxed);

        // Send shutdown JSON-RPC request
        let shutdown_sent = self.send_shutdown().await;

        if shutdown_sent {
            // Wait for the process to exit
            let timeout = tokio::time::timeout(
                std::time::Duration::from_secs(GRACEFUL_SHUTDOWN_TIMEOUT_SECS),
                async {
                    let mut child_guard = self.child.lock().await;
                    if let Some(child) = child_guard.as_mut() {
                        let _ = child.wait().await;
                    }
                    child_guard.take();
                },
            )
            .await;

            if timeout.is_err() {
                tracing::warn!(
                    "Worker did not exit within {}s, sending SIGKILL",
                    GRACEFUL_SHUTDOWN_TIMEOUT_SECS
                );
            }
        }

        // Ensure the process is dead
        self.alive.store(false, Ordering::Relaxed);
        let mut child_guard = self.child.lock().await;
        if let Some(mut child) = child_guard.take() {
            let _ = child.kill().await;
        }
    }

    /// Send a ``shutdown`` JSON-RPC request to the worker.
    ///
    /// Returns true if the request was sent successfully, false otherwise.
    async fn send_shutdown(&self) -> bool {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id,
            method: "shutdown",
            params: serde_json::json!({}),
        };

        let mut line = match serde_json::to_string(&req) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!(error = %e, "Failed to serialize shutdown request");
                return false;
            }
        };
        line.push('\n');

        let mut stdin_guard = self.stdin.lock().await;
        if let Some(stdin) = stdin_guard.as_mut() {
            match stdin.write_all(line.as_bytes()).await {
                Ok(()) => {
                    let _ = stdin.flush().await;
                    tracing::info!("Sent shutdown request to local worker");
                    true
                }
                Err(e) => {
                    tracing::warn!(error = %e, "Failed to send shutdown request to worker");
                    false
                }
            }
        } else {
            false
        }
    }

    /// Whether a graceful shutdown is in progress.
    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::Relaxed)
    }

    /// Reset the reader_started flag so a new notification reader can be
    /// started. Called after an auto-restart or manual restart.
    pub fn reset_reader_started(&self) {
        self.reader_started.store(false, Ordering::Relaxed);
    }
}

impl Default for LocalWorkerBridge {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for LocalWorkerBridge {
    fn drop(&mut self) {
        self.alive.store(false, Ordering::Relaxed);
        self.shutting_down.store(true, Ordering::Relaxed);
    }
}

/// Attempt to auto-restart the worker after a crash.
///
/// Uses exponential backoff: 1s, 2s, 4s between attempts (max 3 attempts).
/// On success, resets the bridge state and calls ``on_restart`` so the
/// caller can start a new notification reader.
///
/// This function is called from the notification reader task when it
/// detects that the worker process has exited.
async fn attempt_auto_restart(
    alive: &Arc<AtomicBool>,
    stdin: &Arc<Mutex<Option<ChildStdin>>>,
    stdout: &Arc<Mutex<Option<BufReader<tokio::process::ChildStdout>>>>,
    child: &Arc<Mutex<Option<Child>>>,
    reader_started: &Arc<AtomicBool>,
    shutting_down: &Arc<AtomicBool>,
    on_restart: &(dyn Fn() + Send + Sync),
) {
    for attempt in 1..=MAX_RESTART_ATTEMPTS {
        if shutting_down.load(Ordering::Relaxed) {
            tracing::info!("Shutting down, aborting auto-restart");
            return;
        }

        let delay_ms = RESTART_BASE_DELAY_MS * 2u64.pow(attempt - 1);
        tracing::info!(
            attempt = attempt,
            max_attempts = MAX_RESTART_ATTEMPTS,
            delay_ms = delay_ms,
            "Attempting to restart local worker"
        );

        tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;

        if shutting_down.load(Ordering::Relaxed) {
            tracing::info!("Shutting down, aborting auto-restart after sleep");
            return;
        }

        // Clean up old state
        {
            let mut stdin_guard = stdin.lock().await;
            *stdin_guard = None;
        }
        {
            let mut stdout_guard = stdout.lock().await;
            *stdout_guard = None;
        }
        {
            let mut child_guard = child.lock().await;
            if let Some(mut old_child) = child_guard.take() {
                let _ = old_child.kill().await;
            }
        }

        // Try to spawn a new worker
        let python_bin = std::env::var("UC_WORKER_PYTHON").unwrap_or_else(|_| "python3".to_string());
        let worker_module = std::env::var("UC_WORKER_MODULE")
            .unwrap_or_else(|_| "ultimate_coders.local_worker".to_string());
        match Command::new(&python_bin)
            .arg("-m")
            .arg(&worker_module)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .spawn()
        {
            Ok(mut new_child) => {
                let new_stdin = match new_child.stdin.take() {
                    Some(s) => s,
                    None => {
                        tracing::warn!("Failed to get stdin from restarted worker");
                        let _ = new_child.kill().await;
                        continue;
                    }
                };
                let new_stdout = match new_child.stdout.take() {
                    Some(s) => s,
                    None => {
                        tracing::warn!("Failed to get stdout from restarted worker");
                        let _ = new_child.kill().await;
                        continue;
                    }
                };

                // Store new handles
                {
                    let mut stdin_guard = stdin.lock().await;
                    *stdin_guard = Some(new_stdin);
                }
                {
                    let mut stdout_guard = stdout.lock().await;
                    *stdout_guard = Some(BufReader::new(new_stdout));
                }
                {
                    let mut child_guard = child.lock().await;
                    *child_guard = Some(new_child);
                }

                // Health check: send ping
                alive.store(true, Ordering::Relaxed);

                let ping_ok = perform_health_check(stdin, stdout, alive).await;

                if ping_ok {
                    tracing::info!(attempt = attempt, "Local worker restarted successfully");
                    // Reset reader_started so a new reader can be started
                    reader_started.store(false, Ordering::Relaxed);
                    on_restart();
                    return;
                } else {
                    tracing::warn!(
                        attempt = attempt,
                        "Health check failed for restarted worker"
                    );
                    alive.store(false, Ordering::Relaxed);
                }
            }
            Err(e) => {
                tracing::warn!(
                    attempt = attempt,
                    error = %e,
                    "Failed to spawn new worker process"
                );
            }
        }
    }

    tracing::error!(
        attempts = MAX_RESTART_ATTEMPTS,
        "All auto-restart attempts failed, worker remains unavailable"
    );
}

/// Perform a health check on the worker by sending a ping and reading the response.
///
/// Returns true if the worker responded successfully within 5 seconds.
async fn perform_health_check(
    stdin: &Arc<Mutex<Option<ChildStdin>>>,
    stdout: &Arc<Mutex<Option<BufReader<tokio::process::ChildStdout>>>>,
    alive: &Arc<AtomicBool>,
) -> bool {
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let req = JsonRpcRequest {
        jsonrpc: "2.0",
        id,
        method: "ping",
        params: serde_json::json!({}),
    };
    let mut line = match serde_json::to_string(&req) {
        Ok(s) => s,
        Err(e) => {
            tracing::warn!(error = %e, "Failed to serialize ping");
            return false;
        }
    };
    line.push('\n');

    // Write ping
    {
        let mut stdin_guard = stdin.lock().await;
        if let Some(s) = stdin_guard.as_mut() {
            if let Err(e) = s.write_all(line.as_bytes()).await {
                tracing::warn!(error = %e, "Failed to write ping");
                return false;
            }
            if let Err(e) = s.flush().await {
                tracing::warn!(error = %e, "Failed to flush ping");
                return false;
            }
        } else {
            return false;
        }
    }

    // Read response with timeout
    let result = tokio::time::timeout(std::time::Duration::from_secs(5), async {
        let mut stdout_guard = stdout.lock().await;
        if let Some(reader) = stdout_guard.as_mut() {
            let mut buf = String::new();
            match reader.read_line(&mut buf).await {
                Ok(0) => {
                    alive.store(false, Ordering::Relaxed);
                    false // stdout closed
                }
                Ok(_) => {
                    let trimmed = buf.trim();
                    match serde_json::from_str::<JsonRpcResponse>(trimmed) {
                        Ok(resp) => resp.error.is_none(),
                        Err(e) => {
                            tracing::warn!(error = %e, "Failed to parse ping response");
                            false
                        }
                    }
                }
                Err(e) => {
                    alive.store(false, Ordering::Relaxed);
                    tracing::warn!(error = %e, "Failed to read ping response");
                    false
                }
            }
        } else {
            false
        }
    })
    .await;

    match result {
        Ok(true) => true,
        Ok(false) => {
            tracing::warn!("Health check failed");
            false
        }
        Err(_) => {
            tracing::warn!("Health check timed out");
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_task_update_deserialize() {
        let json = serde_json::json!({
            "task_id": "t-1",
            "description": "Fix the bug",
            "project_id": "proj-1",
            "status": "in_progress",
            "subtasks": [
                {
                    "id": "s-1",
                    "description": "Write test",
                    "status": "assigned",
                    "assigned_worker": "w-1",
                    "depends_on": []
                }
            ],
            "result": null
        });
        let update: WorkerTaskUpdate = serde_json::from_value(json).unwrap();
        assert_eq!(update.task_id, "t-1");
        assert_eq!(update.status, "in_progress");
        assert_eq!(update.subtasks.len(), 1);
        assert_eq!(update.subtasks[0].id, "s-1");
    }

    #[test]
    fn json_rpc_request_serialize() {
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id: 1,
            method: "ping",
            params: serde_json::json!({}),
        };
        let s = serde_json::to_string(&req).unwrap();
        assert!(s.contains("\"method\":\"ping\""));
        // Must be single line (no embedded newlines)
        assert!(!s.contains('\n'));
    }

    #[test]
    fn bridge_new_starts_unavailable() {
        let bridge = LocalWorkerBridge::new();
        assert!(!bridge.is_available());
    }

    #[test]
    fn bridge_default_starts_unavailable() {
        let bridge = LocalWorkerBridge::default();
        assert!(!bridge.is_available());
    }

    #[tokio::test]
    async fn send_submit_task_without_worker_fails() {
        let bridge = LocalWorkerBridge::new();
        let result = bridge.send_submit_task("test", "").await;
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("not running"));
    }

    #[test]
    fn worker_task_update_with_empty_subtasks() {
        let json = serde_json::json!({
            "task_id": "t-2",
            "description": "Simple task",
            "status": "completed",
            "result": "All done"
        });
        let update: WorkerTaskUpdate = serde_json::from_value(json).unwrap();
        assert_eq!(update.task_id, "t-2");
        assert_eq!(update.status, "completed");
        assert!(update.subtasks.is_empty());
        assert_eq!(update.result, Some("All done".to_string()));
    }

    #[test]
    fn worker_subtask_update_deserialize() {
        let json = serde_json::json!({
            "id": "s-1",
            "description": "Write test",
            "status": "in_progress",
            "assigned_worker": "w-1",
            "depends_on": ["s-0"]
        });
        let update: WorkerSubtaskUpdate = serde_json::from_value(json).unwrap();
        assert_eq!(update.id, "s-1");
        assert_eq!(update.status, "in_progress");
        assert_eq!(update.assigned_worker, Some("w-1".to_string()));
        assert_eq!(update.depends_on, vec!["s-0"]);
    }

    #[test]
    fn json_rpc_protocol_format_single_line() {
        // All JSON-RPC messages must be single-line (no embedded newlines)
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id: 42,
            method: "submit_task",
            params: serde_json::json!({
                "description": "Fix the login bug\nAlso fix the signup bug",
                "project_id": "proj-1"
            }),
        };
        let s = serde_json::to_string(&req).unwrap();
        // The serialized string itself must be a single line
        assert!(!s.contains('\n'));
        // But the description value inside can contain \n as an escape
        assert!(s.contains("Fix the login bug"));
    }

    #[tokio::test]
    async fn submit_task_fallback_without_worker() {
        // Creating a bridge without spawning should not panic
        let bridge = LocalWorkerBridge::new();
        assert!(!bridge.is_available());

        // Trying to send a task should fail gracefully
        let result = bridge.send_submit_task("test task", "").await;
        assert!(result.is_err());
    }

    #[test]
    fn shutdown_request_serializes() {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id,
            method: "shutdown",
            params: serde_json::json!({}),
        };
        let s = serde_json::to_string(&req).unwrap();
        assert!(s.contains("\"method\":\"shutdown\""));
        assert!(!s.contains('\n'));
    }

    #[test]
    fn bridge_shutting_down_flag() {
        let bridge = LocalWorkerBridge::new();
        assert!(!bridge.is_shutting_down());
        // After kill, shutting_down should be true
    }

    #[tokio::test]
    async fn graceful_shutdown_without_worker() {
        // Graceful shutdown on a bridge without a worker should not panic
        let bridge = LocalWorkerBridge::new();
        bridge.graceful_shutdown().await;
        assert!(!bridge.is_available());
        assert!(bridge.is_shutting_down());
    }

    #[tokio::test]
    async fn restart_without_worker_fails() {
        let bridge = LocalWorkerBridge::new();
        let result = bridge.restart().await;
        assert!(result.is_err());
        assert!(!bridge.is_available());
    }
}
