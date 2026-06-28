//! Claude Code agent adapter.
//!
//! Wraps the Claude Code CLI (`claude`) for sandbox execution.
//!
//! Command: `claude -p "{prompt}" --output-format json --max-turns 20`
//! Output: JSON with conversation, tool uses, cost
//! API key: `ANTHROPIC_API_KEY` env var

use crate::sandbox::{
    truncate_str, AgentAdapter, AgentOutput, ExecRequest, ExecResult, SandboxConfig, TokenUsage,
};
use uc_types::{ChangeType, FileChange};

/// Default maximum number of conversation turns.
const DEFAULT_MAX_TURNS: u32 = 20;

/// Agent adapter for Claude Code CLI.
pub struct ClaudeCodeAgent {
    /// Maximum conversation turns.
    max_turns: u32,
}

impl ClaudeCodeAgent {
    /// Create a new Claude Code adapter with default settings.
    pub fn new() -> Self {
        Self {
            max_turns: DEFAULT_MAX_TURNS,
        }
    }

    /// Create a new Claude Code adapter with custom max turns.
    pub fn with_max_turns(max_turns: u32) -> Self {
        Self { max_turns }
    }
}

impl Default for ClaudeCodeAgent {
    fn default() -> Self {
        Self::new()
    }
}

impl AgentAdapter for ClaudeCodeAgent {
    fn name(&self) -> &str {
        "claude-code"
    }

    fn build_request(
        &self,
        prompt: &str,
        working_dir: &str,
        config: &SandboxConfig,
        subtask_config: Option<&serde_json::Value>,
    ) -> ExecRequest {
        use super::{merge_agent_config, SubtaskAgentConfig};

        // Merge config-level and subtask-level overrides
        let effective_config = if let Some(sc) = subtask_config {
            if let Ok(parsed) = serde_json::from_value::<SubtaskAgentConfig>(sc.clone()) {
                merge_agent_config(config, &parsed)
            } else {
                config.clone()
            }
        } else {
            config.clone()
        };

        let mut env_vars = effective_config.env_vars.clone();

        // Ensure ANTHROPIC_API_KEY is available
        if !env_vars.contains_key("ANTHROPIC_API_KEY") {
            if let Ok(key) = std::env::var("ANTHROPIC_API_KEY") {
                env_vars.insert("ANTHROPIC_API_KEY".to_string(), key);
            }
        }

        let mut args = vec![
            "-p".to_string(),
            prompt.to_string(),
            "--output-format".to_string(),
            "json".to_string(),
            "--max-turns".to_string(),
            self.max_turns.to_string(),
            "--dangerously-skip-permissions".to_string(),
        ];

        // Agent customization flags (mirrors Python ClaudeCodeAdapter)
        if !effective_config.tools.is_empty() {
            args.push("--tools".to_string());
            args.extend(effective_config.tools.iter().cloned());
        }
        if !effective_config.allowed_tools.is_empty() {
            args.push("--allowedTools".to_string());
            args.extend(effective_config.allowed_tools.iter().cloned());
        }
        if !effective_config.disallowed_tools.is_empty() {
            args.push("--disallowedTools".to_string());
            args.extend(effective_config.disallowed_tools.iter().cloned());
        }
        if !effective_config.mcp_configs.is_empty() {
            args.push("--mcp-config".to_string());
            args.extend(effective_config.mcp_configs.iter().cloned());
        }
        if let Some(ref prompt) = effective_config.append_system_prompt {
            args.push("--append-system-prompt".to_string());
            args.push(prompt.clone());
        }
        if let Some(ref name) = effective_config.agent_name {
            args.push("--agent".to_string());
            args.push(name.clone());
        }
        if let Some(ref json) = effective_config.agents_json {
            args.push("--agents".to_string());
            args.push(json.clone());
        }

        ExecRequest {
            command: "claude".to_string(),
            args,
            stdin: None,
            timeout_secs: effective_config.resource_limits.max_cpu_seconds,
            working_dir: if working_dir.is_empty() {
                effective_config.working_dir.clone()
            } else {
                working_dir.to_string()
            },
            env_vars,
        }
    }

