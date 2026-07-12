//! Sandbox execution environment for coding agents.
//!
//! Provides isolated execution of Claude Code, Codex, and other coding agents
//! with resource limits and file change tracking.
//!
//! # Architecture
//!
//! ```text
//! Worker
//!   ├── execution_mode="llm"      → LLM tool-calling loop (existing)
//!   └── execution_mode="sandbox"  → SandboxManager
//!                                       │
//!                                 ┌─────┴─────┐
//!                                 │  Sandbox   │  (trait)
//!                                 │  Pool      │
//!                                 └─────┬─────┘
//!                                       │
//!                                       │
//!                               SubprocessSandbox
//!                               (MVP, production)
//! ```

pub mod agents;
pub mod file_tracker;
pub mod pool;
pub mod subprocess;

// Re-export agent adapter types for convenience
pub use agents::claude_code::ClaudeCodeAgent;
pub use agents::codex::CodexAgent;
pub use agents::{available_agents, create_adapter, AgentAdapter};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use uc_types::EngineError;

// ── Sandbox Trait ──────────────────────────────────────────────

/// Sandbox execution trait -- abstracts over isolation backends.
///
/// Implementations provide different levels of isolation:
/// - `SubprocessSandbox`: No isolation, runs as child process (MVP, production)
#[async_trait]
pub trait Sandbox: Send + Sync {
    /// Create a new sandbox environment.
    async fn create(&self, config: &SandboxConfig) -> Result<SandboxHandle, EngineError>;

    /// Execute a command in the sandbox.
    async fn execute(
        &self,
        handle: &SandboxHandle,
        request: ExecRequest,
    ) -> Result<ExecResult, EngineError>;

    /// Stop and clean up a sandbox.
    async fn stop(&self, handle: &SandboxHandle) -> Result<(), EngineError>;

    /// Check sandbox health.
    async fn health(&self, handle: &SandboxHandle) -> Result<SandboxHealth, EngineError>;
}

// ── Configuration Types ────────────────────────────────────────

/// Configuration for creating a sandbox.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SandboxConfig {
    /// Path to the project directory to mount/expose.
    pub project_path: String,
    /// Environment variables to set in the sandbox.
    pub env_vars: HashMap<String, String>,
    /// Resource limits for execution.
    pub resource_limits: ResourceLimits,
    /// Network access mode.
    pub network: NetworkMode,
    /// Working directory inside the sandbox.
    pub working_dir: String,
    // Agent customization (passed as claude CLI flags)
    /// Tool list for --tools flag (e.g. ["default", "mcp__codegraph__*"]).
    #[serde(default)]
    pub tools: Vec<String>,
    /// Allowed tool patterns for --allowedTools.
    #[serde(default)]
    pub allowed_tools: Vec<String>,
    /// Disallowed tool patterns for --disallowedTools.
    #[serde(default)]
    pub disallowed_tools: Vec<String>,
    /// MCP server config file paths for --mcp-config.
    #[serde(default)]
    pub mcp_configs: Vec<String>,
    /// Extra system prompt for --append-system-prompt.
    #[serde(default)]
    pub append_system_prompt: Option<String>,
    /// Custom agent name for --agent.
    #[serde(default)]
    pub agent_name: Option<String>,
    /// JSON string defining custom agents for --agents.
    #[serde(default)]
    pub agents_json: Option<String>,
}

impl Default for SandboxConfig {
    fn default() -> Self {
        Self {
            project_path: String::new(),
            env_vars: HashMap::new(),
            resource_limits: ResourceLimits::default(),
            network: NetworkMode::Full,
            working_dir: String::new(),
            tools: Vec::new(),
            allowed_tools: Vec::new(),
            disallowed_tools: Vec::new(),
            mcp_configs: Vec::new(),
            append_system_prompt: None,
            agent_name: None,
            agents_json: None,
        }
    }
}

