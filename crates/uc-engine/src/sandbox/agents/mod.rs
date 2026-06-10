//! Agent adapters -- translate between subtask prompts and sandbox execution.
//!
//! Each agent adapter knows how to:
//! 1. Build an `ExecRequest` for its specific coding agent
//! 2. Parse the `ExecResult` into a structured `AgentOutput`

pub mod claude_code;
pub mod codex;

use crate::sandbox::{ExecRequest, ExecResult, AgentOutput, SandboxConfig};

/// Adapter trait for coding agents (Claude Code, Codex, etc.).
///
/// Each implementation knows how to construct the right command-line
/// invocation for its agent and how to parse the output.
pub trait AgentAdapter: Send + Sync {
    /// Name of this agent adapter (e.g., "claude-code", "codex").
    fn name(&self) -> &str;

    /// Build an execution request for the given prompt.
    fn build_request(
        &self,
        prompt: &str,
        working_dir: &str,
        config: &SandboxConfig,
    ) -> ExecRequest;

    /// Parse the execution result into a structured agent output.
    fn parse_output(&self, result: &ExecResult) -> AgentOutput;
}

/// Create a default agent adapter by name.
///
/// Returns `None` if the agent name is not recognized.
pub fn create_adapter(name: &str) -> Option<Box<dyn AgentAdapter>> {
    match name {
        "claude-code" => Some(Box::new(claude_code::ClaudeCodeAgent::new())),
        "codex" => Some(Box::new(codex::CodexAgent::new())),
        _ => None,
    }
}

/// List available agent adapter names.
pub fn available_agents() -> Vec<&'static str> {
    vec!["claude-code", "codex"]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn create_adapter_claude_code() {
        let adapter = create_adapter("claude-code");
        assert!(adapter.is_some());
        assert_eq!(adapter.unwrap().name(), "claude-code");
    }

    #[test]
    fn create_adapter_codex() {
        let adapter = create_adapter("codex");
        assert!(adapter.is_some());
        assert_eq!(adapter.unwrap().name(), "codex");
    }

    #[test]
    fn create_adapter_unknown() {
        let adapter = create_adapter("unknown-agent");
        assert!(adapter.is_none());
    }

    #[test]
    fn available_agents_includes_known() {
        let agents = available_agents();
        assert!(agents.contains(&"claude-code"));
        assert!(agents.contains(&"codex"));
    }
}
