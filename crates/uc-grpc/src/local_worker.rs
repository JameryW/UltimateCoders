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

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

/// JSON-RPC request ID counter (shared across all calls).
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

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
pub struct LocalWorkerBridge {
    stdin: Arc<Mutex<Option<ChildStdin>>>,
    stdout: Arc<Mutex<Option<BufReader<tokio::process::ChildStdout>>>>,
    /// Tracks whether the child process is alive.
    alive: Arc<AtomicBool>,
    /// Handle to the child process (for killing on drop).
    child: Arc<Mutex<Option<Child>>>,
    /// Whether the notification reader has been started.
    reader_started: Arc<AtomicBool>,
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
        if self.alive.load(Ordering::Relaxed) {
            return Ok(());
        }

        let mut child = Command::new("python3")
            .arg("-m")
            .arg("ultimate_coders.local_worker")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit()) // logs go to stderr
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
    ) -> Result<(), String> {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id,
            method: "submit_task",
            params: serde_json::json!({
                "description": description,
                "project_id": project_id,
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
    /// - On stdout EOF: marks the worker as dead and calls ``on_worker_dead``
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
    >(
        &self,
        apply_fn: F,
        on_worker_dead: G,
    ) {
        if self.reader_started.swap(true, Ordering::Relaxed) {
            tracing::warn!("Notification reader already started, skipping");
            return;
        }

        let alive = self.alive.clone();
        let stdout = self.stdout.clone();

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
                                    .map_or(false, |v| v.as_str() == Some("ok"))
                                {
                                    // ping response — already handled during ensure_worker
                                    tracing::debug!("Worker ping response received (in reader)");
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
                        break;
                    }
                    Err(e) => {
                        tracing::error!(error = %e, "Error reading worker stdout");
                        alive.store(false, Ordering::Relaxed);
                        on_worker_dead();
                        break;
                    }
                }
            }

            tracing::info!("LocalWorkerBridge: notification reader exiting");
        });
    }

    /// Kill the worker subprocess.
    pub async fn kill(&self) {
        self.alive.store(false, Ordering::Relaxed);
        let mut child_guard = self.child.lock().await;
        if let Some(mut child) = child_guard.take() {
            let _ = child.kill().await;
        }
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
}
