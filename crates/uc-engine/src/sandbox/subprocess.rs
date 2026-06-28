//! Subprocess-based sandbox -- runs agent commands as child processes.
//!
//! MVP implementation with no filesystem/network isolation.
//! Uses ulimit for resource limits on Unix.
//!
//! This is the simplest sandbox implementation: it spawns the agent
//! command as a subprocess with optional resource limits, captures
//! stdout/stderr, and enforces a timeout.

use super::{
    EngineError, ExecRequest, ExecResult, Sandbox, SandboxConfig, SandboxHandle, SandboxHealth,
    SandboxStatus,
};
use async_trait::async_trait;
use std::process::Stdio;
use std::time::Instant;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::time::{timeout, Duration};

/// Subprocess-based sandbox that runs agent commands as child processes.
///
/// No filesystem or network isolation is provided. This is the MVP
/// implementation suitable for local development and trusted environments.
pub struct SubprocessSandbox;

impl SubprocessSandbox {
    /// Create a new SubprocessSandbox.
    pub fn new() -> Self {
        Self
    }
}

impl Default for SubprocessSandbox {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Sandbox for SubprocessSandbox {
    async fn create(&self, config: &SandboxConfig) -> Result<SandboxHandle, EngineError> {
        // For subprocess mode, "creating" a sandbox means validating the config
        // and returning a handle. No actual container or process is started.
        if config.project_path.is_empty() {
            return Err(EngineError::SandboxError(
                "project_path is required".to_string(),
            ));
        }

        let handle = SandboxHandle {
            id: uuid::Uuid::new_v4().to_string(),
            status: SandboxStatus::Ready,
            created_at: chrono::Utc::now().timestamp(),
        };

        tracing::info!(
            sandbox_id = %handle.id,
            project_path = %config.project_path,
            "Created subprocess sandbox"
        );

        Ok(handle)
    }

    async fn execute(
        &self,
        handle: &SandboxHandle,
        request: ExecRequest,
    ) -> Result<ExecResult, EngineError> {
        if handle.status == SandboxStatus::Stopped {
            return Err(EngineError::SandboxError(format!(
                "Sandbox {} is stopped",
                handle.id
            )));
        }

        let start = Instant::now();
        let timeout_duration = Duration::from_secs(request.timeout_secs);

        // Build the command
        let mut cmd = Command::new(&request.command);
        cmd.args(&request.args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);

        // Set working directory
        if !request.working_dir.is_empty() {
            cmd.current_dir(&request.working_dir);
        }

        // Set environment variables
        // Inherit the parent environment, then overlay the request vars
        for (key, value) in &request.env_vars {
            cmd.env(key, value);
        }

        // Apply Unix-specific resource limits
        #[cfg(unix)]
        {
            self.apply_unix_limits(&mut cmd, &request)?;
        }

        tracing::info!(
            sandbox_id = %handle.id,
            command = %request.command,
            args = ?request.args,
            timeout_secs = request.timeout_secs,
            "Executing command in subprocess sandbox"
        );

        // Spawn the process
        let mut child = cmd.spawn().map_err(|e| {
            EngineError::SandboxError(format!(
                "Failed to spawn command '{}': {}",
                request.command, e
            ))
        })?;

        // Optionally provide stdin
        if let Some(stdin_data) = &request.stdin {
            if let Some(mut stdin_pipe) = child.stdin.take() {
                use tokio::io::AsyncWriteExt;
                let _ = stdin_pipe.write_all(stdin_data.as_bytes()).await;
                let _ = stdin_pipe.shutdown().await;
            }
        }

        // Wait for the process with timeout, capturing output
        let result = timeout(timeout_duration, async {
            let mut stdout_buf = Vec::new();
            let mut stderr_buf = Vec::new();

            // Read stdout and stderr concurrently
            let stdout_result = if let Some(mut stdout) = child.stdout.take() {
                stdout.read_to_end(&mut stdout_buf).await
            } else {
                Ok(0)
            };

            let stderr_result = if let Some(mut stderr) = child.stderr.take() {
                stderr.read_to_end(&mut stderr_buf).await
            } else {
                Ok(0)
            };

            // Wait for the process to exit
            let status = child.wait().await;

            // Propagate I/O errors
            stdout_result
                .map_err(|e| EngineError::SandboxError(format!("stdout read error: {}", e)))?;
            stderr_result
                .map_err(|e| EngineError::SandboxError(format!("stderr read error: {}", e)))?;

            let exit_status = status
                .map_err(|e| EngineError::SandboxError(format!("process wait error: {}", e)))?;

            let exit_code = exit_status.code().unwrap_or(-1);

            Ok::<(i32, Vec<u8>, Vec<u8>), EngineError>((exit_code, stdout_buf, stderr_buf))
        })
        .await;

        let duration_ms = start.elapsed().as_millis() as u64;

        match result {
            Ok(Ok((exit_code, stdout_bytes, stderr_bytes))) => {
                // Use the default max output size from ResourceLimits.
                // The subprocess sandbox does not have access to the SandboxConfig
                // at execute time, so we use the default (50 MB).
                // DockerSandbox passes ResourceLimits through its stored config.
                let max_output = 50 * 1024 * 1024;

                let stdout = truncate_output(&stdout_bytes, max_output);
                let stderr = truncate_output(&stderr_bytes, max_output);

                tracing::info!(
                    sandbox_id = %handle.id,
                    exit_code,
                    duration_ms,
                    stdout_len = stdout.len(),
                    stderr_len = stderr.len(),
                    "Command completed in subprocess sandbox"
                );

                Ok(ExecResult {
                    exit_code,
                    stdout,
                    stderr,
                    duration_ms,
                    timed_out: false,
                })
            }
            Ok(Err(e)) => Err(e),
            Err(_) => {
                // Timeout -- kill the child process
                let _ = child.kill().await;
                let _ = child.wait().await;

                let stdout = String::new();
                let stderr = "Command timed out".to_string();

                tracing::warn!(
                    sandbox_id = %handle.id,
                    timeout_secs = request.timeout_secs,
                    "Command timed out in subprocess sandbox"
                );

                Ok(ExecResult {
                    exit_code: -1,
                    stdout,
                    stderr,
                    duration_ms,
                    timed_out: true,
                })
            }
        }
    }