/// Resource limits for sandbox execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResourceLimits {
    /// Maximum CPU time in seconds (default: 3600 = 1 hour).
    pub max_cpu_seconds: u64,
    /// Maximum memory in MB (default: 8192 = 8 GB).
    pub max_memory_mb: u64,
    /// Maximum output size in bytes (default: 50 MB).
    pub max_output_bytes: u64,
    /// Maximum file size in MB (default: 500 MB).
    pub max_file_size_mb: u64,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        Self {
            max_cpu_seconds: 3600,
            max_memory_mb: 8192,
            max_output_bytes: 50 * 1024 * 1024,
            max_file_size_mb: 500,
        }
    }
}

/// Network access mode.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub enum NetworkMode {
    /// No network access.
    None,
    /// Only specific hosts allowed (API endpoints).
    Restricted,
    /// Unrestricted network access — allows all commands and connections.
    #[default]
    Full,
}

// ── Sandbox Handle ─────────────────────────────────────────────

/// Handle to a sandbox instance.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SandboxHandle {
    /// Unique identifier for this sandbox instance.
    pub id: String,
    /// Current status of the sandbox.
    pub status: SandboxStatus,
    /// Unix timestamp when the sandbox was created.
    pub created_at: i64,
}

/// Status of a sandbox instance.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum SandboxStatus {
    /// Sandbox is starting up.
    Starting,
    /// Sandbox is ready to accept commands.
    Ready,
    /// Sandbox is currently executing a command.
    Busy,
    /// Sandbox has been stopped.
    Stopped,
    /// Sandbox encountered an error.
    Failed,
}

// ── Execution Types ────────────────────────────────────────────

/// Execution request sent to a sandbox.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ExecRequest {
    /// Command to execute (e.g., "claude", "codex").
    pub command: String,
    /// Command-line arguments.
    pub args: Vec<String>,
    /// Optional stdin content.
    pub stdin: Option<String>,
    /// Timeout in seconds for this execution.
    pub timeout_secs: u64,
    /// Working directory for the command.
    pub working_dir: String,
    /// Additional environment variables for this command.
    pub env_vars: HashMap<String, String>,
}

impl ExecRequest {
    /// Create a new execution request with the given command and arguments.
    pub fn new(command: impl Into<String>, args: Vec<String>) -> Self {
        Self {
            command: command.into(),
            args,
            stdin: None,
            timeout_secs: 300,
            working_dir: String::new(),
            env_vars: HashMap::new(),
        }
    }
}

/// Result of executing a command in a sandbox.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecResult {
    /// Process exit code (0 = success).
    pub exit_code: i32,
    /// Captured standard output.
    pub stdout: String,
    /// Captured standard error.
    pub stderr: String,
    /// Wall-clock duration in milliseconds.
    pub duration_ms: u64,
    /// Whether the execution timed out.
    pub timed_out: bool,
}

impl ExecResult {
    /// Whether the execution succeeded (exit code 0 and not timed out).
    pub fn is_success(&self) -> bool {
        self.exit_code == 0 && !self.timed_out
    }
}

/// Sandbox health status.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SandboxHealth {
    /// Sandbox identifier.
    pub id: String,
    /// Current status.
    pub status: SandboxStatus,
    /// Uptime in seconds.
    pub uptime_seconds: u64,
}

// ── Agent Output Types ─────────────────────────────────────────

/// Output from an agent adapter after parsing execution results.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentOutput {
    /// Summary of what the agent did.
    pub summary: String,
    /// File changes produced by the agent.
    pub file_changes: Vec<uc_types::FileChange>,
    /// Token usage information (if available).
    pub token_usage: Option<TokenUsage>,
    /// Whether the agent execution succeeded.
    pub success: bool,
}

/// Token usage from an LLM API call.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenUsage {
    /// Input tokens consumed.
    pub input_tokens: u64,
    /// Output tokens produced.
    pub output_tokens: u64,
    /// Total cost in USD (if available).
    pub total_cost_usd: Option<f64>,
}

// ── Shared Utility Functions ──────────────────────────────────────

