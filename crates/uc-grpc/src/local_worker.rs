//! Local worker bridge — manages a Python subprocess for task execution.
//!
//! Spawns ``python -m ultimate_coders.local_worker`` as a long-running child
//! process and communicates via JSON-RPC 2.0 over stdin/stdout (newline-delimited).
//!
//! When the worker is unavailable (Python not installed, extension not built),
//! the bridge reports ``is_available() == false`` and the gRPC server falls
//! back to local (newline-split) decomposition.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
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
    id: Option<u64>,
    result: Option<Value>,
    error: Option<JsonRpcError>,
}

#[derive(Deserialize, Debug)]
struct JsonRpcNotification {
    method: String,
    params: Option<Value>,
}

#[derive(Deserialize, Debug)]
struct JsonRpcError {
    code: i64,
    message: String,
}

/// Task update from the worker (maps to ``task_update`` notification params).
#[derive(Deserialize, Debug, Clone)]
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
#[derive(Deserialize, Debug, Clone)]
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
/// The worker is spawned on construction. If the spawn fails (e.g. Python
/// not installed), ``is_available()`` returns false and all submit calls
/// fall back to local decomposition.
pub struct LocalWorkerBridge {
    stdin: Arc<Mutex<ChildStdin>>,
    stdout: Arc<Mutex<BufReader<ChildStdout>>>,
    /// Tracks whether the child process is alive.
    alive: Arc<AtomicBool>,
    /// Handle to the child process (for killing on drop).
    child: Arc<Mutex<Child>>,
}

impl LocalWorkerBridge {
    /// Try to spawn the local Python worker.
    ///
    /// Returns ``Ok(bridge)`` if the process started and passed a ``ping``
    /// health check. Returns ``Err`` with a human-readable message if the
    /// worker could not be started.
    pub async fn spawn() -> Result<Self, String> {
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

        let bridge = Self {
            stdin: Arc::new(Mutex::new(stdin)),
            stdout: Arc::new(Mutex::new(BufReader::new(stdout))),
            alive: Arc::new(AtomicBool::new(true)),
            child: Arc::new(Mutex::new(child)),
        };

        // Health check: send ping, expect response within 5s
        match tokio::time::timeout(std::time::Duration::from_secs(5), bridge.ping()).await {
            Ok(Ok(())) => {
                tracing::info!("LocalWorkerBridge: worker healthy");
                Ok(bridge)
            }
            Ok(Err(e)) => Err(format!("Worker health check failed: {}", e)),
            Err(_) => Err("Worker health check timed out (5s)".to_string()),
        }
    }

    /// Whether the worker subprocess is alive and healthy.
    pub fn is_available(&self) -> bool {
        self.alive.load(Ordering::Relaxed)
    }

    /// Send a ping to the worker and wait for the response.
    pub async fn ping(&self) -> Result<(), String> {
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let req = JsonRpcRequest {
            jsonrpc: "2.0",
            id,
            method: "ping",
            params: serde_json::json!({}),
        };

        let resp = self.send_and_read(req).await?;
        if let Some(err) = resp.error {
            return Err(format!("ping error {}: {}", err.code, err.message));
        }
        Ok(())
    }

    /// Submit a task to the worker.
    ///
    /// Returns the final task state after execution, and a list of
    /// intermediate notifications (task_update) received during execution.
    pub async fn submit_task(
        &self,
        description: &str,
        project_id: &str,
    ) -> Result<(WorkerTaskUpdate, Vec<WorkerTaskUpdate>), String> {
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

        let mut notifications = Vec::new();

        // Read lines until we get the response with our ID
        {
            let mut stdin = self.stdin.lock().await;
            let mut stdout = self.stdout.lock().await;

            // Send request
            let mut line = serde_json::to_string(&req).map_err(|e| format!("Serialize error: {}", e))?;
            line.push('\n');
            stdin
                .write_all(line.as_bytes())
                .await
                .map_err(|e| format!("Write error: {}", e))?;
            stdin
                .flush()
                .await
                .map_err(|e| format!("Flush error: {}", e))?;

            // Read response (may interleave with notifications)
            loop {
                let mut buf = String::new();
                match stdout.read_line(&mut buf).await {
                    Ok(0) => {
                        self.alive.store(false, Ordering::Relaxed);
                        return Err("Worker process exited (stdout closed)".to_string());
                    }
                    Ok(_) => {}
                    Err(e) => {
                        self.alive.store(false, Ordering::Relaxed);
                        return Err(format!("Read error: {}", e));
                    }
                }

                let trimmed = buf.trim();
                if trimmed.is_empty() {
                    continue;
                }

                // Try parsing as response first
                if let Ok(resp) = serde_json::from_str::<JsonRpcResponse>(trimmed) {
                    if resp.id == Some(id) {
                        // This is our response
                        if let Some(err) = resp.error {
                            return Err(format!("Worker error {}: {}", err.code, err.message));
                        }
                        let result = resp.result.ok_or_else(|| "Missing result field".to_string())?;
                        let update: WorkerTaskUpdate =
                            serde_json::from_value(result).map_err(|e| format!("Parse result: {}", e))?;
                        return Ok((update, notifications));
                    }
                    // Response for a different ID — skip (shouldn't happen in sequential mode)
                    continue;
                }

                // Try parsing as notification
                if let Ok(notif) = serde_json::from_str::<JsonRpcNotification>(trimmed) {
                    if notif.method == "task_update" {
                        if let Some(params) = notif.params {
                            if let Ok(update) = serde_json::from_value::<WorkerTaskUpdate>(params) {
                                notifications.push(update);
                            }
                        }
                    }
                }
            }
        }
    }

    /// Kill the worker subprocess.
    pub async fn kill(&self) {
        self.alive.store(false, Ordering::Relaxed);
        let mut child = self.child.lock().await;
        let _ = child.kill().await;
    }

    async fn send_and_read(&self, req: JsonRpcRequest) -> Result<JsonRpcResponse, String> {
        let mut stdin = self.stdin.lock().await;
        let mut stdout = self.stdout.lock().await;

        let mut line = serde_json::to_string(&req).map_err(|e| format!("Serialize: {}", e))?;
        line.push('\n');
        stdin
            .write_all(line.as_bytes())
            .await
            .map_err(|e| format!("Write: {}", e))?;
        stdin
            .flush()
            .await
            .map_err(|e| format!("Flush: {}", e))?;

        // Read one line
        let mut buf = String::new();
        stdout
            .read_line(&mut buf)
            .await
            .map_err(|e| format!("Read: {}", e))?;

        serde_json::from_str(buf.trim()).map_err(|e| format!("Parse response: {}", e))
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
    }
}