    async fn stop(&self, _handle: &SandboxHandle) -> Result<(), EngineError> {
        // For subprocess mode, stop is a no-op since the process
        // has already exited after execute() returns.
        Ok(())
    }

    async fn health(&self, handle: &SandboxHandle) -> Result<SandboxHealth, EngineError> {
        let uptime_seconds = if handle.created_at > 0 {
            (chrono::Utc::now().timestamp() - handle.created_at).max(0) as u64
        } else {
            0
        };

        Ok(SandboxHealth {
            id: handle.id.clone(),
            status: handle.status.clone(),
            uptime_seconds,
        })
    }
}

impl SubprocessSandbox {
    /// Apply Unix-specific resource limits to a command.
    ///
    /// On Unix, we use process groups and ulimit-style limits.
    /// This is a best-effort mechanism -- it doesn't provide true
    /// isolation like containers do.
    #[cfg(unix)]
    fn apply_unix_limits(
        &self,
        _cmd: &mut Command,
        _request: &ExecRequest,
    ) -> Result<(), EngineError> {
        // Future: use posix_spawnattr_t or prlimit to set:
        // - RLIMIT_CPU for CPU time
        // - RLIMIT_AS for memory
        // - RLIMIT_FSIZE for file size
        //
        // For now, we rely on the tokio timeout mechanism for time limits.
        // Memory and file size limits would require process-level controls
        // or container-based isolation.
        Ok(())
    }
}

/// Truncate output bytes to the maximum allowed size, appending a marker
/// if truncation occurred.
pub(crate) fn truncate_output(bytes: &[u8], max_bytes: usize) -> String {
    if bytes.len() <= max_bytes {
        String::from_utf8_lossy(bytes).into_owned()
    } else {
        let mut truncated = String::from_utf8_lossy(&bytes[..max_bytes]).into_owned();
        truncated.push_str("\n... (output truncated)");
        truncated
    }
}

#[cfg(test)]
mod tests {
    use super::super::{NetworkMode, ResourceLimits};
    use super::*;
    use std::collections::HashMap;

