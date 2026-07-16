//! Grok Build agent adapter.
//!
//! Wraps the xAI Grok Build CLI (`grok`) for sandbox execution.
//!
//! The worker's Python adapter owns the richer MCP JSON-to-TOML translation.
//! This Rust adapter keeps the engine-level factory and direct adapter API
//! compatible with the same headless command contract.

use crate::sandbox::{
    truncate_str, AgentAdapter, AgentOutput, ExecRequest, ExecResult, SandboxConfig, TokenUsage,
};
use uc_types::{ChangeType, FileChange};

/// Agent adapter for the xAI Grok Build CLI.
pub struct GrokBuildAgent;

impl GrokBuildAgent {
    /// Create a new Grok Build adapter.
    pub fn new() -> Self {
        Self
    }
}

impl Default for GrokBuildAgent {
    fn default() -> Self {
        Self::new()
    }
}

impl AgentAdapter for GrokBuildAgent {
    fn name(&self) -> &str {
        "grok-build"
    }

    fn build_request(
        &self,
        prompt: &str,
        working_dir: &str,
        config: &SandboxConfig,
        subtask_config: Option<&serde_json::Value>,
    ) -> ExecRequest {
        use super::{merge_agent_config, SubtaskAgentConfig};

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
        if !env_vars.contains_key("XAI_API_KEY") {
            if let Ok(key) = std::env::var("XAI_API_KEY") {
                env_vars.insert("XAI_API_KEY".to_string(), key);
            }
        }

        let effective_working_dir = if working_dir.is_empty() {
            effective_config.working_dir.clone()
        } else {
            working_dir.to_string()
        };
        let mut args = vec![
            "--no-auto-update".to_string(),
            "--no-alt-screen".to_string(),
            "--always-approve".to_string(),
            "--cwd".to_string(),
            effective_working_dir.clone(),
            "--output-format".to_string(),
            "streaming-json".to_string(),
            "-p".to_string(),
            prompt.to_string(),
        ];

        if !effective_config.tools.is_empty() {
            let tools = effective_config
                .tools
                .iter()
                .map(|tool| tool.strip_prefix("mcp__").unwrap_or(tool))
                .collect::<Vec<_>>()
                .join(",");
            args.extend(["--tools".to_string(), tools]);
        }
        for rule in &effective_config.allowed_tools {
            args.extend(["--allow".to_string(), rule.clone()]);
        }
        if !effective_config.disallowed_tools.is_empty() {
            args.extend([
                "--disallowed-tools".to_string(),
                effective_config.disallowed_tools.join(","),
            ]);
        }
        if let Some(ref rules) = effective_config.append_system_prompt {
            args.extend(["--rules".to_string(), rules.clone()]);
        }
        if let Some(ref name) = effective_config.agent_name {
            args.extend(["--agent".to_string(), name.clone()]);
        }
        if let Some(ref agents_json) = effective_config.agents_json {
            args.extend(["--agents".to_string(), agents_json.clone()]);
        }

        ExecRequest {
            command: "grok".to_string(),
            args,
            stdin: None,
            timeout_secs: effective_config.resource_limits.max_cpu_seconds,
            working_dir: effective_working_dir,
            env_vars,
        }
    }

    fn parse_output(&self, result: &ExecResult) -> AgentOutput {
        if result.timed_out {
            return AgentOutput {
                summary: "Grok Build execution timed out".to_string(),
                file_changes: vec![],
                token_usage: None,
                success: false,
            };
        }

        if result.exit_code != 0 {
            return AgentOutput {
                summary: format!(
                    "Grok Build exited with code {}: {}",
                    result.exit_code,
                    truncate_str(&result.stderr, 200)
                ),
                file_changes: vec![],
                token_usage: None,
                success: false,
            };
        }

        let output = result.stdout.trim();
        let events = parse_json_events(output);
        if events.is_empty() {
            return AgentOutput {
                summary: if output.is_empty() {
                    "Grok Build completed".to_string()
                } else {
                    truncate_str(output, 1000).to_string()
                },
                file_changes: parse_file_changes(output),
                token_usage: None,
                success: true,
            };
        }

        let mut final_summary = String::new();
        let mut last_summary = String::new();
        let mut message_chunks = Vec::new();
        let mut token_usage = None;

        for event in &events {
            let event_type = event
                .get("type")
                .or_else(|| event.get("event"))
                .or_else(|| event.get("kind"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_ascii_lowercase();

            if let Some(usage) = extract_token_usage(event) {
                token_usage = Some(usage);
            }

            let text = extract_text(event);
            if text.is_empty() || event_type.contains("thought") || event_type.contains("tool") {
                continue;
            }
            if event_type.contains("chunk") && event_type.contains("message") {
                message_chunks.push(text);
            } else if matches!(
                event_type.as_str(),
                "result" | "final" | "completion" | "done"
            ) || event.get("result").is_some()
            {
                final_summary = text;
            } else {
                last_summary = text;
            }
        }

        let summary = if !final_summary.is_empty() {
            final_summary
        } else if !message_chunks.is_empty() {
            message_chunks.concat()
        } else {
            last_summary
        };

        AgentOutput {
            summary: if summary.is_empty() {
                "Grok Build completed".to_string()
            } else {
                truncate_str(&summary, 1000).to_string()
            },
            file_changes: parse_file_changes(output),
            token_usage,
            success: true,
        }
    }
}

fn parse_json_events(output: &str) -> Vec<serde_json::Value> {
    if output.is_empty() {
        return Vec::new();
    }
    if let Ok(value) = serde_json::from_str::<serde_json::Value>(output) {
        return match value {
            serde_json::Value::Object(_) => vec![value],
            serde_json::Value::Array(values) => values
                .into_iter()
                .filter(|value| value.is_object())
                .collect(),
            _ => Vec::new(),
        };
    }

    output
        .lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .filter(|value| value.is_object())
        .collect()
}

fn extract_text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(text) => text.clone(),
        serde_json::Value::Array(values) => values
            .iter()
            .map(extract_text)
            .filter(|text| !text.is_empty())
            .collect::<Vec<_>>()
            .join(" "),
        serde_json::Value::Object(object) => {
            for key in [
                "result",
                "output_text",
                "text",
                "summary",
                "response",
                "message",
                "content",
                "output",
            ] {
                if let Some(value) = object.get(key) {
                    let text = extract_text(value);
                    if !text.is_empty() {
                        return text;
                    }
                }
            }
            if let Some(messages) = object.get("messages").and_then(|value| value.as_array()) {
                for message in messages.iter().rev() {
                    if message
                        .get("role")
                        .and_then(|role| role.as_str())
                        .map(|role| role == "assistant")
                        .unwrap_or(true)
                    {
                        let text = extract_text(message);
                        if !text.is_empty() {
                            return text;
                        }
                    }
                }
            }
            String::new()
        }
        _ => String::new(),
    }
}

