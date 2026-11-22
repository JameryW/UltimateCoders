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
        // timeout_secs == 0 means no timeout (unbounded). Guard against the
        // default (0) which would otherwise expire immediately and kill the
        // process before it runs.
        let timeout_duration = if request.timeout_secs == 0 {
            None
        } else {
            Some(Duration::from_secs(request.timeout_secs))
        };

        // Build the command
        let mut cmd = Command::new(&request.command);
        cmd.args(&request.args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if request.stdin.is_some() {
            cmd.stdin(Stdio::piped());
        }

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

        // On Linux the opt-in cgroup guard is created BEFORE spawn (so it drops
        // AFTER `child` is reaped) and attaches the child inside `pre_exec`
        // before execve so no grandchild can fork before migration.
        #[cfg(target_os = "linux")]
        let mut cgroup_guard = match super::cgroup::CgroupGuard::create("sandbox") {
            Ok(guard) if guard.is_active() => {
                let procs_path = guard.path().join("cgroup.procs");
                // SAFETY: `pre_exec` runs between fork and exec in the child
                // process. Writing `"0"` to `cgroup.procs` via std::fs or
                // raw open/write only accesses a pre-allocated path; failures
                // are non-fatal because the parent still attempts attach.
                unsafe {
                    cmd.pre_exec(move || {
                        let _ = std::fs::write(&procs_path, b"0");
                        Ok(())
                    });
                }
                Some(guard)
            }
            Ok(_) => None,
            Err(error) => {
                tracing::warn!(%error, "could not create subprocess cgroup guard");
                None
            }
        };

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

        // A Job Object guarantees that a Windows timeout terminates the whole
        // command tree, not just a shell wrapper such as `cmd.exe /C`.
        #[cfg(windows)]
        let process_job = match WindowsProcessJob::assign(&child) {
            Ok(job) => Some(job),
            Err(error) => {
                tracing::warn!(%error, "could not assign subprocess to a Windows Job Object");
                None
            }
        };

        // Best-effort parent-side attach in case pre_exec could not migrate.
        #[cfg(target_os = "linux")]
        if let Some(guard) = &cgroup_guard {
            if let Some(pid) = child.id() {
                if let Err(error) = guard.attach(pid) {
                    tracing::debug!(%error, "parent-side cgroup attach skipped or failed");
                }
            }
        }

        // Cap in-flight output buffering at 50 MB + 1 byte so a chatty process
        // cannot OOM the gateway before exit or timeout.
        const MAX_OUTPUT_BYTES: usize = 50 * 1024 * 1024;
        const READ_LIMIT: u64 = (MAX_OUTPUT_BYTES + 1) as u64;

        // Drain both pipes in background tasks BEFORE writing stdin and while
        // waiting for the process so large stdin/stdout exchanges never deadlock.
        let mut stdout_task = child.stdout.take().map(|stdout| {
            tokio::spawn(async move {
                let mut bytes = Vec::new();
                let mut reader = stdout.take(READ_LIMIT);
                reader.read_to_end(&mut bytes).await?;
                // Drain any remaining bytes without storing them so the child
                // never blocks on a full pipe before exit.
                let mut sink = tokio::io::sink();
                let _ = tokio::io::copy(&mut reader.into_inner(), &mut sink).await;
                Ok::<Vec<u8>, std::io::Error>(bytes)
            })
        });
        let mut stderr_task = child.stderr.take().map(|stderr| {
            tokio::spawn(async move {
                let mut bytes = Vec::new();
                let mut reader = stderr.take(READ_LIMIT);
                reader.read_to_end(&mut bytes).await?;
                let mut sink = tokio::io::sink();
                let _ = tokio::io::copy(&mut reader.into_inner(), &mut sink).await;
                Ok::<Vec<u8>, std::io::Error>(bytes)
            })
        });

        // Optionally provide stdin
        if let Some(stdin_data) = &request.stdin {
            if let Some(mut stdin_pipe) = child.stdin.take() {
                use tokio::io::AsyncWriteExt;
                let _ = stdin_pipe.write_all(stdin_data.as_bytes()).await;
                let _ = stdin_pipe.shutdown().await;
            }
        }

        // Apply the timeout to the process wait only. Descendant processes may
        // inherit stdout/stderr; timing out a pipe read would otherwise wait
        // for those descendants to exit even after the requested command has
        // been terminated.
        let status = match timeout_duration {
            Some(duration) => timeout(duration, child.wait()).await,
            None => Ok(child.wait().await),
        };

        let duration_ms = start.elapsed().as_millis() as u64;

        match status {
            Ok(Ok(exit_status)) => {
                // If an active cgroup holds stray descendants keeping stdout/stderr
                // open after the direct child exited, terminate them once the
                // direct child has finished so `collect_output` cannot hang.
                #[cfg(target_os = "linux")]
                if let Some(guard) = cgroup_guard.as_mut() {
                    let _ = guard.cleanup();
                }
                let stdout_bytes = collect_output(stdout_task.take(), "stdout").await?;
                let stderr_bytes = collect_output(stderr_task.take(), "stderr").await?;
                let exit_code = exit_status.code().unwrap_or(-1);

                let stdout = truncate_output(&stdout_bytes, MAX_OUTPUT_BYTES);
                let stderr = truncate_output(&stderr_bytes, MAX_OUTPUT_BYTES);

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
            Ok(Err(e)) => Err(EngineError::SandboxError(format!(
                "process wait error: {}",
                e
            ))),
            Err(_) => {
                // Timeout -- kill the process and, on Windows/Linux, its descendants.
                #[cfg(windows)]
                if let Some(job) = &process_job {
                    job.terminate();
                } else if let Some(pid) = child.id() {
                    Self::kill_process_tree(pid).await;
                }
                #[cfg(target_os = "linux")]
                if let Some(guard) = &cgroup_guard {
                    if let Err(error) = guard.kill() {
                        tracing::warn!(%error, "could not kill subprocess cgroup");
                    }
                }
                let _ = child.start_kill();
                // Bounded reap before cleaning up the cgroup so the kernel can
                // remove the directory instead of failing with EBUSY.
                let _ = timeout(Duration::from_millis(500), child.wait()).await;
                #[cfg(target_os = "linux")]
                if let Some(guard) = cgroup_guard.as_mut() {
                    let _ = guard.cleanup();
                }
                if let Some(task) = stdout_task.take() {
                    task.abort();
                }
                if let Some(task) = stderr_task.take() {
                    task.abort();
                }

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
    /// Terminate a Windows process tree so descendants cannot outlive a timed
    /// out shell command while retaining its stdout/stderr pipe handles.
    #[cfg(windows)]
    async fn kill_process_tree(pid: u32) {
        let mut taskkill = match Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            // Allow taskkill to finish cleaning descendants after this timeout
            // path has returned; it must never delay the sandbox response.
            .kill_on_drop(false)
            .spawn()
        {
            Ok(taskkill) => taskkill,
            Err(_) => return,
        };

        let _ = timeout(Duration::from_millis(500), taskkill.wait()).await;
    }

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

#[cfg(windows)]
struct WindowsProcessJob {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

// A Job Object handle is an owned kernel handle. This wrapper never exposes
// the raw handle or permits mutation without a shared reference.
#[cfg(windows)]
unsafe impl Send for WindowsProcessJob {}

#[cfg(windows)]
impl WindowsProcessJob {
    fn assign(child: &tokio::process::Child) -> std::io::Result<Self> {
        use std::mem::size_of;
        use std::ptr::null;
        use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        let handle = unsafe { CreateJobObjectW(null(), null()) };
        if handle.is_null() {
            return Err(std::io::Error::last_os_error());
        }

        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            unsafe { CloseHandle(handle) };
            return Err(std::io::Error::last_os_error());
        }

        let Some(child_handle) = child.raw_handle() else {
            unsafe { CloseHandle(handle) };
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "child process handle unavailable",
            ));
        };
        let assigned = unsafe { AssignProcessToJobObject(handle, child_handle as HANDLE) };
        if assigned == 0 {
            unsafe { CloseHandle(handle) };
            return Err(std::io::Error::last_os_error());
        }

        Ok(Self { handle })
    }

    fn terminate(&self) {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;

        unsafe { TerminateJobObject(self.handle, 1) };
    }
}

#[cfg(windows)]
impl Drop for WindowsProcessJob {
    fn drop(&mut self) {
        use windows_sys::Win32::Foundation::CloseHandle;

        unsafe { CloseHandle(self.handle) };
    }
}

async fn collect_output(
    task: Option<tokio::task::JoinHandle<Result<Vec<u8>, std::io::Error>>>,
    stream_name: &str,
) -> Result<Vec<u8>, EngineError> {
    match task {
        Some(task) => task
            .await
            .map_err(|e| EngineError::SandboxError(format!("{stream_name} read task failed: {e}")))?
            .map_err(|e| EngineError::SandboxError(format!("{stream_name} read error: {e}"))),
        None => Ok(Vec::new()),
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
    use super::super::test_shell_command;
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

        let (command, args) =
            test_shell_command("printf '%s\\n' 'hello world'", "echo hello world");
        let request = ExecRequest {
            command,
            args,
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

        let (command, args) =
            test_shell_command("printf '%s\\n' \"$UC_TEST_VAR\"", "echo %UC_TEST_VAR%");
        let request = ExecRequest {
            command,
            args,
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

        let (command, args) = test_shell_command("sleep 60", "ping -n 61 127.0.0.1 > NUL");
        let request = ExecRequest {
            command,
            args,
            stdin: None,
            timeout_secs: 1,
            working_dir: String::new(),
            env_vars: HashMap::new(),
        };

        let result = sandbox.execute(&handle, request).await.unwrap();
        assert!(result.timed_out);
        assert_ne!(result.exit_code, 0);
        assert!(
            result.duration_ms < 5_000,
            "timeout returned after {} ms",
            result.duration_ms
        );
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

        let (command, args) = test_shell_command("false", "exit /B 1");
        let request = ExecRequest {
            command,
            args,
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

    // Regression: timeout_secs == 0 (the default) used to create
    // Duration::from_secs(0) → immediate timeout, killing the process
    // before it ran. 0 now means unbounded.
    #[tokio::test]
    async fn subprocess_sandbox_execute_timeout_zero_is_unbounded() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        let (command, args) = test_shell_command("printf '%s\\n' survived", "echo survived");
        let request = ExecRequest {
            command,
            args,
            stdin: None,
            timeout_secs: 0, // unbounded
            working_dir: String::new(),
            env_vars: HashMap::new(),
        };

        let result = sandbox.execute(&handle, request).await.unwrap();
        assert!(!result.timed_out, "timeout_secs=0 must not time out");
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("survived"));
    }

    // Regression: stdout+stderr were read serially before child.wait(),
    // deadlocking when the child writes >64KB to one pipe while the other
    // fills. Concurrent reads (tokio::join!) drain both. This writes a
    // large amount to BOTH stdout and stderr — under the old serial read
    // it would hang until the test timeout.
    #[tokio::test]
    async fn subprocess_sandbox_large_concurrent_stdout_stderr_no_deadlock() {
        let sandbox = SubprocessSandbox::new();
        let config = test_config();
        let handle = sandbox.create(&config).await.unwrap();

        // Print ~200KB to stdout AND ~200KB to stderr concurrently. Under the
        // old serial read this hangs until the test timeout because the child
        // blocks writing to one pipe while the other fills.
        let (command, args) = {
            #[cfg(windows)]
            {
                (
                    "powershell.exe".to_string(),
                    vec![
                        "-NoProfile".to_string(),
                        "-Command".to_string(),
                        "$out = 'x' * 64; $err = 'y' * 64; 1..4000 | ForEach-Object { [Console]::Out.Write($out); [Console]::Error.Write($err) }".to_string(),
                    ],
                )
            }

            #[cfg(not(windows))]
            {
                let script = r#"
            i=0
            while [ $i -lt 4000 ]; do
              printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
              printf 'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy' 1>&2
              i=$((i+1))
            done
        "#;
                test_shell_command(script, "")
            }
        };
        let request = ExecRequest {
            command,
            args,
            stdin: None,
            timeout_secs: 30,
            working_dir: String::new(),
            env_vars: HashMap::new(),
        };

        let result = sandbox.execute(&handle, request).await.unwrap();
        assert!(
            !result.timed_out,
            "should not deadlock on large dual-pipe output"
        );
        assert_eq!(result.exit_code, 0);
        // ~200KB each (allowing for shell variance).
        assert!(
            result.stdout.len() > 100_000,
            "stdout len: {}",
            result.stdout.len()
        );
        assert!(
            result.stderr.len() > 100_000,
            "stderr len: {}",
            result.stderr.len()
        );
    }
}