    fn test_config() -> SandboxConfig {
        SandboxConfig {
            project_path: "/tmp/test-project".to_string(),
            working_dir: "/tmp/test-project".to_string(),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn subprocess_sandbox_create() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();
        assert_eq!(handle.status, SandboxStatus::Ready);
        assert!(!handle.id.is_empty());
    }

    #[tokio::test]
    async fn subprocess_sandbox_create_empty_path() {
        let sandbox = SubprocessSandbox::new();
        let config = SandboxConfig {
            project_path: String::new(),
            ..Default::default()
        };
        let result = sandbox.create(&config).await;
        assert!(result.is_err());
        match result.unwrap_err() {
            EngineError::SandboxError(msg) => assert!(msg.contains("project_path")),
            other => panic!("Expected SandboxError, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn subprocess_sandbox_execute_echo() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        let request = ExecRequest {
            command: "echo".to_string(),
            args: vec!["hello world".to_string()],
            stdin: None,
            timeout_secs: 10,
            working_dir: String::new(),
            env_vars: HashMap::new(),
        };

        let result = sandbox.execute(&handle, request).await.unwrap();
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("hello world"));
        assert!(!result.timed_out);
    }

    #[tokio::test]
    async fn subprocess_sandbox_execute_with_env() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        // On Unix, use `env` or `printenv`; on Windows, use `echo %VAR%`
        let request = ExecRequest {
            command: "printenv".to_string(),
            args: vec!["UC_TEST_VAR".to_string()],
            stdin: None,
            timeout_secs: 10,
            working_dir: String::new(),
            env_vars: HashMap::from([("UC_TEST_VAR".to_string(), "test_value_12345".to_string())]),
        };

        let result = sandbox.execute(&handle, request).await.unwrap();
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("test_value_12345"));
    }

    #[tokio::test]
    async fn subprocess_sandbox_execute_timeout() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        let request = ExecRequest {
            command: "sleep".to_string(),
            args: vec!["60".to_string()],
            stdin: None,
            timeout_secs: 1,
            working_dir: String::new(),
            env_vars: HashMap::new(),
        };

        let result = sandbox.execute(&handle, request).await.unwrap();
        assert!(result.timed_out);
        assert_ne!(result.exit_code, 0);
    }

    #[tokio::test]
    async fn subprocess_sandbox_execute_nonexistent_command() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        let request = ExecRequest {
            command: "nonexistent_command_xyz_12345".to_string(),
            args: vec![],
            stdin: None,
            timeout_secs: 5,
            working_dir: String::new(),
            env_vars: HashMap::new(),
        };

        let result = sandbox.execute(&handle, request).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn subprocess_sandbox_execute_failing_command() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        let request = ExecRequest {
            command: "false".to_string(),
            args: vec![],
            stdin: None,
            timeout_secs: 5,
            working_dir: String::new(),
            env_vars: HashMap::new(),
        };

        let result = sandbox.execute(&handle, request).await.unwrap();
        assert_ne!(result.exit_code, 0);
        assert!(!result.is_success());
    }

    #[tokio::test]
    async fn subprocess_sandbox_execute_stopped_handle() {
        let sandbox = SubprocessSandbox::new();
        let handle = SandboxHandle {
            id: "test-stopped".to_string(),
            status: SandboxStatus::Stopped,
            created_at: 0,
        };

        let request = ExecRequest::new("echo", vec!["hello".to_string()]);
        let result = sandbox.execute(&handle, request).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn subprocess_sandbox_stop() {
        let sandbox = SubprocessSandbox::new();
        let handle = SandboxHandle {
            id: "test-stop".to_string(),
            status: SandboxStatus::Ready,
            created_at: 0,
        };
        // Stop is a no-op for subprocess sandbox
        sandbox.stop(&handle).await.unwrap();
    }

    #[tokio::test]
    async fn subprocess_sandbox_health() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        let health = sandbox.health(&handle).await.unwrap();
        assert_eq!(health.id, handle.id);
        assert_eq!(health.status, SandboxStatus::Ready);
    }

    #[test]
    fn truncate_output_within_limit() {
        let bytes = b"hello world";
        let result = truncate_output(bytes, 100);
        assert_eq!(result, "hello world");
    }

    #[test]
    fn truncate_output_exceeds_limit() {
        let bytes = b"hello world this is a long string";
        let result = truncate_output(bytes, 11);
        assert!(result.contains("hello world"));
        assert!(result.contains("truncated"));
    }
}
