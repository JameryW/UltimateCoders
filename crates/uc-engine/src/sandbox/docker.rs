//! Docker-based sandbox -- runs agent commands in Docker containers.
//!
//! Provides full isolation: filesystem, network, PID namespace.
//! Resource limits via Docker cgroups.
//!
//! This implementation uses the Docker CLI (`docker run`) rather than
//! the Docker API (bollard) to keep dependencies minimal.

use super::{
    EngineError, ExecRequest, ExecResult, NetworkMode, Sandbox, SandboxConfig, SandboxHandle,
    SandboxHealth, SandboxStatus,
};
use async_trait::async_trait;
use std::process::Stdio;
use std::time::Instant;
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::time::{timeout, Duration};

/// Default Docker image for the sandbox.
const DEFAULT_SANDBOX_IMAGE: &str = "ultimate-coders/sandbox:latest";

/// Docker-based sandbox that runs agent commands in containers.
///
/// Provides full filesystem, network, and PID namespace isolation.
/// Resource limits are enforced via Docker cgroups.
pub struct DockerSandbox {
    /// Docker image to use for sandbox containers.
    image: String,
    /// Whether Docker daemon is available.
    docker_available: bool,
    /// Default configuration for sandbox creation.
    default_config: SandboxConfig,
}

impl DockerSandbox {
    /// Create a new DockerSandbox with the default image.
    pub fn new() -> Self {
        let docker_available = Self::check_docker_available();
        Self {
            image: DEFAULT_SANDBOX_IMAGE.to_string(),
            docker_available,
            default_config: SandboxConfig::default(),
        }
    }

    /// Create a new DockerSandbox with a custom image.
    pub fn with_image(image: impl Into<String>) -> Self {
        let docker_available = Self::check_docker_available();
        Self {
            image: image.into(),
            docker_available,
            default_config: SandboxConfig::default(),
        }
    }

    /// Create a new DockerSandbox with a custom image and config.
    pub fn with_config(image: impl Into<String>, config: SandboxConfig) -> Self {
        let docker_available = Self::check_docker_available();
        Self {
            image: image.into(),
            docker_available,
            default_config: config,
        }
    }

    /// Check if the Docker daemon is available.
    ///
    /// Note: This uses a synchronous `docker info` call. It should only
    /// be called during initialization, not from an async context, as it
    /// may block for a few seconds if the Docker daemon is slow to respond.
    fn check_docker_available() -> bool {
        // Quick synchronous check -- run `docker info` and see if it succeeds
        std::process::Command::new("docker")
            .arg("info")
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    /// Whether Docker is available on this system.
    pub fn is_available(&self) -> bool {
        self.docker_available
    }

    /// Build the network mode flag for Docker.
    ///
    /// Note: Docker does not have a "restricted" network mode.
    /// For `Restricted`, we use `bridge` (default Docker networking)
    /// which provides NAT-based isolation without host network access.
    fn network_flag(mode: &NetworkMode) -> &'static str {
        match mode {
            NetworkMode::None => "--network=none",
            NetworkMode::Restricted => "--network=bridge",
            NetworkMode::Full => "--network=host",
        }
    }
}

impl Default for DockerSandbox {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl Sandbox for DockerSandbox {
    async fn create(&self, config: &SandboxConfig) -> Result<SandboxHandle, EngineError> {
        if !self.docker_available {
            return Err(EngineError::SandboxError(
                "Docker daemon is not available".to_string(),
            ));
        }

        if config.project_path.is_empty() {
            return Err(EngineError::SandboxError(
                "project_path is required for Docker sandbox".to_string(),
            ));
        }

        // For Docker, we don't pre-create a container here.
        // Instead, we create a handle and the container is created
        // on-demand during execute(). This simplifies pool management
        // and avoids stale containers.
        let handle = SandboxHandle {
            id: uuid::Uuid::new_v4().to_string(),
            status: SandboxStatus::Ready,
            created_at: chrono::Utc::now().timestamp(),
        };

        tracing::info!(
            sandbox_id = %handle.id,
            image = %self.image,
            project_path = %config.project_path,
            "Created Docker sandbox handle"
        );

        Ok(handle)
    }

