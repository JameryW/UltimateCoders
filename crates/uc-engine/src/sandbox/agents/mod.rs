//! Agent adapters -- translate between subtask prompts and sandbox execution.
//!
//! Each agent adapter knows how to:
//! 1. Build an `ExecRequest` for its specific coding agent
//! 2. Parse the `ExecResult` into a structured `AgentOutput`

pub mod claude_code;
pub mod codex;
pub mod grok;

use crate::sandbox::{AgentOutput, ExecRequest, ExecResult, SandboxConfig};

/// Per-subtask agent config overrides (deserialized from agent_config_json).
/// Each field overrides the corresponding `SandboxConfig` field when present.
#[derive(Debug, Clone, Default, serde::Deserialize)]
pub struct SubtaskAgentConfig {
    #[serde(default)]
    pub tools: Option<Vec<String>>,
    #[serde(default)]
    pub allowed_tools: Option<Vec<String>>,
    #[serde(default)]
    pub disallowed_tools: Option<Vec<String>>,
    #[serde(default)]
    pub mcp_configs: Option<Vec<String>>,
    #[serde(default)]
    pub append_system_prompt: Option<String>,
    #[serde(default)]
    pub agent_name: Option<String>,
    #[serde(default)]
    pub agents_json: Option<String>,
}

/// Merge `SandboxConfig` agent fields with per-subtask overrides.
/// Subtask-level values take precedence. Lists are replaced, not merged.
pub fn merge_agent_config(config: &SandboxConfig, subtask: &SubtaskAgentConfig) -> SandboxConfig {
    let mut merged = config.clone();
    if let Some(ref tools) = subtask.tools {
        merged.tools = tools.clone();
    }
    if let Some(ref allowed) = subtask.allowed_tools {
        merged.allowed_tools = allowed.clone();
    }
    if let Some(ref disallowed) = subtask.disallowed_tools {
        merged.disallowed_tools = disallowed.clone();
    }
    if let Some(ref mcp) = subtask.mcp_configs {
        merged.mcp_configs = mcp.clone();
    }
    if let Some(ref prompt) = subtask.append_system_prompt {
        merged.append_system_prompt = Some(prompt.clone());
    }
    if let Some(ref name) = subtask.agent_name {
        merged.agent_name = Some(name.clone());
    }
    if let Some(ref json) = subtask.agents_json {
        merged.agents_json = Some(json.clone());
    }
    merged
}

/// Adapter trait for coding agents (Grok Build, Claude Code, Codex, etc.).
///
/// Each implementation knows how to construct the right command-line
/// invocation for its agent and how to parse the output.
pub trait AgentAdapter: Send + Sync {
    /// Name of this agent adapter (e.g., "grok-build", "claude-code", "codex").
    fn name(&self) -> &str;

    /// Build an execution request for the given prompt.
    ///
    /// `subtask_config` provides per-subtask agent overrides (JSON).
    /// When present, these override the corresponding `SandboxConfig` fields.
    fn build_request(
        &self,
        prompt: &str,
        working_dir: &str,
        config: &SandboxConfig,
        subtask_config: Option<&serde_json::Value>,
    ) -> ExecRequest;

    /// Parse the execution result into a structured agent output.
    fn parse_output(&self, result: &ExecResult) -> AgentOutput;
}

/// Create a default agent adapter by name.
///
/// Returns `None` if the agent name is not recognized.
pub fn create_adapter(name: &str) -> Option<Box<dyn AgentAdapter>> {
    match name {
        "grok-build" | "grok" => Some(Box::new(grok::GrokBuildAgent::new())),
        "claude-code" => Some(Box::new(claude_code::ClaudeCodeAgent::new())),
        "codex" => Some(Box::new(codex::CodexAgent::new())),
        _ => None,
    }
}

/// List available agent adapter names.
pub fn available_agents() -> Vec<&'static str> {
    vec!["grok-build", "grok", "claude-code", "codex"]
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
    fn create_adapter_grok_build_and_alias() {
        let adapter = create_adapter("grok-build");
        assert!(adapter.is_some());
        assert_eq!(adapter.unwrap().name(), "grok-build");

        let alias = create_adapter("grok");
        assert!(alias.is_some());
        assert_eq!(alias.unwrap().name(), "grok-build");
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
        assert!(agents.contains(&"grok-build"));
        assert!(agents.contains(&"grok"));
        assert!(agents.contains(&"claude-code"));
        assert!(agents.contains(&"codex"));
    }
}