    fn parse_output(&self, result: &ExecResult) -> AgentOutput {
        if result.timed_out {
            return AgentOutput {
                summary: "Claude Code execution timed out".to_string(),
                file_changes: vec![],
                token_usage: None,
                success: false,
            };
        }

        if result.exit_code != 0 {
            return AgentOutput {
                summary: format!(
                    "Claude Code exited with code {}: {}",
                    result.exit_code,
                    truncate_str(&result.stderr, 200)
                ),
                file_changes: vec![],
                token_usage: None,
                success: false,
            };
        }

        // Try to parse JSON output from Claude Code
        let output = result.stdout.trim();

        if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(output) {
            // Extract summary from the JSON response
            let summary = extract_summary(&parsed);

            // Extract token usage if available
            let token_usage = extract_token_usage(&parsed);

            // Extract file changes from tool use log
            let file_changes = extract_file_changes(&parsed);

            AgentOutput {
                summary,
                file_changes,
                token_usage,
                success: true,
            }
        } else {
            // If not JSON, treat the entire output as the summary
            AgentOutput {
                summary: truncate_str(output, 1000).to_string(),
                file_changes: vec![],
                token_usage: None,
                success: true,
            }
        }
    }
}

/// Extract a summary from the Claude Code JSON output.
fn extract_summary(parsed: &serde_json::Value) -> String {
    // Claude Code JSON output structure varies. Try common fields.
    if let Some(result) = parsed.get("result").and_then(|r| r.as_str()) {
        return truncate_str(result, 500).to_string();
    }

    if let Some(message) = parsed.get("message").and_then(|m| m.as_str()) {
        return truncate_str(message, 500).to_string();
    }

    // Fallback: use the last assistant message
    if let Some(messages) = parsed.get("messages").and_then(|m| m.as_array()) {
        for msg in messages.iter().rev() {
            if msg.get("role").and_then(|r| r.as_str()) == Some("assistant") {
                if let Some(content) = msg.get("content").and_then(|c| c.as_str()) {
                    return truncate_str(content, 500).to_string();
                }
                // Content might be an array of blocks
                if let Some(blocks) = msg.get("content").and_then(|c| c.as_array()) {
                    let text_parts: Vec<&str> = blocks
                        .iter()
                        .filter_map(|b| b.get("text").and_then(|t| t.as_str()))
                        .collect();
                    if !text_parts.is_empty() {
                        return truncate_str(&text_parts.join(" "), 500).to_string();
                    }
                }
            }
        }
    }

    "Claude Code completed (output parsing limited)".to_string()
}