    async fn execute(
        &self,
        handle: &SandboxHandle,
        request: ExecRequest,
    ) -> Result<ExecResult, EngineError> {
        if !self.docker_available {
            return Err(EngineError::SandboxError(
                "Docker daemon is not available".to_string(),
            ));
        }

        if handle.status == SandboxStatus::Stopped {
            return Err(EngineError::SandboxError(format!(
                "Sandbox {} is stopped",
                handle.id
            )));
        }

        let start = Instant::now();
        // timeout_secs == 0 means no timeout (unbounded). Guard against the
        // default (0) which would otherwise expire immediately.
        let timeout_duration = if request.timeout_secs == 0 {
            None
        } else {
            Some(Duration::from_secs(request.timeout_secs))
        };

        let config = &self.default_config;
        let working_dir = if request.working_dir.is_empty() {
            &config.project_path
        } else {
            &request.working_dir
        };

        // Build the docker run command
        let mut docker_args = vec![
            "run".to_string(),
            "--rm".to_string(), // Remove container after execution
            format!("--name=uc-sandbox-{}", handle.id),
        ];

        // Volume mount for project directory (host_path:container_path)
        docker_args.push(format!("-v={}:/workspace", working_dir));

        // Working directory inside container
        docker_args.push("-w=/workspace".to_string());

        // Environment variables
        for (key, value) in &request.env_vars {
            docker_args.push(format!("-e={}={}", key, value));
        }

        // Resource limits
        docker_args.push(format!(
            "--memory={}m",
            config.resource_limits.max_memory_mb
        ));
        docker_args.push("--cpus=1".to_string());

        // Network mode
        docker_args.push(Self::network_flag(&config.network).to_string());

        // Image
        docker_args.push(self.image.clone());

        // Command and args
        docker_args.push(request.command.clone());
        for arg in &request.args {
            docker_args.push(arg.clone());
        }

        tracing::info!(
            sandbox_id = %handle.id,
            image = %self.image,
            command = %request.command,
            "Executing command in Docker sandbox"
        );

        // Run docker
        let mut cmd = Command::new("docker");
        cmd.args(&docker_args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);

        // Spawn the docker process
        let mut child = cmd
            .spawn()
            .map_err(|e| EngineError::SandboxError(format!("Failed to spawn docker: {}", e)))?;

        // Wait with optional timeout. Read stdout+stderr concurrently via
        // tokio::join! before child.wait() — serial reads deadlock when the
        // child writes >64KB to one pipe while the other fills.
        let inner = async {
            let mut stdout_buf = Vec::new();
            let mut stderr_buf = Vec::new();

            let stdout_handle = child.stdout.take();
            let stderr_handle = child.stderr.take();

            let (stdout_result, stderr_result) = tokio::join!(
                async {
                    match stdout_handle {
                        Some(mut stdout) => stdout.read_to_end(&mut stdout_buf).await,
                        None => Ok(0),
                    }
                },
                async {
                    match stderr_handle {
                        Some(mut stderr) => stderr.read_to_end(&mut stderr_buf).await,
                        None => Ok(0),
                    }
                },
            );

            let status = child.wait().await;

            stdout_result
                .map_err(|e| EngineError::SandboxError(format!("stdout read error: {}", e)))?;
            stderr_result
                .map_err(|e| EngineError::SandboxError(format!("stderr read error: {}", e)))?;

            let exit_status = status.map_err(|e| {
                EngineError::SandboxError(format!("docker process wait error: {}", e))
            })?;

            let exit_code = exit_status.code().unwrap_or(-1);

            Ok::<(i32, Vec<u8>, Vec<u8>), EngineError>((exit_code, stdout_buf, stderr_buf))
        };

        // Apply timeout only when configured; 0 means unbounded.
        let result: Result<Result<(i32, Vec<u8>, Vec<u8>), EngineError>, EngineError> =
            match timeout_duration {
                Some(d) => timeout(d, inner)
                    .await
                    .map_err(|_| EngineError::SandboxError("docker command timed out".into())),
                None => Ok(inner.await),
            };

        let duration_ms = start.elapsed().as_millis() as u64;

        match result {
            Ok(Ok((exit_code, stdout_bytes, stderr_bytes))) => {
                let max_output = config.resource_limits.max_output_bytes as usize;
                let stdout = super::subprocess::truncate_output(&stdout_bytes, max_output);
                let stderr = super::subprocess::truncate_output(&stderr_bytes, max_output);

                tracing::info!(
                    sandbox_id = %handle.id,
                    exit_code,
                    duration_ms,
                    "Docker command completed"
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
                let _ = child.kill().await;
                let _ = child.wait().await;

                tracing::warn!(
                    sandbox_id = %handle.id,
                    "Docker command timed out"
                );

                Ok(ExecResult {
                    exit_code: -1,
                    stdout: String::new(),
                    stderr: "Docker command timed out".to_string(),
                    duration_ms,
                    timed_out: true,
                })
            }
        }
    }

    async fn stop(&self, handle: &SandboxHandle) -> Result<(), EngineError> {
        // Try to stop and remove the container
        let container_name = format!("uc-sandbox-{}", handle.id);

        // docker stop
        let _ = Command::new("docker")
            .args(["stop", &container_name])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;

        // docker rm (in case --rm didn't clean up)
        let _ = Command::new("docker")
            .args(["rm", "-f", &container_name])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;

        tracing::info!(
            sandbox_id = %handle.id,
            "Stopped Docker sandbox container"
        );

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

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config() -> SandboxConfig {
        SandboxConfig {
            project_path: "/tmp/test-project".to_string(),
            working_dir: "/tmp/test-project".to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn docker_sandbox_network_flag() {
        assert_eq!(
            DockerSandbox::network_flag(&NetworkMode::None),
            "--network=none"
        );
        assert_eq!(
            DockerSandbox::network_flag(&NetworkMode::Restricted),
            "--network=bridge"
        );
        assert_eq!(
            DockerSandbox::network_flag(&NetworkMode::Full),
            "--network=host"
        );
    }

    #[test]
    fn docker_sandbox_default_image() {
        let sandbox = DockerSandbox::new();
        assert_eq!(sandbox.image, DEFAULT_SANDBOX_IMAGE);
    }

    #[test]
    fn docker_sandbox_custom_image() {
        let sandbox = DockerSandbox::with_image("custom:latest");
        assert_eq!(sandbox.image, "custom:latest");
    }

    #[tokio::test]
    async fn docker_sandbox_create_without_docker() {
        let sandbox = DockerSandbox {
            image: DEFAULT_SANDBOX_IMAGE.to_string(),
            docker_available: false,
            default_config: SandboxConfig::default(),
        };
        let config = test_config();
        let result = sandbox.create(&config).await;
        assert!(result.is_err());
        match result.unwrap_err() {
            EngineError::SandboxError(msg) => assert!(msg.contains("not available")),
            other => panic!("Expected SandboxError, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn docker_sandbox_create_empty_path() {
        let sandbox = DockerSandbox {
            image: DEFAULT_SANDBOX_IMAGE.to_string(),
            docker_available: true,
            default_config: SandboxConfig::default(),
        };
        let config = SandboxConfig {
            project_path: String::new(),
            ..Default::default()
        };
        let result = sandbox.create(&config).await;
        assert!(result.is_err());
    }
}