fn extract_token_usage(value: &serde_json::Value) -> Option<TokenUsage> {
    let usage = value
        .get("usage")
        .or_else(|| value.get("token_usage"))?
        .as_object()?;
    let get_u64 = |keys: &[&str]| {
        keys.iter()
            .find_map(|key| usage.get(*key).and_then(|value| value.as_u64()))
            .unwrap_or(0)
    };
    let total_cost_usd = usage
        .get("total_cost_usd")
        .or_else(|| usage.get("cost_usd"))
        .and_then(|value| value.as_f64());
    Some(TokenUsage {
        input_tokens: get_u64(&["input_tokens", "prompt_tokens"]),
        output_tokens: get_u64(&["output_tokens", "completion_tokens"]),
        total_cost_usd,
    })
}

fn parse_file_changes(output: &str) -> Vec<FileChange> {
    let mut changes = Vec::new();
    for line in output.lines() {
        let trimmed = line.trim();
        for (prefix, change_type) in [
            ("Created:", ChangeType::Created),
            ("Modified:", ChangeType::Modified),
            ("Deleted:", ChangeType::Deleted),
        ] {
            if let Some(path) = trimmed.strip_prefix(prefix).map(str::trim) {
                if !path.is_empty() {
                    changes.push(FileChange {
                        file_path: path.to_string(),
                        change_type,
                        diff: String::new(),
                    });
                }
                break;
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
    fn grok_adapter_name() {
        assert_eq!(GrokBuildAgent::new().name(), "grok-build");
    }

    #[test]
    fn grok_build_request_is_headless() {
        let request = GrokBuildAgent::new().build_request(
            "Implement feature X",
            "/tmp/test",
            &test_config(),
            None,
        );
        assert_eq!(request.command, "grok");
        assert!(request.args.contains(&"-p".to_string()));
        assert!(request.args.contains(&"Implement feature X".to_string()));
        assert!(request.args.contains(&"streaming-json".to_string()));
        assert!(request.args.contains(&"--always-approve".to_string()));
        assert_eq!(request.working_dir, "/tmp/test");
    }

    #[test]
    fn grok_build_request_maps_tools() {
        let config = test_config();
        let subtask = serde_json::json!({
            "tools": ["default", "mcp__codegraph__*"],
            "allowed_tools": ["Bash(git *)"],
            "disallowed_tools": ["Bash(rm *)"],
        });
        let request =
            GrokBuildAgent::new().build_request("Fix", "/tmp/test", &config, Some(&subtask));
        assert!(request
            .args
            .windows(2)
            .any(|pair| { pair == ["--tools", "default,codegraph__*"].map(String::from) }));
        assert!(request
            .args
            .windows(2)
            .any(|pair| { pair == ["--allow", "Bash(git *)"].map(String::from) }));
    }

    #[test]
    fn grok_parse_streaming_result() {
        let stdout = [
            serde_json::json!({"type": "agent_message_chunk", "text": "Fixed "}),
            serde_json::json!({"type": "agent_message_chunk", "text": "the bug."}),
            serde_json::json!({
                "type": "result",
                "result": "Fixed the bug.",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }),
        ]
        .iter()
        .map(serde_json::Value::to_string)
        .collect::<Vec<_>>()
        .join("\n");
        let result = ExecResult {
            exit_code: 0,
            stdout,
            stderr: String::new(),
            duration_ms: 100,
            timed_out: false,
        };
        let output = GrokBuildAgent::new().parse_output(&result);
        assert!(output.success);
        assert_eq!(output.summary, "Fixed the bug.");
        assert_eq!(output.token_usage.unwrap().input_tokens, 10);
    }

    #[test]
    fn grok_parse_plain_output() {
        let result = ExecResult {
            exit_code: 0,
            stdout: "Implemented feature.\nCreated: src/feature.rs".to_string(),
            stderr: String::new(),
            duration_ms: 100,
            timed_out: false,
        };
        let output = GrokBuildAgent::new().parse_output(&result);
        assert!(output.success);
        assert!(output.summary.contains("Implemented feature"));
        assert_eq!(output.file_changes[0].file_path, "src/feature.rs");
    }
}