/// Extract token usage from Claude Code JSON output.
fn extract_token_usage(parsed: &serde_json::Value) -> Option<TokenUsage> {
    let usage = parsed.get("usage")?;

    Some(TokenUsage {
        input_tokens: usage
            .get("input_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or(0),
        output_tokens: usage
            .get("output_tokens")
            .and_then(|v| v.as_u64())
            .unwrap_or(0),
        total_cost_usd: usage.get("total_cost_usd").and_then(|v| v.as_f64()),
    })
}

/// Extract file changes from Claude Code JSON output.
///
/// Parses tool use entries for file write/edit operations.
fn extract_file_changes(parsed: &serde_json::Value) -> Vec<FileChange> {
    let mut changes = Vec::new();

    // Look for tool_use messages in the conversation
    if let Some(messages) = parsed.get("messages").and_then(|m| m.as_array()) {
        for msg in messages {
            if msg.get("role").and_then(|r| r.as_str()) != Some("assistant") {
                continue;
            }

            if let Some(blocks) = msg.get("content").and_then(|c| c.as_array()) {
                for block in blocks {
                    if block.get("type").and_then(|t| t.as_str()) != Some("tool_use") {
                        continue;
                    }

                    let tool_name = block.get("name").and_then(|n| n.as_str()).unwrap_or("");
                    let input = block.get("input").unwrap_or(&serde_json::Value::Null);

                    match tool_name {
                        "write_file" | "create_file" => {
                            if let Some(path) = input.get("file_path").and_then(|p| p.as_str()) {
                                changes.push(FileChange {
                                    file_path: path.to_string(),
                                    change_type: ChangeType::Created,
                                    diff: String::new(),
                                });
                            }
                        }
                        "edit_file" | "replace_in_file" => {
                            if let Some(path) = input.get("file_path").and_then(|p| p.as_str()) {
                                changes.push(FileChange {
                                    file_path: path.to_string(),
                                    change_type: ChangeType::Modified,
                                    diff: String::new(),
                                });
                            }
                        }
                        "delete_file" => {
                            if let Some(path) = input.get("file_path").and_then(|p| p.as_str()) {
                                changes.push(FileChange {
                                    file_path: path.to_string(),
                                    change_type: ChangeType::Deleted,
                                    diff: String::new(),
                                });
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
    }

    changes
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sandbox::{NetworkMode, ResourceLimits};
    use std::collections::HashMap;

    fn test_config() -> SandboxConfig {
        SandboxConfig {
            project_path: "/tmp/test".to_string(),
            env_vars: HashMap::new(),
            resource_limits: ResourceLimits::default(),
            network: NetworkMode::Restricted,
            working_dir: "/tmp/test".to_string(),
            tools: Vec::new(),
            allowed_tools: Vec::new(),
            disallowed_tools: Vec::new(),
            mcp_configs: Vec::new(),
            append_system_prompt: None,
            agent_name: None,
            agents_json: None,
        }
    }

    #[test]
    fn claude_code_adapter_name() {
        let adapter = ClaudeCodeAgent::new();
        assert_eq!(adapter.name(), "claude-code");
    }

    #[test]
    fn claude_code_build_request() {
        let adapter = ClaudeCodeAgent::new();
        let config = test_config();
        let request = adapter.build_request("Fix the bug", "/tmp/test", &config, None);

        assert_eq!(request.command, "claude");
        assert!(request.args.contains(&"-p".to_string()));
        assert!(request.args.contains(&"Fix the bug".to_string()));
        assert!(request.args.contains(&"--output-format".to_string()));
        assert!(request.args.contains(&"json".to_string()));
        assert!(request.args.contains(&"--max-turns".to_string()));
        assert!(request.args.contains(&"20".to_string()));
        assert!(request
            .args
            .contains(&"--dangerously-skip-permissions".to_string()));
        assert_eq!(request.working_dir, "/tmp/test");
    }

    #[test]
    fn claude_code_build_request_custom_turns() {
        let adapter = ClaudeCodeAgent::with_max_turns(10);
        let config = test_config();
        let request = adapter.build_request("Test", "/tmp/test", &config, None);
        assert!(request.args.contains(&"10".to_string()));
    }

    #[test]
    fn claude_code_parse_output_success() {
        let adapter = ClaudeCodeAgent::new();
        let result = ExecResult {
            exit_code: 0,
            stdout: r#"{"result": "Fixed the authentication bug"}"#.to_string(),
            stderr: String::new(),
            duration_ms: 5000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(output.success);
        assert!(output.summary.contains("Fixed the authentication bug"));
    }

    #[test]
    fn claude_code_parse_output_timeout() {
        let adapter = ClaudeCodeAgent::new();
        let result = ExecResult {
            exit_code: -1,
            stdout: String::new(),
            stderr: "Timed out".to_string(),
            duration_ms: 30000,
            timed_out: true,
        };

        let output = adapter.parse_output(&result);
        assert!(!output.success);
        assert!(output.summary.contains("timed out"));
    }

    #[test]
    fn claude_code_parse_output_failure() {
        let adapter = ClaudeCodeAgent::new();
        let result = ExecResult {
            exit_code: 1,
            stdout: String::new(),
            stderr: "API error: rate limited".to_string(),
            duration_ms: 1000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(!output.success);
        assert!(output.summary.contains("exited with code 1"));
    }

    #[test]
    fn claude_code_parse_output_non_json() {
        let adapter = ClaudeCodeAgent::new();
        let result = ExecResult {
            exit_code: 0,
            stdout: "I fixed the bug by editing main.rs".to_string(),
            stderr: String::new(),
            duration_ms: 5000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(output.success);
        assert!(output.summary.contains("fixed the bug"));
    }

    #[test]
    fn claude_code_parse_output_with_token_usage() {
        let adapter = ClaudeCodeAgent::new();
        let result = ExecResult {
            exit_code: 0,
            stdout: r#"{"result": "Done", "usage": {"input_tokens": 1500, "output_tokens": 300, "total_cost_usd": 0.05}}"#.to_string(),
            stderr: String::new(),
            duration_ms: 5000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(output.success);
        let usage = output.token_usage.unwrap();
        assert_eq!(usage.input_tokens, 1500);
        assert_eq!(usage.output_tokens, 300);
        assert_eq!(usage.total_cost_usd, Some(0.05));
    }

    #[test]
    fn claude_code_parse_output_with_file_changes() {
        let adapter = ClaudeCodeAgent::new();
        let result = ExecResult {
            exit_code: 0,
            stdout: r#"{
                "result": "Fixed bug",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "edit_file",
                                "input": {"file_path": "src/main.rs"}
                            }
                        ]
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "write_file",
                                "input": {"file_path": "src/new_module.rs"}
                            }
                        ]
                    }
                ]
            }"#
            .to_string(),
            stderr: String::new(),
            duration_ms: 5000,
            timed_out: false,
        };

        let output = adapter.parse_output(&result);
        assert!(output.success);
        assert_eq!(output.file_changes.len(), 2);
        assert_eq!(output.file_changes[0].file_path, "src/main.rs");
        assert_eq!(output.file_changes[0].change_type, ChangeType::Modified);
        assert_eq!(output.file_changes[1].file_path, "src/new_module.rs");
        assert_eq!(output.file_changes[1].change_type, ChangeType::Created);
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
    }

    #[test]
    fn claude_code_build_request_with_tools() {
        let adapter = ClaudeCodeAgent::new();
        let config = SandboxConfig {
            project_path: "/tmp/test".to_string(),
            working_dir: "/tmp/test".to_string(),
            tools: vec!["default".to_string(), "mcp__codegraph__*".to_string()],
            ..Default::default()
        };
        let request = adapter.build_request("Fix", "/tmp/test", &config, None);
        let idx = request.args.iter().position(|a| a == "--tools").unwrap();
        assert_eq!(request.args[idx + 1], "default");
        assert_eq!(request.args[idx + 2], "mcp__codegraph__*");
    }

    #[test]
    fn claude_code_build_request_with_mcp_configs() {
        let adapter = ClaudeCodeAgent::new();
        let config = SandboxConfig {
            project_path: "/tmp/test".to_string(),
            working_dir: "/tmp/test".to_string(),
            mcp_configs: vec!["/etc/mcp/codegraph.json".to_string()],
            ..Default::default()
        };
        let request = adapter.build_request("Fix", "/tmp/test", &config, None);
        let idx = request
            .args
            .iter()
            .position(|a| a == "--mcp-config")
            .unwrap();
        assert_eq!(request.args[idx + 1], "/etc/mcp/codegraph.json");
    }

    #[test]
    fn claude_code_build_request_with_allowed_tools() {
        let adapter = ClaudeCodeAgent::new();
        let config = SandboxConfig {
            project_path: "/tmp/test".to_string(),
            working_dir: "/tmp/test".to_string(),
            allowed_tools: vec!["Bash(git *)".to_string(), "Edit".to_string()],
            ..Default::default()
        };
        let request = adapter.build_request("Fix", "/tmp/test", &config, None);
        assert!(request.args.contains(&"--allowedTools".to_string()));
        assert!(request.args.contains(&"Bash(git *)".to_string()));
    }

    #[test]
    fn claude_code_build_request_with_agent_name() {
        let adapter = ClaudeCodeAgent::new();
        let config = SandboxConfig {
            project_path: "/tmp/test".to_string(),
            working_dir: "/tmp/test".to_string(),
            agent_name: Some("reviewer".to_string()),
            ..Default::default()
        };
        let request = adapter.build_request("Fix", "/tmp/test", &config, None);
        let idx = request.args.iter().position(|a| a == "--agent").unwrap();
        assert_eq!(request.args[idx + 1], "reviewer");
    }

    #[test]
    fn claude_code_build_request_with_append_system_prompt() {
        let adapter = ClaudeCodeAgent::new();
        let config = SandboxConfig {
            project_path: "/tmp/test".to_string(),
            working_dir: "/tmp/test".to_string(),
            append_system_prompt: Some("Focus on Rust".to_string()),
            ..Default::default()
        };
        let request = adapter.build_request("Fix", "/tmp/test", &config, None);
        let idx = request
            .args
            .iter()
            .position(|a| a == "--append-system-prompt")
            .unwrap();
        assert_eq!(request.args[idx + 1], "Focus on Rust");
    }

    #[test]
    fn claude_code_build_request_no_extra_flags_by_default() {
        let adapter = ClaudeCodeAgent::new();
        let config = test_config();
        let request = adapter.build_request("Fix", "/tmp/test", &config, None);
        assert!(!request.args.contains(&"--tools".to_string()));
        assert!(!request.args.contains(&"--mcp-config".to_string()));
        assert!(!request.args.contains(&"--allowedTools".to_string()));
        assert!(!request.args.contains(&"--disallowedTools".to_string()));
        assert!(!request.args.contains(&"--append-system-prompt".to_string()));
        assert!(!request.args.contains(&"--agent".to_string()));
        assert!(!request.args.contains(&"--agents".to_string()));
    }

    #[test]
    fn claude_code_build_request_with_subtask_config_override() {
        let adapter = ClaudeCodeAgent::new();
        let config = SandboxConfig {
            project_path: "/tmp/test".to_string(),
            working_dir: "/tmp/test".to_string(),
            tools: vec!["default".to_string()],
            ..Default::default()
        };
        let subtask_config = serde_json::json!({
            "tools": ["mcp__codegraph__*"],
            "agent_name": "reviewer"
        });
        let request = adapter.build_request("Fix", "/tmp/test", &config, Some(&subtask_config));

        // tools overridden by subtask config
        let idx = request.args.iter().position(|a| a == "--tools").unwrap();
        assert_eq!(request.args[idx + 1], "mcp__codegraph__*");
        // agent_name from subtask config
        let idx = request.args.iter().position(|a| a == "--agent").unwrap();
        assert_eq!(request.args[idx + 1], "reviewer");
    }

    #[test]
    fn claude_code_build_request_subtask_config_adds_new_key() {
        let adapter = ClaudeCodeAgent::new();
        let config = SandboxConfig {
            project_path: "/tmp/test".to_string(),
            working_dir: "/tmp/test".to_string(),
            ..Default::default()
        };
        let subtask_config = serde_json::json!({
            "append_system_prompt": "Focus on security"
        });
        let request = adapter.build_request("Fix", "/tmp/test", &config, Some(&subtask_config));
        let idx = request
            .args
            .iter()
            .position(|a| a == "--append-system-prompt")
            .unwrap();
        assert_eq!(request.args[idx + 1], "Focus on security");
    }
}