/// Truncate a string to a maximum character boundary, respecting UTF-8.
///
/// This is used by agent adapters to limit summary/error message lengths.
pub(crate) fn truncate_str(s: &str, max_len: usize) -> &str {
    if s.len() <= max_len {
        s
    } else {
        // Find a good break point near max_len that doesn't split a char
        let end = s
            .char_indices()
            .take_while(|(i, _)| *i < max_len)
            .last()
            .map(|(i, c)| i + c.len_utf8())
            .unwrap_or(max_len.min(s.len()));
        &s[..end]
    }
}

// ── Tests ──────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sandbox_config_default() {
        let config = SandboxConfig::default();
        assert!(config.project_path.is_empty());
        assert!(config.env_vars.is_empty());
        assert_eq!(config.network, NetworkMode::Full);
        assert_eq!(config.resource_limits.max_cpu_seconds, 3600);
        assert_eq!(config.resource_limits.max_memory_mb, 8192);
        assert_eq!(config.resource_limits.max_output_bytes, 50 * 1024 * 1024);
        assert_eq!(config.resource_limits.max_file_size_mb, 500);
    }

    #[test]
    fn resource_limits_default() {
        let limits = ResourceLimits::default();
        assert_eq!(limits.max_cpu_seconds, 3600);
        assert_eq!(limits.max_memory_mb, 8192);
    }

    #[test]
    fn exec_request_new() {
        let req = ExecRequest::new("echo", vec!["hello".to_string()]);
        assert_eq!(req.command, "echo");
        assert_eq!(req.args, vec!["hello"]);
        assert!(req.stdin.is_none());
        assert_eq!(req.timeout_secs, 300);
        assert!(req.working_dir.is_empty());
        assert!(req.env_vars.is_empty());
    }

    #[test]
    fn exec_result_is_success() {
        let result = ExecResult {
            exit_code: 0,
            stdout: "ok".to_string(),
            stderr: String::new(),
            duration_ms: 100,
            timed_out: false,
        };
        assert!(result.is_success());

        let failed = ExecResult {
            exit_code: 1,
            stdout: String::new(),
            stderr: "error".to_string(),
            duration_ms: 50,
            timed_out: false,
        };
        assert!(!failed.is_success());

        let timed_out = ExecResult {
            exit_code: 0,
            stdout: String::new(),
            stderr: String::new(),
            duration_ms: 30000,
            timed_out: true,
        };
        assert!(!timed_out.is_success());
    }

    #[test]
    fn sandbox_config_serialization() {
        let config = SandboxConfig {
            project_path: "/tmp/project".to_string(),
            env_vars: HashMap::from([("KEY".to_string(), "VALUE".to_string())]),
            network: NetworkMode::None,
            working_dir: "/workspace".to_string(),
            ..Default::default()
        };

        let json = serde_json::to_string(&config).unwrap();
        let deserialized: SandboxConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(deserialized.project_path, "/tmp/project");
        assert_eq!(deserialized.network, NetworkMode::None);
        assert_eq!(deserialized.env_vars.get("KEY").unwrap(), "VALUE");
    }

    #[test]
    fn network_mode_default() {
        assert_eq!(NetworkMode::default(), NetworkMode::Full);
    }

    #[test]
    fn agent_output_success() {
        let output = AgentOutput {
            summary: "Fixed the bug".to_string(),
            file_changes: vec![],
            token_usage: Some(TokenUsage {
                input_tokens: 1000,
                output_tokens: 500,
                total_cost_usd: Some(0.05),
            }),
            success: true,
        };
        assert!(output.success);
        assert_eq!(output.token_usage.as_ref().unwrap().input_tokens, 1000);
    }

    #[test]
    fn truncate_str_short() {
        assert_eq!(truncate_str("hello", 10), "hello");
    }

    #[test]
    fn truncate_str_long() {
        let long = "a".repeat(200);
        let truncated = truncate_str(&long, 100);
        assert!(truncated.len() <= 100);
        assert_eq!(truncated.len(), 100);
    }

    #[test]
    fn truncate_str_multibyte() {
        // Test with multibyte UTF-8 characters
        let s = "hello world";
        let truncated = truncate_str(s, 5);
        assert_eq!(truncated, "hello");
    }
}
